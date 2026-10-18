"""Causal join of normalized on-chain fills to decision-time quotes.

The join consumes already captured records only.  It never fetches a chain,
reads an account, submits an order, or treats a public print as an account
fill.  A quote is eligible only when it is at or before the fill timestamp,
has the same token identity, and (when both are present) the same market
identity.  The output is deterministic and preserves unmatched fills so a
coverage failure cannot silently become a strategy result.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _timestamp(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a nonnegative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a nonnegative integer") from exc
    if result < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return result


def _event_id(fill: dict[str, Any]) -> str:
    value = _text(fill.get("event_id"))
    if not value:
        raise ValueError("fill event_id is required")
    return value


def _dedupe_fills(fills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for fill in fills:
        if not isinstance(fill, dict):
            raise ValueError("fill must be an object")
        event_id = _event_id(fill)
        prior = seen.get(event_id)
        if prior is not None and prior != fill:
            raise ValueError("conflicting_duplicate_fill")
        seen[event_id] = fill
    return [seen[key] for key in sorted(seen)]


def _quote_key(quote: dict[str, Any], index: int) -> tuple[str, str, int, str, int]:
    if not isinstance(quote, dict):
        raise ValueError("quote must be an object")
    token = _text(quote.get("token_id"))
    if not token:
        raise ValueError("quote token_id is required")
    market = _text(quote.get("market_id") or quote.get("condition_id"))
    timestamp = _timestamp(quote.get("timestamp_ms"), "quote timestamp_ms")
    quote_id = _text(quote.get("quote_id") or quote.get("event_id") or quote.get("source_path"))
    # The original index is a final deterministic tie-breaker for source rows
    # that have identical timestamps and no stable source identifier.
    return token, market, timestamp, quote_id, index


def _compatible(fill: dict[str, Any], quote: dict[str, Any]) -> bool:
    token = _text(fill.get("token_id"))
    if not token or token != _text(quote.get("token_id")):
        return False
    fill_market = _text(fill.get("market_id") or fill.get("condition_id"))
    quote_market = _text(quote.get("market_id") or quote.get("condition_id"))
    return not (fill_market and quote_market and fill_market != quote_market)


def join_orderfilled_quotes(
    fills: Iterable[dict[str, Any]],
    quotes: Iterable[dict[str, Any]],
    *,
    tolerance_ms: int = 2500,
) -> dict[str, Any]:
    """Join each normalized fill to the latest causal quote.

    ``tolerance_ms`` is an age bound.  Future quotes are always rejected,
    even if their absolute timestamp difference is inside that bound.
    """
    if isinstance(tolerance_ms, bool) or tolerance_ms < 0:
        raise ValueError("tolerance_ms must be a nonnegative integer")
    canonical_fills = _dedupe_fills(fills)
    indexed_quotes: list[tuple[tuple[str, str, int, str, int], dict[str, Any]]] = []
    for index, quote in enumerate(quotes):
        indexed_quotes.append((_quote_key(quote, index), quote))
    indexed_quotes.sort(key=lambda item: item[0])

    by_token: dict[str, list[tuple[tuple[str, str, int, str, int], dict[str, Any]]]] = defaultdict(list)
    for key, quote in indexed_quotes:
        by_token[key[0]].append((key, quote))

    joined: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for fill in canonical_fills:
        fill_id = _event_id(fill)
        token = _text(fill.get("token_id"))
        try:
            fill_timestamp = _timestamp(fill.get("timestamp_ms"), "fill timestamp_ms")
        except ValueError:
            unmatched.append({"event_id": fill_id, "reason": "fill_timestamp_missing_or_invalid"})
            continue
        candidates: list[tuple[int, tuple[str, str, int, str, int], dict[str, Any]]] = []
        for key, quote in by_token.get(token, []):
            quote_timestamp = key[2]
            if quote_timestamp > fill_timestamp:
                continue
            age = fill_timestamp - quote_timestamp
            if age > tolerance_ms or not _compatible(fill, quote):
                continue
            candidates.append((age, key, quote))
        if not candidates:
            reason = "no_causal_quote_within_tolerance"
            if not token:
                reason = "fill_token_id_missing_or_invalid"
            unmatched.append({"event_id": fill_id, "reason": reason})
            continue
        # Smallest age wins; the key tie-breaks are stable across input order.
        age, key, quote = min(candidates, key=lambda item: (item[0], item[1]))
        joined.append({
            "event_id": fill_id,
            "fill": fill,
            "quote": quote,
            "quote_age_ms": age,
            "causal": True,
            "quote_timestamp_ms": key[2],
            "fill_timestamp_ms": fill_timestamp,
        })

    total = len(canonical_fills)
    return {
        "schema_version": 1,
        "joined": joined,
        "unmatched": unmatched,
        "fill_count": total,
        "joined_count": len(joined),
        "unmatched_count": len(unmatched),
        "coverage": (len(joined) / total) if total else 0.0,
        "tolerance_ms": int(tolerance_ms),
        "future_quote_leakage_allowed": False,
    }


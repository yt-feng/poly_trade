"""H508 x H301 paper forward-admission adapter.

This module is deliberately separate from the frozen quality-first episode.
It consumes a cloud-created, preselected H508 candidate bundle together with
the H301 share-floor projection and contemporaneous execution evidence.  It
does not fit a model, fetch accounts, sign orders, or enable live trading.

The adapter fails closed when the bundle omits the current order contract,
depth/fee/latency provenance, or explicit paper fill-admission fields.  The
only policy comparison is the preregistered 0.50 versus 0.75 fraction; no
holdout tuning is performed here.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

try:
    from .causal_cash_ledger import replay_cash_ledger
except ImportError:  # direct execution from the repository root
    from causal_cash_ledger import replay_cash_ledger


FORMAT = "btc5m-h508-h301-forward-input-v1"
RESULT_FORMAT = "btc5m-h508-h301-forward-adapter-v1"
HYPOTHESIS_ID = "H508_H301_SHARE_FLOOR_DEPTH_ADAPTIVE_FORWARD"
FRACTIONS = (0.50, 0.75)
REQUIRED_FILLS = 10
INITIAL_CASH = 10.0
MINIMUM_SHARES = 5.0
MAX_ROWS = 100_000
MAX_QUOTES_PER_MARKET = 10_000


class ContractError(ValueError):
    """Raised when a forward bundle is not safe to evaluate."""


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _digest(value: Any) -> str:
    # Bundle producers hash the complete market_metadata object as UTF-8 JSON,
    # with sorted keys, compact separators, ASCII escapes and no NaN values.
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _require_digest(container: Mapping[str, Any], name: str, errors: list[str]) -> None:
    value = container.get(name)
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
        errors.append(f"missing_or_invalid_digest:{name}")


def validate_document(document: Any) -> dict[str, Any]:
    """Validate a complete bundle without reading large source data."""
    if not isinstance(document, dict):
        raise ContractError("input_must_be_object")
    errors: list[str] = []
    if document.get("format") != FORMAT:
        errors.append("format_mismatch")

    source = document.get("source")
    if not isinstance(source, dict):
        errors.append("source_provenance_missing")
        source = {}
    for name in ("h508_summary_sha256", "h301_projection_sha256", "market_constraints_sha256",
                 "market_metadata_sha256", "depth_source_sha256", "fee_source_sha256",
                 "latency_source_sha256"):
        _require_digest(source, name, errors)
    if source.get("source_commit") != "bbda5f2050685f63153621a54641ebdfab943487":
        errors.append("h508_source_commit_mismatch")

    protocol = document.get("protocol")
    if not isinstance(protocol, dict):
        errors.append("protocol_missing")
        protocol = {}
    expected = {
        "initial_cash_usd": INITIAL_CASH,
        "minimum_order_shares": MINIMUM_SHARES,
        "required_fills": REQUIRED_FILLS,
        "same_ledger": True,
        "no_cross_window_concatenation": True,
        "paper_only": True,
    }
    for name, expected_value in expected.items():
        actual = protocol.get(name)
        if isinstance(expected_value, float):
            if _finite(actual) is None or abs(float(actual) - expected_value) > 1e-9:
                errors.append(f"protocol_mismatch:{name}")
        elif actual != expected_value:
            errors.append(f"protocol_mismatch:{name}")
    for name in ("minimum_order_usd", "fee_rate", "cash_release_delay_seconds",
                 "exit_latency_seconds", "max_exit_quote_wait_seconds", "depth_haircut"):
        if _finite(protocol.get(name)) is None:
            errors.append(f"protocol_missing_or_nonfinite:{name}")
    if _finite(protocol.get("minimum_order_usd")) is not None and float(protocol["minimum_order_usd"]) <= 0:
        # The current market contract must be read back before replay.  A
        # legacy zero-floor projection is not execution evidence.
        errors.append("minimum_order_usd_not_currently_verified")
    if _finite(protocol.get("fee_rate")) is not None and not 0 <= float(protocol["fee_rate"]) <= 1:
        errors.append("fee_rate_out_of_range")
    if _finite(protocol.get("depth_haircut")) is not None and not 0 < float(protocol["depth_haircut"]) <= 1:
        errors.append("depth_haircut_out_of_range")
    if _finite(protocol.get("cash_release_delay_seconds")) is not None and float(protocol["cash_release_delay_seconds"]) < 0:
        errors.append("cash_release_delay_negative")
    if _finite(protocol.get("exit_latency_seconds")) is not None and float(protocol["exit_latency_seconds"]) < 0:
        errors.append("exit_latency_negative")
    if _finite(protocol.get("max_exit_quote_wait_seconds")) is not None and float(protocol["max_exit_quote_wait_seconds"]) < 0:
        errors.append("exit_wait_negative")

    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        errors.append("candidate_rows_missing")
        rows = []
    elif len(rows) > MAX_ROWS:
        errors.append("candidate_row_budget_exceeded")
    for index, row in enumerate(rows[:MAX_ROWS]):
        if not isinstance(row, dict):
            errors.append(f"row_not_object:{index}")
            continue
        for name in ("slug", "entry_ts", "market_end_ts", "side", "fill_price", "entry_depth",
                     "slippage", "label_basis", "label_available_ts", "outcome_up"):
            if name not in row or row.get(name) in (None, ""):
                errors.append(f"row_missing:{index}:{name}")
        if row.get("paper_fill_admitted") is not True:
            errors.append(f"row_not_explicitly_paper_admitted:{index}")
        if row.get("owner_scope_verified") is not False:
            errors.append(f"row_owner_scope_must_be_false_for_paper_adapter:{index}")
        if row.get("side") not in {"up", "down"}:
            errors.append(f"row_side_invalid:{index}")
        if _finite(row.get("fill_price")) is None or not 0 < float(row["fill_price"]) < 1:
            errors.append(f"row_price_invalid:{index}")
        if _finite(row.get("entry_depth")) is None or float(row["entry_depth"]) < 0:
            errors.append(f"row_depth_invalid:{index}")

    # The official CLOB contract is market-specific and can change while a
    # paper bundle is being assembled.  A numeric USD floor alone is not
    # enough: bind every candidate row to metadata available at its decision
    # cutoff. There is no registered maximum age, so this checks causal
    # availability only; it does not establish freshness or source authenticity.
    market_metadata = document.get("market_metadata")
    if not isinstance(market_metadata, dict):
        errors.append("market_metadata_missing")
        market_metadata = {}
    try:
        metadata_digest = _digest(market_metadata)
    except (TypeError, ValueError):
        errors.append("market_metadata_not_canonical_json")
    else:
        declared_digest = source.get("market_metadata_sha256")
        if not isinstance(declared_digest, str) or declared_digest.lower() != metadata_digest:
            errors.append("market_metadata_digest_mismatch")
    metadata_validation: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:MAX_ROWS]):
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug", "")).strip()
        metadata = market_metadata.get(slug)
        if not isinstance(metadata, dict):
            errors.append(f"market_metadata_missing:{index}")
            continue
        for name in ("min_order_size", "tick_size", "itode", "observed_ts"):
            if name not in metadata or metadata.get(name) in (None, ""):
                errors.append(f"market_metadata_field_missing:{index}:{name}")
        if _finite(metadata.get("min_order_size")) is None or float(metadata.get("min_order_size")) <= 0:
            errors.append(f"market_metadata_min_order_size_invalid:{index}")
        elif float(metadata["min_order_size"]) > MINIMUM_SHARES:
            # Preserve the frozen replay floor rather than silently simulating
            # quantities the supplied market would reject.
            errors.append(f"market_minimum_exceeds_frozen_shares:{index}")
        if _finite(metadata.get("tick_size")) is None or float(metadata.get("tick_size")) <= 0:
            errors.append(f"market_metadata_tick_size_invalid:{index}")
        if type(metadata.get("itode")) is not bool:
            errors.append(f"market_metadata_itode_invalid:{index}")
        elif metadata["itode"]:
            # The frozen adapter has no entry-delay execution model. An exit
            # latency parameter or an asserted paper fill cannot stand in for it.
            errors.append(f"market_entry_delay_unsupported:{index}")

        observed = _aware_timestamp(metadata.get("observed_ts"))
        cutoff_field = "decision_ts" if "decision_ts" in row else "entry_ts"
        cutoff = _aware_timestamp(row.get(cutoff_field))
        entry = _aware_timestamp(row.get("entry_ts"))
        if observed is None:
            errors.append(f"market_metadata_observed_ts_invalid:{index}")
        if cutoff is None or entry is None:
            errors.append(f"row_metadata_cutoff_invalid:{index}")
        elif cutoff > entry:
            errors.append(f"row_decision_after_entry:{index}")
        if observed is not None and cutoff is not None and observed > cutoff:
            errors.append(f"market_metadata_observed_after_cutoff:{index}")

        # The average fill price may legitimately fall between ticks. Only a
        # concrete submitted order/limit price supports an alignment check.
        price_fields = [name for name in ("order_price", "limit_price") if name in row]
        tick = _finite(metadata.get("tick_size"))
        for name in price_fields:
            price = _finite(row[name])
            if price is None or not 0 < price < 1:
                errors.append(f"row_order_price_invalid:{index}:{name}")
            elif tick is not None and tick > 0:
                try:
                    aligned = Decimal(str(row[name])) % Decimal(str(metadata["tick_size"])) == 0
                except InvalidOperation:
                    aligned = False
                if not aligned:
                    errors.append(f"row_order_price_off_tick:{index}:{name}")
        metadata_validation.append({
            "row_index": index,
            "cutoff_field": cutoff_field,
            "observation_timing": "available_by_cutoff",
            "freshness": "unverified_no_registered_max_age",
            "source_authenticity": "unverified_digest_binds_content_only",
            "tick_execution_applicability": (
                "order_price_alignment_checked" if price_fields else "unverified_fill_vwap_only"
            ),
        })

    quotes = document.get("quotes")
    if not isinstance(quotes, dict):
        errors.append("quotes_missing")
        quotes = {}
    for slug, values in list(quotes.items())[:MAX_ROWS]:
        if not isinstance(slug, str) or not isinstance(values, list):
            errors.append(f"quotes_invalid:{slug}")
            continue
        if len(values) > MAX_QUOTES_PER_MARKET:
            errors.append(f"quote_budget_exceeded:{slug}")
        for qindex, quote in enumerate(values[:MAX_QUOTES_PER_MARKET]):
            if not isinstance(quote, dict) or not quote.get("ts_iso", quote.get("ts_utc")):
                errors.append(f"quote_missing_timestamp:{slug}:{qindex}")

    if errors:
        raise ContractError(";".join(errors[:40]))
    return {"source": source, "protocol": protocol, "rows": rows, "quotes": quotes,
            "market_metadata": market_metadata, "metadata_validation": metadata_validation}


def evaluate(document: Any) -> dict[str, Any]:
    """Run the two preregistered paper policies on one cloud-created bundle."""
    validated = validate_document(document)
    protocol = validated["protocol"]
    common = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source": validated["source"],
        "protocol": protocol,
        "market_metadata": validated["market_metadata"],
        "metadata_validation": validated["metadata_validation"],
        "tested_fractions": list(FRACTIONS),
        "required_fills": REQUIRED_FILLS,
        "promotion_eligible": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "new_data_collected": False,
        "model_refit": False,
        "evidence_boundary": "paper_forward_replay; owner scope is explicitly false",
    }
    results: dict[str, Any] = {}
    for fraction in FRACTIONS:
        results[f"{fraction:.2f}"] = replay_cash_ledger(
            validated["rows"], validated["quotes"],
            initial_cash=float(protocol["initial_cash_usd"]),
            base_fraction=fraction,
            fee_rate=float(protocol["fee_rate"]),
            minimum_notional=float(protocol["minimum_order_usd"]),
            minimum_shares=float(protocol["minimum_order_shares"]),
            depth_haircut=float(protocol["depth_haircut"]),
            exit_latency_seconds=float(protocol["exit_latency_seconds"]),
            max_exit_quote_wait_seconds=float(protocol["max_exit_quote_wait_seconds"]),
            cash_release_delay_seconds=float(protocol["cash_release_delay_seconds"]),
            required_fills=REQUIRED_FILLS,
        )
    common["results"] = results
    common["decision"] = "retain_as_paper_only_until_owner_bundle_and_selection_gate"
    return common


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    result = evaluate(document)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "computed", "format": RESULT_FORMAT, "promotion_eligible": False}, sort_keys=True))


if __name__ == "__main__":
    main()

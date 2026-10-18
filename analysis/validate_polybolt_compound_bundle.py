"""Offline admission for a PolyBolt-backed authenticated compound bundle.

This is a stricter v2 boundary than the legacy authenticated bundle validator.
It accepts an already captured artifact only; it never connects to Polymarket,
reads credentials, submits orders, or turns public observations into fills.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from authenticated_compound_reconciler import reconcile_authenticated_chain
from polybolt_twap_adapter import (
    PolyBoltContractError,
    build_continuity_manifest,
    normalize_polybolt_event,
)


SCHEMA = "authenticated-polybolt-compound-bundle-v1"
MARKET_SLUG = re.compile(r"^btc-updown-5m-[0-9]+$")


def _fail(reason: str, *, event_count: int = 0, source_event_count: int = 0) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "required_fills": 10,
        "event_count": int(event_count),
        "source_event_count": int(source_event_count),
        "source_continuity_ok": False,
        "gate_passed": False,
        "failure_reason": reason,
    }


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def _market_windows(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
    windows = document.get("market_windows")
    if not isinstance(windows, list) or not windows:
        return {}, "market_windows_required"
    out: dict[str, dict[str, Any]] = {}
    for item in windows:
        if not isinstance(item, dict):
            return {}, "invalid_market_window"
        market_id = item.get("market_id")
        slug = item.get("slug")
        start = item.get("window_start_ms")
        end = item.get("window_end_ms")
        if not isinstance(market_id, str) or not market_id.strip():
            return {}, "market_id_required"
        if not isinstance(slug, str) or not MARKET_SLUG.fullmatch(slug):
            return {}, "btc_five_minute_slug_required"
        if not _integer(start) or not _integer(end) or end - start != 300_000:
            return {}, "btc_five_minute_window_required"
        if item.get("asset") != "btc":
            return {}, "btc_asset_required"
        if market_id in out:
            return {}, "duplicate_market_id"
        out[market_id] = {"market_id": market_id, "slug": slug, "window_start_ms": start, "window_end_ms": end}
    return out, None


def validate_bundle_document(document: Any, *, required_fills: int = 10) -> dict[str, Any]:
    """Validate a source-and-account bundle without exposing account identifiers."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return _fail("invalid_bundle_schema")
    if type(required_fills) is not int or required_fills <= 0:
        return _fail("invalid_required_fills")
    owner = document.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return _fail("bundle_owner_required")
    source_events = document.get("source_events")
    events = document.get("events")
    if not isinstance(source_events, list) or not source_events:
        return _fail("source_events_required", event_count=len(events) if isinstance(events, list) else 0)
    if not isinstance(events, list):
        return _fail("bundle_events_required", source_event_count=len(source_events))
    windows, error = _market_windows(document)
    if error:
        return _fail(error, event_count=len(events), source_event_count=len(source_events))
    account_market_ids: set[str] = set()
    for row in events:
        if not isinstance(row, dict) or row.get("market_id") not in windows:
            return _fail("account_event_market_identity_mismatch", event_count=len(events), source_event_count=len(source_events))
        account_market_ids.add(row["market_id"])
    source_market_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in source_events:
        if not isinstance(raw, dict) or raw.get("market_id") not in windows:
            return _fail("source_event_market_identity_mismatch", event_count=len(events), source_event_count=len(source_events))
        source_market_ids.add(raw["market_id"])
        try:
            record = normalize_polybolt_event(raw)
        except PolyBoltContractError:
            return _fail("source_contract_violation", event_count=len(events), source_event_count=len(source_events))
        window = windows[raw["market_id"]]
        if record["event_ms"] > window["window_end_ms"]:
            return _fail("source_event_after_market_window", event_count=len(events), source_event_count=len(source_events))
        record["market_id"] = raw["market_id"]
        normalized.append(record)
    if source_market_ids != account_market_ids:
        return _fail("source_account_market_coverage_mismatch", event_count=len(events), source_event_count=len(source_events))
    try:
        manifest = build_continuity_manifest(normalized)
    except PolyBoltContractError:
        return _fail("source_continuity_missing", event_count=len(events), source_event_count=len(source_events))
    if not manifest["continuity_ok"]:
        return {
            **_fail("source_continuity_not_admissible", event_count=len(events), source_event_count=len(source_events)),
            "source_continuity_manifest": manifest,
        }
    result = reconcile_authenticated_chain(
        events,
        owner=owner,
        required_fills=required_fills,
    )
    # Keep the source manifest compact and omit owner/account identifiers.
    return {
        "schema": SCHEMA,
        "required_fills": int(required_fills),
        "event_count": len(events),
        "source_event_count": len(source_events),
        "source_update_count": sum(record.get("kind") == "update" for record in normalized),
        "source_continuity_ok": True,
        "source_reconnect_segments": manifest["reconnect_segments"],
        "market_window_count": len(windows),
        "fills": int(result.get("fills", 0)),
        "consecutive": bool(result.get("consecutive", False)),
        "paper_replay_passed": bool(result.get("paper_replay_passed", False)),
        "gate_passed": bool(result.get("gate_passed", False)),
        "failure_reason": result.get("failure_reason"),
        "ending_cash": result.get("ending_cash"),
        "net_pnl": result.get("net_pnl"),
        "owner_scoped": bool(result.get("owner_scoped", False)),
        "identity_complete": bool(result.get("identity_complete", False)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--required-fills", type=int, default=10)
    args = parser.parse_args()
    try:
        document = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = validate_bundle_document(document, required_fills=args.required_fills)
    except (OSError, json.JSONDecodeError):
        result = _fail("bundle_read_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Build an H534 fixed-0.55 compound bundle from captured evidence.

H534 is the dense-grid paper candidate on the pinned strict-quality old-clean
source.  This builder is offline only: it accepts exactly ten already captured
taker rows, preserves the per-fill strategy trace, and immediately applies the
H534 validator profile.  It never fetches data, reads credentials, or submits
orders.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping

try:
    from .validate_polybolt_compound_bundle import (
        H534_STRATEGY_CONFIG_SHA256,
        H534_STRATEGY_ID,
        H534_STRATEGY_PROFILE,
        H534_STRATEGY_VERSION,
        validate_bundle_document,
    )
except ImportError:
    from validate_polybolt_compound_bundle import (
        H534_STRATEGY_CONFIG_SHA256,
        H534_STRATEGY_ID,
        H534_STRATEGY_PROFILE,
        H534_STRATEGY_VERSION,
        validate_bundle_document,
    )


REQUIRED_FILLS = 10
TRACE_KEYS = {
    "decision_ref",
    "sequence_no",
    "cash_fraction",
    "profitable_released_exits_before",
}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_required")
    return value.strip()


def _required(mapping: Mapping[str, Any], field: str) -> Any:
    if field not in mapping or mapping[field] in (None, ""):
        raise ValueError(f"{field}_required")
    return mapping[field]


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _strategy_trace(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("strategy_decision")
    if nested is not None and not isinstance(nested, Mapping):
        raise ValueError("strategy_decision_must_be_object")
    source = dict(nested) if isinstance(nested, Mapping) else dict(row)
    trace = {
        "decision_ref": _text(source.get("decision_ref", row.get("strategy_decision_ref")), "strategy_decision_ref"),
        "sequence_no": _required(source, "sequence_no"),
        "cash_fraction": _required(source, "cash_fraction"),
        "profitable_released_exits_before": _required(source, "profitable_released_exits_before"),
    }
    if set(trace) != TRACE_KEYS:
        raise ValueError("strategy_decision_trace_invalid")
    if type(trace["sequence_no"]) is not int or not 1 <= trace["sequence_no"] <= REQUIRED_FILLS:
        raise ValueError("strategy_decision_sequence_invalid")
    if type(trace["profitable_released_exits_before"]) is not int or trace["profitable_released_exits_before"] != 0:
        raise ValueError("strategy_decision_profit_state_invalid")
    return trace


def _normalize_row(raw: Mapping[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(raw))
    row.pop("strategy_decision", None)
    if "entry_ts_ms" not in row and "entry_ts" in row:
        row["entry_ts_ms"] = row["entry_ts"]
    if "cash_available_ts_ms" not in row and "cash_available_ts" in row:
        row["cash_available_ts_ms"] = row["cash_available_ts"]
    row["strategy_decision_ref"] = trace["decision_ref"]
    row["strategy_decision_sha256"] = _canonical_hash(trace)
    for field in ("entry_ts_ms", "cash_available_ts_ms", "source_ref", "depth_ref", "market_id"):
        _required(row, field)
    return row


def build_h534_bundle(capture: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(capture, Mapping):
        raise ValueError("capture_must_be_object")
    raw_rows = capture.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != REQUIRED_FILLS:
        raise ValueError("exactly_ten_rows_required")
    rows: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("row_must_be_object")
        trace = _strategy_trace(raw)
        traces.append(trace)
        rows.append(_normalize_row(raw, trace))
    if sorted(trace["sequence_no"] for trace in traces) != list(range(1, REQUIRED_FILLS + 1)):
        raise ValueError("strategy_decisions_must_cover_one_to_ten")

    strategy = capture.get("strategy")
    if strategy is None:
        strategy = {"id": H534_STRATEGY_ID, "version": H534_STRATEGY_VERSION, "config_sha256": H534_STRATEGY_CONFIG_SHA256}
    elif not isinstance(strategy, Mapping):
        raise ValueError("strategy_must_be_object")
    elif (
        strategy.get("id") != H534_STRATEGY_ID
        or strategy.get("version") != H534_STRATEGY_VERSION
        or strategy.get("config_sha256") != H534_STRATEGY_CONFIG_SHA256
    ):
        raise ValueError("strategy_must_be_h534")

    required = (
        "owner", "ledger_id", "chain_id", "starting_checkpoint_id", "closing_balance_checkpoint_id",
        "initial_cash", "closing_balance", "market_windows", "source_events", "depth_snapshots",
        "settlement_receipts", "cash_release_receipts",
    )
    for field in required:
        _required(capture, field)
    return {
        "schema": "authenticated-polybolt-compound-bundle-v2",
        "required_fills": REQUIRED_FILLS,
        "owner": capture["owner"],
        "ledger_id": capture["ledger_id"],
        "chain_id": capture["chain_id"],
        "starting_checkpoint_id": capture["starting_checkpoint_id"],
        "initial_cash": capture["initial_cash"],
        "closing_balance": capture["closing_balance"],
        "closing_balance_checkpoint_id": capture["closing_balance_checkpoint_id"],
        "strategy": dict(strategy),
        "market_windows": deepcopy(capture["market_windows"]),
        "cash_release_receipts": deepcopy(capture["cash_release_receipts"]),
        "settlement_receipts": deepcopy(capture["settlement_receipts"]),
        "source_events": deepcopy(capture["source_events"]),
        "depth_snapshots": deepcopy(capture["depth_snapshots"]),
        "events": rows,
        "strategy_decisions": traces,
    }


def validate_built_h534_bundle(capture: Mapping[str, Any]) -> dict[str, Any]:
    return validate_bundle_document(build_h534_bundle(capture), strategy_profile=H534_STRATEGY_PROFILE)


__all__ = ["build_h534_bundle", "validate_built_h534_bundle"]

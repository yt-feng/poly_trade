"""Audit existing capture coverage and executable quote capacity.

This module is deliberately not a strategy backtest. It uses only snapshot
fields at a pre-registered decision horizon and keeps quote markout labels,
official outcomes, fills, and settlement receipts in separate counters.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


DECISION_TTE_LOW = 15.0
DECISION_TTE_HIGH = 25.0


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _has_execution_receipt(value: Any) -> bool:
    """Search only for receipt-like keys; quote fields do not qualify."""
    receipt_keys = {
        # Market-data trade_id fields are intentionally excluded: they identify
        # a venue print, not an order or a fill for this account.
        "order_id", "orderId", "fill_id", "fillId",
        "execution_id", "executionId", "settlement_id", "settlementId",
        "filled_size", "filledSize", "actual_fill", "actualFill",
    }
    if isinstance(value, dict):
        if any(key in value for key in receipt_keys):
            return True
        return any(_has_execution_receipt(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_execution_receipt(item) for item in value)
    return False


def _quote_notional(quote: Any) -> Decimal | None:
    if not isinstance(quote, dict):
        return None
    ask = _positive_decimal(quote.get("ask"))
    size = _positive_decimal(quote.get("ask_size"))
    return ask * size if ask is not None and size is not None else None


def audit(input_root: Path, *, minimum_notional: Decimal = Decimal("5")) -> dict[str, Any]:
    snapshots = sorted(input_root.rglob("snapshots-*.jsonl.gz"))
    labels = sorted(input_root.rglob("labels-*.jsonl.gz"))
    if not snapshots:
        raise ValueError("no snapshot archives found")

    counts: collections.Counter[str] = collections.Counter()
    tte_rows: list[dict[str, Any]] = []
    snapshot_keys: set[tuple[str, int]] = set()
    receipt_rows = 0
    for path in snapshots:
        for row in _iter_jsonl(path):
            counts["snapshot_rows"] += 1
            for field in (
                "poly_valid", "pair_quote_valid", "rules_valid", "fee_model_valid",
                "twap60_valid", "matching_reference_feed_valid", "official_threshold_valid",
                "resolution_reference_valid", "spot_depth20_valid", "poly_ws_data_valid",
            ):
                if row.get(field) is True:
                    counts[field] += 1
            if _has_execution_receipt(row):
                receipt_rows += 1
            micro = row.get("microstructure") if isinstance(row.get("microstructure"), dict) else {}
            tte = micro.get("time_to_expiry_seconds")
            if isinstance(tte, (int, float)) and DECISION_TTE_LOW <= float(tte) <= DECISION_TTE_HIGH:
                counts["decision_window_rows"] += 1
                tte_rows.append(row)
                sample_ms = row.get("sample_ms")
                slug = row.get("slug")
                if isinstance(slug, str) and isinstance(sample_ms, int):
                    snapshot_keys.add((slug, sample_ms))

    decision_counts: collections.Counter[str] = collections.Counter()
    capacity = {"up": [], "down": []}
    missing = collections.Counter()
    min_order_values: collections.Counter[str] = collections.Counter()
    for row in tte_rows:
        micro = row.get("microstructure") if isinstance(row.get("microstructure"), dict) else {}
        rules = micro.get("rules") if isinstance(micro.get("rules"), dict) else {}
        for field in ("poly_valid", "pair_quote_valid", "rules_valid", "fee_model_valid",
                      "twap60_valid", "official_threshold_valid", "resolution_reference_valid"):
            if row.get(field) is True:
                decision_counts[field] += 1
        for name in ("up", "down"):
            quote = row.get(name)
            if isinstance(quote, dict):
                ask = _positive_decimal(quote.get("ask"))
                ask_size = _positive_decimal(quote.get("ask_size"))
                notional = _quote_notional(quote)
                if notional is not None:
                    capacity[name].append(notional)
                    if notional >= minimum_notional:
                        decision_counts[f"{name}_ask_depth_ge_minimum"] += 1
                if ask is not None and ask_size is not None:
                    # Fee model is recorded as a field-presence check only. No
                    # quote markout is converted into realized PnL here.
                    fee = _positive_decimal(rules.get("fees", {}).get("rate"))
                    if fee is not None:
                        decision_counts[f"{name}_fee_field_present"] += 1
            else:
                missing[f"{name}_quote"] += 1
        min_order = rules.get("min_order_size")
        if min_order is not None:
            min_order_values[str(min_order)] += 1
        else:
            missing["min_order_size"] += 1

    label_counts: collections.Counter[str] = collections.Counter()
    label_horizons: collections.Counter[str] = collections.Counter()
    joined = 0
    official_outcomes = 0
    for path in labels:
        for row in _iter_jsonl(path):
            label_counts["label_rows"] += 1
            kind = row.get("kind")
            if isinstance(kind, str):
                label_counts[f"kind:{kind}"] += 1
            horizon = row.get("horizon_seconds")
            label_horizons[str(horizon)] += 1
            key = (row.get("slug"), row.get("prediction_ms"))
            if key in snapshot_keys:
                joined += 1
            if kind in {"official_clob_winner", "market_resolved"}:
                official_outcomes += 1

    def summary(values: list[Decimal]) -> dict[str, str | int | None]:
        if not values:
            return {"count": 0, "min": None, "median": None, "max": None}
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "min": str(ordered[0]),
            "median": str(ordered[len(ordered) // 2]),
            "max": str(ordered[-1]),
        }

    result = {
        "schema_version": 1,
        "audit": "existing_capture_execution_field_coverage",
        "decision_window_seconds_before_end": [DECISION_TTE_LOW, DECISION_TTE_HIGH],
        "minimum_notional": str(minimum_notional),
        "archives": {"snapshots": len(snapshots), "labels": len(labels)},
        "counts": dict(counts),
        "decision_window_counts": dict(decision_counts),
        "ask_depth_notional": {name: summary(values) for name, values in capacity.items()},
        "min_order_size_values": dict(min_order_values),
        "missing_decision_fields": dict(missing),
        "label_counts": dict(label_counts),
        "label_horizons": dict(label_horizons),
        "decision_snapshot_label_joins": joined,
        "official_outcome_label_rows": official_outcomes,
        "execution_receipt_snapshot_rows": receipt_rows,
        "execution_receipts_present": receipt_rows > 0,
        "interpretation": {
            "strategy_pnl": "not_computed",
            "quote_markout_is_fill_pnl": False,
            "official_outcomes_are_execution_receipts": False,
            "live_orders": False,
        },
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "snapshot_rows": result["counts"].get("snapshot_rows", 0),
        "decision_window_rows": result["counts"].get("decision_window_rows", 0),
        "decision_pair_quote_valid": result["decision_window_counts"].get("pair_quote_valid", 0),
        "decision_up_depth_ge_minimum": result["decision_window_counts"].get("up_ask_depth_ge_minimum", 0),
        "decision_down_depth_ge_minimum": result["decision_window_counts"].get("down_ask_depth_ge_minimum", 0),
        "execution_receipts_present": result["execution_receipts_present"],
        "official_outcome_label_rows": result["official_outcome_label_rows"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()

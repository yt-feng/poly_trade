"""Summarize existing capture evidence without reacquiring or replaying rows."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid object: {name}")
    return value


def summarize(root: Path) -> dict[str, Any]:
    execution = _load(root, "current-capture-execution-audit.json")
    outcome = _load(root, "current-capture-official-outcome.json")
    logistic = _load(root, "current-capture-settlement-logistic.json")
    return {
        "format": "existing-h80-capture-evidence-summary-v1",
        "source_scope": "existing current-capture-execution-audit-20261018 artifacts; no acquisition or row replay",
        "execution": {
            "snapshot_rows": execution.get("counts", {}).get("snapshot_rows"),
            "decision_window_rows": execution.get("counts", {}).get("decision_window_rows"),
            "pair_quote_valid": execution.get("counts", {}).get("pair_quote_valid"),
            "twap60_valid": execution.get("counts", {}).get("twap60_valid"),
            "official_threshold_valid": execution.get("counts", {}).get("official_threshold_valid"),
            "official_outcome_label_rows": execution.get("official_outcome_label_rows"),
            "quote_markout_label_rows": execution.get("label_counts", {}).get("kind:quote_markout_not_fill_pnl"),
            "execution_receipts_present": execution.get("execution_receipts_present"),
            "execution_receipt_snapshot_rows": execution.get("execution_receipt_snapshot_rows"),
        },
        "official_outcome_candidates": {
            name: {
                "selection_trades": value.get("train_selection", {}).get("paper_settlement_trades"),
                "selection_pnl": value.get("train_selection", {}).get("paper_settlement_pnl"),
                "test_trades": value.get("test", {}).get("paper_settlement_trades"),
                "test_pnl": value.get("test", {}).get("paper_settlement_pnl"),
            }
            for name, value in outcome.get("metrics", {}).items()
            if isinstance(value, dict)
        },
        "fixed_logistic_candidate": {
            "eligible_markets": logistic.get("eligible_markets_with_complete_features"),
            "selection_trades": logistic.get("metrics", {}).get("train_selection", {}).get("paper_settlement_trades"),
            "selection_pnl": logistic.get("metrics", {}).get("train_selection", {}).get("paper_settlement_pnl"),
            "test_trades": logistic.get("metrics", {}).get("test", {}).get("paper_settlement_trades"),
            "test_pnl": logistic.get("metrics", {}).get("test", {}).get("paper_settlement_pnl"),
        },
        "decision": "existing_capture_paper_only_no_canary_candidate",
        "root_cause": (
            "Official outcome labels are available for a small joined subset, but no account execution receipts "
            "are present. The lower-ask and fixed logistic candidates lose on independent test periods; the "
            "target-sign candidate has too few trades and also loses on test. Quote markouts cannot repair this evidence gap."
        ),
        "evidence_boundary": "Official outcome labels support paper settlement analysis only; they are not order, fill, settlement-receipt or reusable-cash evidence.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"decision": result["decision"], "actual_fills": 0, "verified_canary_fills": 0}, sort_keys=True))


if __name__ == "__main__":
    main()

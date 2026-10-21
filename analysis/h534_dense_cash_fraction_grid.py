"""H534: pre-registered dense fixed-cash-fraction grid on the frozen H508 tape.

This is a paper-only refinement of H508.  It reuses the unchanged strict-quality
old-clean source and the H434/H452 signal, then tests the previously untested
intermediate fractions.  The selected policy is fixed before any authenticated
bundle is considered; it never enables orders or changes Canary counters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h508_cash_fraction_depth_tradeoff as h508


HYPOTHESIS_ID = "H534_DENSE_FIXED_CASH_FRACTION_GRID"
RESULT_FORMAT = "old-clean-h534-dense-cash-fraction-grid-v1"
SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
DENSE_FRACTIONS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00)


def _select_largest_survivor(rows: list[dict]) -> tuple[float | None, dict | None]:
    """Select the largest fixed fraction that passes both frozen boundaries."""
    candidates = []
    for fraction in DENSE_FRACTIONS:
        policy = h508._fixed_fraction_diagnostic(rows, fraction=fraction)
        if policy["selection_all_ten"] and policy["holdout_all_ten"]:
            candidates.append((fraction, policy))
    return max(candidates, key=lambda item: item[0]) if candidates else (None, None)


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict:
    """Run H508 with the pre-registered dense fraction grid and record H534."""
    h508.GROSS_CASH_FRACTIONS = DENSE_FRACTIONS
    result = h508.evaluate(source_root, source_commit=source_commit, output=output)
    selected_fraction, fixed = _select_largest_survivor(result["window_rows"])
    result.update(
        {
            "format": RESULT_FORMAT,
            "hypothesis_id": HYPOTHESIS_ID,
            "pre_registered_grid": list(DENSE_FRACTIONS),
            "selected_fraction": selected_fraction,
            "dense_fixed_fraction_policy": fixed,
            "selection_rule": (
                "choose the largest fixed fraction whose first eight windows pass all "
                "predeclared latency/depth/slippage rows; evaluate the same fraction on "
                "the independent nine-window holdout"
            ),
            "decision": (
                "h534_largest_dense_fraction_passes_selection_and_holdout"
                if fixed is not None
                else "h534_no_dense_fraction_passes_both_boundaries"
            ),
            "paper_only": True,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
            "live_orders_enabled": False,
            "new_data_collected": False,
        }
    )
    # h508 writes the raw result first; rewrite it after adding the H534 contract.
    summary = output / "summary.json"
    result.pop("summary_sha256", None)
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256(summary.read_bytes()).hexdigest()
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(json.dumps({
        "format": result["format"],
        "selected_fraction": result["selected_fraction"],
        "dense_fixed_fraction_policy": result["dense_fixed_fraction_policy"],
        "decision": result["decision"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

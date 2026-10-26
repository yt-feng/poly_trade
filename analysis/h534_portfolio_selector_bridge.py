"""Compare the frozen H534 0.55 paper profile with the portfolio selector.

This is a preregistered diagnostic on the immutable old-clean source.  H534 is
run through its existing dense-grid implementation and the fixed 0.55 baseline
cell is read back into a comparable selection/holdout summary.  The portfolio
selector is replayed without its unrelated path filter.  Both sides remain
paper-only and retain the one evolving cash ledger, minimum order, fee and
settlement/release contract; the result can reject a selector but cannot create
owner-scoped fill, settlement or Canary evidence.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h404_old_clean_portfolio_path_synergy as h404
import h534_dense_cash_fraction_grid as h534


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
HYPOTHESIS_ID = "H534_PORTFOLIO_SELECTOR_BRIDGE"
RESULT_FORMAT = "old-clean-h534-portfolio-selector-bridge-v1"
FIXED_FRACTION = 0.55
BASE_SLIPPAGE = 0.005
BASE_LATENCY_SECONDS = 4.0
BASE_DEPTH_HAIRCUT = 0.15
SELECTION_WINDOWS = 8
REQUIRED_FILLS = 10
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0


def _close(left: Any, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-12)


def _h534_cell(summary: dict[str, Any]) -> dict[str, Any]:
    rows = [
        row for row in summary.get("window_rows", [])
        if isinstance(row, dict)
        and _close(row.get("gross_cash_fraction"), FIXED_FRACTION)
        and _close(row.get("slippage"), BASE_SLIPPAGE)
        and _close(row.get("latency_seconds"), BASE_LATENCY_SECONDS)
        and _close(row.get("depth_haircut"), BASE_DEPTH_HAIRCUT)
    ]
    if not rows:
        raise ValueError("H534_BASE_CELL_MISSING")
    selection = [row for row in rows if int(row["window_index"]) <= SELECTION_WINDOWS]
    holdout = [row for row in rows if int(row["window_index"]) > SELECTION_WINDOWS]
    if not selection or not holdout:
        raise ValueError("H534_SELECTION_HOLDOUT_MISSING")

    def passing(row: dict[str, Any]) -> bool:
        return bool(
            bool(row.get("first_ten_passed"))
            and int(row.get("first_ten_fills", -1)) == REQUIRED_FILLS
            and float(row.get("first_ten_min_cash_before_entry", -1.0)) >= MINIMUM_ORDER
            and float(row.get("first_ten_ending_cash", -1.0)) >= INITIAL_CASH
        )

    return {
        "profile": "h534_fixed_fraction_055",
        "cell": {
            "slippage": BASE_SLIPPAGE,
            "latency_seconds": BASE_LATENCY_SECONDS,
            "depth_haircut": BASE_DEPTH_HAIRCUT,
        },
        "selection_window_count": len(selection),
        "holdout_window_count": len(holdout),
        "selection_passed_rows": sum(passing(row) for row in selection),
        "holdout_passed_rows": sum(passing(row) for row in holdout),
        "selection_all_ten": bool(selection) and all(passing(row) for row in selection),
        "holdout_all_ten": bool(holdout) and all(passing(row) for row in holdout),
        "selection_min_cash": min(float(row["first_ten_ending_cash"]) for row in selection),
        "holdout_min_cash": min(float(row["first_ten_ending_cash"]) for row in holdout),
        "selection_min_fills": min(int(row["first_ten_fills"]) for row in selection),
        "holdout_min_fills": min(int(row["first_ten_fills"]) for row in holdout),
    }


def _portfolio_without_path_filter(features: pd.DataFrame) -> pd.DataFrame:
    """Build the fixed portfolio selector; do not tune or apply H404's path gate."""
    out = features.copy()
    choices = out.apply(lambda row: h404.qrf.choose_trade(row, h404.STRATEGY), axis=1)
    out[f"{h404.STRATEGY}_side"] = choices.map(lambda item: item[0])
    out[f"{h404.STRATEGY}_minute"] = choices.map(lambda item: item[1]).astype(int)
    out[f"{h404.STRATEGY}_fraction"] = choices.map(lambda item: item[2]).fillna(0.0).astype(float)
    out[f"{h404.STRATEGY}_component"] = choices.map(lambda item: item[3])
    return out


def _portfolio_rows(frame: pd.DataFrame, runs: list[str]) -> list[dict[str, Any]]:
    test_runs = runs[4:]
    selection_runs = test_runs[: h404.SELECTION_RUNS]
    holdout_runs = test_runs[h404.SELECTION_RUNS :]
    blocks: list[list[str]] = []
    cursor = 0
    for size in h404.BLOCK_SIZES:
        blocks.append(holdout_runs[cursor : cursor + size])
        cursor += size
    if cursor != len(holdout_runs):
        raise ValueError("PORTFOLIO_BLOCKS_DO_NOT_COVER_HOLDOUT")
    rows = [
        h404._replay(frame, selection_runs, "selection20"),
        h404._replay(frame, holdout_runs, "holdout59"),
    ]
    rows.extend(h404._replay(frame, names, f"block{index}") for index, names in enumerate(blocks, 1))
    return rows


def _portfolio_gate(rows: list[dict[str, Any]]) -> bool:
    if len(rows) != 7:
        return False
    selection, holdout, *blocks = rows
    def ok(row: dict[str, Any]) -> bool:
        return bool(
            row.get("paper_chain_passed")
            and int(row.get("paper_chain_fills", 0)) == REQUIRED_FILLS
            and float(row.get("paper_chain_minimum_cash_before_entry", -1.0)) >= MINIMUM_ORDER
            and float(row.get("paper_chain_ending_cash", -1.0)) >= INITIAL_CASH
        )
    return ok(selection) and ok(holdout) and bool(blocks) and all(ok(row) for row in blocks)


def evaluate(source_root: Path, output: Path, *, source_commit: str = SOURCE_COMMIT) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")

    h534_output = output / "h534-parent"
    h534_output.mkdir(parents=True, exist_ok=True)
    h534.evaluate(source_root, source_commit=source_commit, output=h534_output)
    h534_summary = json.loads((h534_output / "summary.json").read_text(encoding="utf-8"))
    h534_cell = _h534_cell(h534_summary)

    features, coverage, runs = h404._prepare_lightweight(source_root, strict_quality=True)
    portfolio_frame = _portfolio_without_path_filter(features)
    portfolio_rows = _portfolio_rows(portfolio_frame, runs)
    portfolio_gate = _portfolio_gate(portfolio_rows)
    h534_gate = bool(h534_cell["selection_all_ten"] and h534_cell["holdout_all_ten"])
    if h534_gate and not portfolio_gate:
        decision = "retain_h534_fixed_055_reject_portfolio_selector"
    elif portfolio_gate and not h534_gate:
        decision = "retain_portfolio_selector_reject_h534_fixed_055"
    elif h534_gate and portfolio_gate:
        decision = "both_paper_profiles_survive_owner_receipts_required"
    else:
        decision = "neither_profile_passes_same_ledger_paper_gate"

    result: dict[str, Any] = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "h534_parent_format": h534_summary.get("format"),
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "comparison": {
            "h534_fixed_055": h534_cell,
            "portfolio_conservative_v2": {
                "selection": portfolio_rows[0],
                "holdout": portfolio_rows[1],
                "blocks": portfolio_rows[2:],
                "paper_gate": portfolio_gate,
            },
        },
        "selection_rule": "H534 fixed 0.55 and portfolio_conservative_v2 are compared without threshold tuning or model-weight changes; one same-ledger ten-fill paper gate is required before owner evidence is considered.",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_usd": MINIMUM_ORDER,
            "minimum_order_shares": 5.0,
            "taker_fee_rate": 0.07,
            "settlement_seconds": 300.0,
            "cash_release_after_settlement_seconds": 600.0,
            "same_evolving_ledger": True,
            "no_cross_window_concatenation": True,
            "required_consecutive_fills": REQUIRED_FILLS,
            "paper_only": True,
        },
        "decision": decision,
        "root_cause_scope": "This bridge tests selector/ledger compatibility only. It cannot establish owner-scoped OrderFilled, settlement, released-cash, or balance continuity.",
        "new_data_collected": False,
        "model_weights_changed": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "canary_gate_passed": False,
        "owner_scoped_evidence_required": True,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    coverage.to_csv(output / "coverage.csv", index=False)
    pd.DataFrame(portfolio_rows).to_csv(output / "portfolio_metrics.csv", index=False)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, args.output, source_commit=args.source_commit)
    print(json.dumps({key: result[key] for key in ("hypothesis_id", "decision", "actual_fills", "verified_canary_fills")}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

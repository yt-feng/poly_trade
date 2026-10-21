"""H441: repair the H440 ten-fill compound-chain contract.

H440 reused the frozen H434 model and ledger timing, but its replay helper
treated any settled loss as a terminal chain failure and required every one
of the first ten fills to win. The operating contract requires one evolving
cash ledger: a settled loss reduces cash to zero payout, while the chain may
continue if the next minimum order is fundable. H441 keeps the H434 model,
features, threshold, fees, slippage cells and old-clean source fixed and only
repairs that reproduced execution-contract defect. It remains paper-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    CASH_RELEASE_SECONDS,
    INITIAL_CASH,
    MINIMUM_ORDER_USD,
    REQUIRED_FILLS,
    SLIPPAGES,
    _candidate_frame,
    _fit,
)
from h434_old_clean_extratrees_high_confidence import (
    MODEL_CONFIDENCE_FLOOR,
    apply_confidence_gate,
)
from h440_h434_rolling_stability import rolling_windows


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
WINDOW_RUNS = 12
WINDOW_STRIDE = 4
ROBUST_WINDOW_FRACTION = 0.80


def replay_chain(candidates: pd.DataFrame, run_names: list[str]) -> dict[str, Any]:
    """Replay one single-position, evolving-cash chain where losses continue."""
    if candidates.empty:
        return {
            "candidate_rows": 0, "fills": 0, "blocked_before_release": 0,
            "ending_cash": INITIAL_CASH, "first_ten_fills": 0,
            "first_ten_ending_cash": INITIAL_CASH, "first_ten_wins": 0,
            "first_ten_losses": 0, "first_ten_min_cash_before_entry": INITIAL_CASH,
            "first_ten_passed": False, "failure_reason": "no_candidates",
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = INITIAL_CASH
    previous: dict[str, Any] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = row.entry_ts.to_pydatetime()
        if previous is not None:
            if entry <= previous["cash_release_ts"]:
                blocked_before_release += 1
                continue
            cash += previous["payout"]
            previous = None
        total = float(row.cost + row.fee)
        if total > cash + 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        before = cash
        cash -= total
        win = bool(
            (float(row.outcome_up) == 1.0)
            if row.side == "up"
            else (float(row.outcome_up) == 0.0)
        )
        payout = float(row.shares) if win else 0.0
        previous = {"cash_release_ts": row.cash_release_ts.to_pydatetime(), "payout": payout}
        chain.append({
            "slug": str(row.slug), "run_name": str(row.run_name),
            "entry_ts": row.entry_ts.isoformat(), "cash_release_ts": row.cash_release_ts.isoformat(),
            "side": str(row.side), "q_side": float(row.q_side), "price": float(row.price),
            "fill_price": float(row.fill_price), "cost": total, "fee": float(row.fee),
            "win": win, "cash_before_entry": before, "cash_after_entry": cash,
        })
        if len(chain) >= REQUIRED_FILLS:
            break
    if previous is not None:
        cash += previous["payout"]
    prefix = chain[:REQUIRED_FILLS]
    prefix_ending_cash = float(cash) if len(prefix) == REQUIRED_FILLS else float(
        prefix[-1]["cash_after_entry"] if prefix else INITIAL_CASH
    )
    minimum_cash = float(min([INITIAL_CASH] + [item["cash_before_entry"] for item in prefix]))
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and minimum_cash >= MINIMUM_ORDER_USD
        and prefix_ending_cash >= INITIAL_CASH
    )
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)), "fills": int(len(chain)),
        "blocked_before_release": int(blocked_before_release), "ending_cash": float(cash),
        "first_ten_fills": int(len(prefix)), "first_ten_ending_cash": prefix_ending_cash,
        "first_ten_wins": int(sum(item["win"] for item in prefix)),
        "first_ten_losses": int(sum(not item["win"] for item in prefix)),
        "first_ten_min_cash_before_entry": minimum_cash, "first_ten_passed": passed,
        "failure_reason": failure_reason, "chain": prefix,
    }


def _strip_chain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_chain(item) for key, item in value.items() if key != "chain"}
    if isinstance(value, list):
        return [_strip_chain(item) for item in value]
    return value


def _cell_summary(rows: list[dict[str, Any]], slippage: float) -> dict[str, Any]:
    selected = [row for row in rows if float(row["slippage"]) == float(slippage)]
    passed = [row for row in selected if bool(row["first_ten_passed"])]
    return {
        "candidate": "H434_EXTRATREES_FIXED_HIGH_CONFIDENCE_GATE", "slippage": float(slippage),
        "window_count": len(selected), "growth_positive_windows": len(passed),
        "growth_window_fraction": len(passed) / len(selected) if selected else 0.0,
        "robust_window_gate": bool(selected) and len(passed) / len(selected) >= ROBUST_WINDOW_FRACTION,
        "all_windows_passed": bool(selected) and len(passed) == len(selected),
        "minimum_first_ten_cash_usd": min((float(row["first_ten_ending_cash"]) for row in selected), default=INITIAL_CASH),
        "median_first_ten_cash_usd": float(pd.Series([float(row["first_ten_ending_cash"]) for row in selected]).median()) if selected else INITIAL_CASH,
        "minimum_first_ten_fills": min((int(row["first_ten_fills"]) for row in selected), default=0),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
    candidates_by_slippage: dict[float, list[pd.DataFrame]] = {float(s): [] for s in SLIPPAGES}
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = _fit(train)
        if model is None:
            continue
        for slippage in SLIPPAGES:
            current = apply_confidence_gate(_candidate_frame(model, medians, by_run[run_name], float(slippage)))
            if not current.empty:
                current = current.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)
    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    for window in windows:
        for slippage in SLIPPAGES:
            frames = candidates_by_slippage[float(slippage)]
            candidates = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            rows.append({
                "window_index": window["window_index"], "start_run": window["start_run"],
                "end_run": window["end_run"], "split": "h441_h434_loss_tolerant_rolling_window",
                "slippage": float(slippage), **_strip_chain(replay_chain(candidates, window["runs"])),
            })
    cells = [_cell_summary(rows, float(slippage)) for slippage in SLIPPAGES]
    all_cells_robust = bool(cells) and all(bool(cell["robust_window_gate"]) for cell in cells)
    result: dict[str, Any] = {
        "format": "old-clean-h441-h434-loss-tolerant-compound-v1",
        "hypothesis_id": "H441_H434_LOSS_TOLERANT_COMPOUND_REPLAY",
        "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed,
        "strict_quality": True, "complete_runs": len(runs), "feature_rows": int(len(features)),
        "candidate": "H434_EXTRATREES_FIXED_HIGH_CONFIDENCE_GATE",
        "selection_rule": {"confidence_floor": MODEL_CONFIDENCE_FLOOR, "single_fixed_gate": True, "threshold_grid": False, "model_fit": "prequential; each scored run uses only earlier runs"},
        "window_count": len(windows), "window_runs": WINDOW_RUNS, "window_stride": WINDOW_STRIDE,
        "robust_window_gate": f"at least {ROBUST_WINDOW_FRACTION:.0%} of windows pass ten funded fills with non-negative net growth in every slippage cell",
        "cell_summaries": cells, "all_cells_robust": all_cells_robust, "window_rows": rows,
        "decision": "paper_stability_supports_contract_repair_receipts_required" if all_cells_robust else "reject_h441_loss_tolerant_compound_stability",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH, "required_fills": REQUIRED_FILLS, "minimum_order_usd": 5.0,
            "minimum_order_shares": 5.0, "taker_fee_rate": 0.07, "cash_release_delay_seconds": CASH_RELEASE_SECONDS,
            "slippages": list(SLIPPAGES), "same_ledger_within_window": True,
            "settled_loss_continues_if_next_order_is_fundable": True,
            "first_ten_pass_requires_net_growth_not_all_wins": True,
            "fresh_diagnostic_ledger_per_window": True, "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True, "paper_only": True,
        },
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": False, "model_refit": True, "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean rolling paper diagnostic; windows are not Canary fills and no authenticated owner, settlement or cash-release receipt exists",
        "root_cause": "H440 replay ended a chain on any settled loss and required all first-ten fills to win, contrary to the continuous ledger contract",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(pd.DataFrame(rows).to_csv(index=False), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"window_count": len(windows), "cell_summaries": cells, "decision": result["decision"]}, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()

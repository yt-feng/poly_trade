"""H508: cash fraction versus execution-depth stress on the frozen old-clean signal.

H434/H452 entry features, fee arithmetic, 900-second cash release, minimum
order floors and the single chronological ledger stay fixed.  This audit
changes only gross cash fraction together with the already predeclared H506
latency/depth stress.  It uses the first eight rolling windows to choose a
fraction and evaluates that choice on the remaining nine windows.  It is a
paper execution diagnostic; it does not infer authenticated fills or place
orders.
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

import h506_h452_execution_depth_stress as depth
import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    CASH_RELEASE_SECONDS,
    INITIAL_CASH,
    MINIMUM_ORDER_SHARES,
    MINIMUM_ORDER_USD,
    SLIPPAGES,
    TAKER_FEE_RATE,
    _candidate_frame,
    _fit,
)
from h434_old_clean_extratrees_high_confidence import apply_confidence_gate
from h440_h434_rolling_stability import rolling_windows
from h447_old_clean_breakeven_exit import (
    EXPECTED_COMPLETE_RUNS,
    SOURCE_COMMIT,
    WINDOW_RUNS,
    WINDOW_STRIDE,
    _quote_index,
)
from h452_old_clean_cash_fraction import GROSS_CASH_FRACTIONS as H452_FRACTIONS
from h452_old_clean_cash_fraction import _size_from_cash


HYPOTHESIS_ID = "H508_CASH_FRACTION_DEPTH_TRADEOFF"
RESULT_FORMAT = "old-clean-h508-cash-fraction-depth-tradeoff-v1"
GROSS_CASH_FRACTIONS = tuple(float(value) for value in (*H452_FRACTIONS, 0.95, 1.0))
LATENCIES_SECONDS = (2.0, 4.0, 8.0)
DEPTH_HAIRCUTS = (0.10, 0.15, 0.20, 0.25)
SELECTION_WINDOWS = 8
REQUIRED_FILLS = 10


def _is_holdout_window(window_index: int) -> bool:
    """Keep the first eight windows in selection and exclude them from holdout."""
    return int(window_index) > SELECTION_WINDOWS


def _validate_fraction(value: float) -> None:
    if not 0.0 < float(value) <= 1.0:
        raise ValueError("gross_cash_fraction must be in (0, 1]")


def _replay_fraction_depth(
    candidates: pd.DataFrame,
    run_names: list[str],
    quotes: dict[str, pd.DataFrame],
    *,
    gross_cash_fraction: float,
    latency_seconds: float,
    depth_haircut: float,
    exit_cache: dict[tuple[Any, ...], dict[str, Any] | None],
) -> dict[str, Any]:
    """Replay one no-reset chain using fractional cash and stressed exits."""
    _validate_fraction(gross_cash_fraction)
    depth._validate_stress(latency_seconds, depth_haircut)
    if candidates.empty:
        return {
            "candidate_rows": 0,
            "fills": 0,
            "blocked_before_release": 0,
            "ending_cash": INITIAL_CASH,
            "first_ten_fills": 0,
            "first_ten_ending_cash": INITIAL_CASH,
            "first_ten_min_cash_before_entry": INITIAL_CASH,
            "first_ten_passed": False,
            "failure_reason": "no_candidates",
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = float(INITIAL_CASH)
    pending: tuple[pd.Timestamp, float] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = pd.Timestamp(row.entry_ts)
        if pending is not None:
            if entry <= pending[0]:
                blocked_before_release += 1
                continue
            cash += pending[1]
            pending = None
        if cash < MINIMUM_ORDER_USD - 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        terms = _size_from_cash(row, cash, gross_cash_fraction)
        if terms is None:
            failure_reason = "cash_fraction_cannot_fit_minimum_order"
            break
        key = (
            str(row.slug),
            str(row.side),
            round(float(terms["shares"]), 8),
            float(row.slippage),
            float(latency_seconds),
            float(depth_haircut),
        )
        if key not in exit_cache:
            exit_cache[key] = depth._first_stressed_exit(
                row,
                quotes.get(str(row.slug)),
                shares=float(terms["shares"]),
                entry_outlay=float(terms["reserved"]),
                latency_seconds=latency_seconds,
                depth_haircut=depth_haircut,
            )
        exit_event = exit_cache[key]
        if exit_event is None:
            failure_reason = "no_full_size_break_even_exit_under_latency_depth_stress"
            break
        before = cash
        cash -= float(terms["reserved"])
        pending = (pd.Timestamp(row.cash_release_ts), float(exit_event["exit_payout"]))
        chain.append(
            {
                "run_name": str(row.run_name),
                "entry_ts": entry.isoformat(),
                "cash_release_ts": pd.Timestamp(row.cash_release_ts).isoformat(),
                "shares": float(terms["shares"]),
                "cost": float(terms["cost"]),
                "fee": float(terms["fee"]),
                "payout": float(exit_event["exit_payout"]),
                "exit_ts": str(exit_event["exit_ts"]),
                "cash_before_entry": before,
                "cash_after_entry": cash,
            }
        )
        if len(chain) >= REQUIRED_FILLS:
            break
    if pending is not None:
        cash += pending[1]
    prefix = chain[:REQUIRED_FILLS]
    ending = float(cash) if len(prefix) == REQUIRED_FILLS else float(
        prefix[-1]["cash_after_entry"] if prefix else INITIAL_CASH
    )
    minimum_cash = min(
        [INITIAL_CASH] + [float(item["cash_before_entry"]) for item in prefix]
    )
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and minimum_cash >= MINIMUM_ORDER_USD
        and ending >= INITIAL_CASH
    )
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)),
        "fills": int(len(chain)),
        "blocked_before_release": int(blocked_before_release),
        "ending_cash": ending,
        "first_ten_fills": int(len(prefix)),
        "first_ten_ending_cash": ending,
        "first_ten_min_cash_before_entry": float(minimum_cash),
        "first_ten_passed": passed,
        "failure_reason": failure_reason,
    }


def _choose_fraction(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Predeclared selection: all ten-fill cells, then max minimum cash."""
    grouped: dict[float, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(float(row["gross_cash_fraction"]), []).append(row)
    eligible = []
    for fraction, values in grouped.items():
        if not values or not all(bool(item["first_ten_passed"]) for item in values):
            continue
        eligible.append(
            {
                "gross_cash_fraction": fraction,
                "selection_min_cash": min(float(item["first_ten_ending_cash"]) for item in values),
                "selection_median_cash": float(pd.Series([float(item["first_ten_ending_cash"]) for item in values]).median()),
            }
        )
    return max(eligible, key=lambda item: (item["selection_min_cash"], item["selection_median_cash"], item["gross_cash_fraction"])) if eligible else None


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
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
            candidate = apply_confidence_gate(
                _candidate_frame(model, medians, by_run[run_name], float(slippage))
            )
            if not candidate.empty:
                current = candidate.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)
    all_candidates = pd.concat(
        [frame for frames in candidates_by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = _quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for slippage in SLIPPAGES:
        base = pd.concat(candidates_by_slippage[float(slippage)], ignore_index=True)
        for latency in LATENCIES_SECONDS:
            for haircut in DEPTH_HAIRCUTS:
                selected_rows: list[dict[str, Any]] = []
                for fraction in GROSS_CASH_FRACTIONS:
                    cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                    fraction_rows = []
                    for window in windows:
                        result = _replay_fraction_depth(
                            base,
                            window["runs"],
                            quotes,
                            gross_cash_fraction=float(fraction),
                            latency_seconds=float(latency),
                            depth_haircut=float(haircut),
                            exit_cache=cache,
                        )
                        item = {
                            "slippage": float(slippage),
                            "latency_seconds": float(latency),
                            "depth_haircut": float(haircut),
                            "gross_cash_fraction": float(fraction),
                            "window_index": window["window_index"],
                            "start_run": window["start_run"],
                            "end_run": window["end_run"],
                            **result,
                        }
                        rows.append(item)
                        fraction_rows.append(item)
                    if fraction_rows:
                        selected_rows.extend(fraction_rows[:SELECTION_WINDOWS])
                chosen = _choose_fraction(selected_rows)
                holdout_rows = [
                    row for row in rows
                    if row["slippage"] == float(slippage)
                    and row["latency_seconds"] == float(latency)
                    and row["depth_haircut"] == float(haircut)
                    and chosen is not None
                    and row["gross_cash_fraction"] == float(chosen["gross_cash_fraction"])
                    and _is_holdout_window(row["window_index"])
                ]
                cells.append(
                    {
                        "slippage": float(slippage),
                        "latency_seconds": float(latency),
                        "depth_haircut": float(haircut),
                        "selection_window_count": SELECTION_WINDOWS,
                        "holdout_window_count": len(windows) - SELECTION_WINDOWS,
                        "selected_fraction": chosen["gross_cash_fraction"] if chosen else None,
                        "selection_passed": chosen is not None,
                        "holdout_all_ten": bool(holdout_rows) and all(bool(row["first_ten_passed"]) for row in holdout_rows),
                        "holdout_growth_fraction": (
                            sum(bool(row["first_ten_passed"]) for row in holdout_rows) / len(holdout_rows)
                            if holdout_rows else 0.0
                        ),
                        "holdout_min_fills": min((int(row["first_ten_fills"]) for row in holdout_rows), default=0),
                        "holdout_min_cash": min((float(row["first_ten_ending_cash"]) for row in holdout_rows), default=INITIAL_CASH),
                    }
                )
    result: dict[str, Any] = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "candidate": "H434_H452_FIXED_SIGNAL_CASH_FRACTION_UNDER_H506_STRESS",
        "gross_cash_fractions": list(GROSS_CASH_FRACTIONS),
        "latencies_seconds": list(LATENCIES_SECONDS),
        "depth_haircuts": list(DEPTH_HAIRCUTS),
        "selection_windows": SELECTION_WINDOWS,
        "window_count": len(windows),
        "cell_summaries": cells,
        "holdout_gate_passed": bool(cells) and all(bool(cell["holdout_all_ten"]) for cell in cells),
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_usd": MINIMUM_ORDER_USD,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_SECONDS,
            "same_ledger_within_window": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "new_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "strict-quality old-clean paper replay; stressed displayed depth is not authenticated execution or reusable cash",
        "root_cause": "tests whether reducing gross cash per entry can preserve a complete ten-fill compounding chain when latency and exit depth make full-cash positions too large",
        "window_rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(pd.DataFrame(rows).to_csv(index=False), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cell_summaries": cells, "holdout_gate_passed": result["holdout_gate_passed"]}, ensure_ascii=False, sort_keys=True))
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

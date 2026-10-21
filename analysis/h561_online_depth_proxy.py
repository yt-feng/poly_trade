"""H561: use entry-visible sell depth to cap the next cash fraction.

H508 showed that a full-cash position can break the same-ledger ten-fill chain
when effective exit depth is severely reduced.  H561 keeps the frozen H434
signal, fee model, release clock and causal exit replay, but maps the sell-side
depth visible in the first two minutes to a predeclared gross cash fraction.
The rule uses no post-entry quote or outcome and remains paper evidence only.
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

import h508_cash_fraction_depth_tradeoff as h508
import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    INITIAL_CASH,
    MINIMUM_ORDER_USD,
    SLIPPAGES,
    SOURCE_COMMIT,
    _candidate_frame,
    _fit,
)
from h434_old_clean_extratrees_high_confidence import apply_confidence_gate
from h440_h434_rolling_stability import rolling_windows
from h447_old_clean_breakeven_exit import (
    EXPECTED_COMPLETE_RUNS,
    WINDOW_RUNS,
    WINDOW_STRIDE,
    _quote_index,
)


HYPOTHESIS_ID = "H561_ONLINE_ENTRY_DEPTH_PROXY_SIZING"
RESULT_FORMAT = "old-clean-h561-online-depth-proxy-variant-audit-v1"
LATENCIES_SECONDS = (2.0, 4.0, 8.0)
DEPTH_HAIRCUTS = (0.10, 0.15, 0.20, 0.25)
REQUIRED_FILLS = 10
SELECTION_WINDOWS = 8
THRESHOLD_VARIANTS: dict[str, tuple[float, float, float]] = {
    "current_2_3_4": (2.0, 3.0, 4.0),
    "conservative_4_6_8": (4.0, 6.0, 8.0),
    "conservative_5_8_12": (5.0, 8.0, 12.0),
    "conservative_6_10_16": (6.0, 10.0, 16.0),
}


def depth_proxy_fraction(
    row: Any,
    cash: float,
    thresholds: tuple[float, float, float],
) -> tuple[float, float]:
    """Return the fraction and capacity ratio from entry-time sell depth."""
    if len(thresholds) != 3 or not 0 < thresholds[0] < thresholds[1] < thresholds[2]:
        raise ValueError("depth thresholds must be strictly increasing")
    full = h508._size_from_cash(row, cash, 1.0)
    if full is None:
        return 0.50, 0.0
    side = str(row.side)
    depth = float(getattr(row, "sell_up_size_2m" if side == "up" else "sell_down_size_2m"))
    ratio = depth / float(full["shares"]) if float(full["shares"]) > 0 else 0.0
    if ratio >= thresholds[2]:
        return 1.00, ratio
    if ratio >= thresholds[1]:
        return 0.90, ratio
    if ratio >= thresholds[0]:
        return 0.75, ratio
    return 0.50, ratio


def replay(
    candidates: pd.DataFrame,
    run_names: list[str],
    quotes: dict[str, pd.DataFrame],
    *,
    latency_seconds: float,
    depth_haircut: float,
    exit_cache: dict[tuple[Any, ...], dict[str, Any] | None],
    thresholds: tuple[float, float, float],
) -> dict[str, Any]:
    """Replay one chronological chain without resetting the cash ledger."""
    if candidates.empty:
        return {
            "fills": 0,
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
        fraction, ratio = depth_proxy_fraction(row, cash, thresholds)
        terms = h508._size_from_cash(row, cash, fraction)
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
            exit_cache[key] = h508.depth._first_stressed_exit(
                row,
                quotes.get(str(row.slug)),
                shares=float(terms["shares"]),
                entry_outlay=float(terms["reserved"]),
                latency_seconds=float(latency_seconds),
                depth_haircut=float(depth_haircut),
            )
        exit_event = exit_cache[key]
        if exit_event is None:
            failure_reason = "no_full_size_break_even_exit_under_latency_depth_stress"
            break
        before = cash
        cash -= float(terms["reserved"])
        payout = float(exit_event["exit_payout"])
        pending = (pd.Timestamp(row.cash_release_ts), payout)
        chain.append(
            {
                "run_name": str(row.run_name),
                "slug": str(row.slug),
                "fraction": fraction,
                "depth_capacity_ratio": ratio,
                "entry_ts": entry.isoformat(),
                "exit_ts": str(exit_event["exit_ts"]),
                "cash_before_entry": before,
                "cash_after_entry": cash,
                "payout": payout,
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
    minimum_cash = min([INITIAL_CASH] + [float(item["cash_before_entry"]) for item in prefix])
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and minimum_cash >= MINIMUM_ORDER_USD
        and ending >= INITIAL_CASH
    )
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "fills": len(chain),
        "blocked_before_release": blocked_before_release,
        "ending_cash": ending,
        "first_ten_fills": len(prefix),
        "first_ten_ending_cash": ending,
        "first_ten_min_cash_before_entry": minimum_cash,
        "first_ten_passed": passed,
        "failure_reason": failure_reason,
        "chain": prefix,
    }


def _candidate_frames(source_root: Path) -> tuple[dict[float, list[pd.DataFrame]], list[str], int]:
    features, _, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
    by_slippage: dict[float, list[pd.DataFrame]] = {float(s): [] for s in SLIPPAGES}
    for index, name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = _fit(train)
        if model is None:
            continue
        for slippage in SLIPPAGES:
            frame = apply_confidence_gate(_candidate_frame(model, medians, by_run[name], float(slippage)))
            if not frame.empty:
                frame = frame.copy()
                frame["model_run_index"] = index
                by_slippage[float(slippage)].append(frame)
    return by_slippage, runs, int(len(features))


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    by_slippage, runs, feature_rows = _candidate_frames(source_root)
    all_candidates = pd.concat([frame for frames in by_slippage.values() for frame in frames], ignore_index=True)
    quotes = _quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    summaries: dict[str, dict[str, Any]] = {}
    for variant, thresholds in THRESHOLD_VARIANTS.items():
        rows: list[dict[str, Any]] = []
        for slippage in SLIPPAGES:
            base = pd.concat(by_slippage[float(slippage)], ignore_index=True)
            for latency in LATENCIES_SECONDS:
                for haircut in DEPTH_HAIRCUTS:
                    cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                    for window in windows:
                        result = replay(
                            base,
                            window["runs"],
                            quotes,
                            latency_seconds=latency,
                            depth_haircut=haircut,
                            exit_cache=cache,
                            thresholds=thresholds,
                        )
                        rows.append(
                            {
                                "variant": variant,
                                "slippage": float(slippage),
                                "latency_seconds": float(latency),
                                "depth_haircut": float(haircut),
                                "window_index": int(window["window_index"]),
                                **{key: value for key, value in result.items() if key != "chain"},
                            }
                        )
        frame = pd.DataFrame(rows)
        selection = frame[frame["window_index"] <= SELECTION_WINDOWS]
        holdout = frame[frame["window_index"] > SELECTION_WINDOWS]
        selection_cells = selection.groupby(["slippage", "latency_seconds", "depth_haircut"])
        holdout_cells = holdout.groupby(["slippage", "latency_seconds", "depth_haircut"])
        summaries[variant] = {
            "thresholds": list(thresholds),
            "selection_all_ten": all(bool(group["first_ten_passed"].all()) for _, group in selection_cells),
            "holdout_all_ten": all(bool(group["first_ten_passed"].all()) for _, group in holdout_cells),
            "selection_min_cash": float(selection["first_ten_ending_cash"].min()),
            "holdout_min_cash": float(holdout["first_ten_ending_cash"].min()),
            "holdout_median_cash": float(holdout["first_ten_ending_cash"].median()),
            "overall_pass_rate": float(frame["first_ten_passed"].mean()),
            "overall_min_fills": int(frame["first_ten_fills"].min()),
        }
        output.mkdir(parents=True, exist_ok=True)
        frame.to_csv(output / f"{variant}-rolling-metrics.csv", index=False)
    result = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": feature_rows,
        "variants": summaries,
        "selection_rule": "retain only a rule with all 24 stress cells passing all 8 selection windows; compare holdout only after selection",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "same_ledger": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "entry_depth_source": "sell_up_size_2m or sell_down_size_2m",
            "paper_only": True,
        },
        "new_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "evidence_boundary": "strict-quality old-clean quote replay; entry-visible depth is observed but execution, settlement and reusable cash remain paper-only",
    }
    output.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode() + b"\n"
    result["summary_sha256"] = hashlib.sha256(payload).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(json.dumps(result["variants"], sort_keys=True))


if __name__ == "__main__":
    main()

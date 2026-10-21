"""H567: combine entry-depth sizing with a released-profit cash boost.

H434 supplies the frozen causal signal.  H561 uses entry-visible sell depth to
cap the gross cash fraction, while H549 raises only the low-depth floor after a
strictly profitable *released* exit.  The combination is pre-registered as a
single rule: ``max(depth_proxy_fraction, released_profit_fraction)``.  It is
tested on the pinned strict-quality old-clean archive with the same one-ledger
cash, fee, release, latency and depth contract.  This is paper evidence only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent

import h508_cash_fraction_depth_tradeoff as h508
import h561_online_depth_proxy as h561
from h542_adaptive_cash_boost import _fraction_for_state


HYPOTHESIS_ID = "H567_H561_DEPTH_PROXY_H549_RELEASED_PROFIT_SYNERGY"
RESULT_FORMAT = "old-clean-h567-depth-profit-synergy-v1"
SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
THRESHOLDS = (4.0, 6.0, 8.0)
BASE_FRACTION = 0.55
BOOST_FRACTION = 0.5875
BOOST_AFTER_PROFITS = 1
REQUIRED_FILLS = 10
SELECTION_WINDOWS = 8
LATENCIES_SECONDS = (2.0, 4.0, 8.0)
DEPTH_HAIRCUTS = (0.10, 0.15, 0.20, 0.25)

# H561's sealed old-clean artifact is the non-degradation reference.  These
# values are source-bound and are not tuned by this run.
H561_BASELINE = {
    "selection_min_cash": 12.666096767044696,
    "holdout_min_cash": 12.117983426626632,
    "holdout_median_cash": 14.238209762102585,
    "summary_sha256": "55c1c1338daa9bc0e7405332cb5a5691c4954779ef257b798cd87648bd71932a",
}


def combined_fraction(
    row: Any,
    cash: float,
    consecutive_released_profits: int,
    *,
    thresholds: tuple[float, float, float] = THRESHOLDS,
    base_fraction: float = BASE_FRACTION,
    boost_fraction: float = BOOST_FRACTION,
    boost_after_profits: int = BOOST_AFTER_PROFITS,
) -> tuple[float, float, float, float]:
    """Return (chosen, depth, released-profit, depth/cash ratio)."""
    depth_fraction, ratio = h561.depth_proxy_fraction(row, cash, thresholds)
    profit_fraction = _fraction_for_state(
        consecutive_released_profits,
        base_fraction=base_fraction,
        boost_fraction=boost_fraction,
        boost_after_profits=boost_after_profits,
    )
    return max(float(depth_fraction), float(profit_fraction)), float(depth_fraction), float(profit_fraction), float(ratio)


def replay(
    candidates: pd.DataFrame,
    run_names: list[str],
    quotes: dict[str, pd.DataFrame],
    *,
    latency_seconds: float,
    depth_haircut: float,
    exit_cache: dict[tuple[Any, ...], dict[str, Any] | None],
) -> dict[str, Any]:
    """Replay one chronological chain; profit state changes only on release."""
    if candidates.empty:
        return {
            "fills": 0,
            "blocked_before_release": 0,
            "ending_cash": h561.INITIAL_CASH,
            "first_ten_fills": 0,
            "first_ten_ending_cash": h561.INITIAL_CASH,
            "first_ten_min_cash_before_entry": h561.INITIAL_CASH,
            "first_ten_passed": False,
            "failure_reason": "no_candidates",
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = float(h561.INITIAL_CASH)
    pending: tuple[pd.Timestamp, float, bool] | None = None
    consecutive_released_profits = 0
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = pd.Timestamp(row.entry_ts)
        if pending is not None:
            release_ts, payout, profitable = pending
            if entry <= release_ts:
                blocked_before_release += 1
                continue
            cash += payout
            pending = None
            consecutive_released_profits = consecutive_released_profits + 1 if profitable else 0
        if cash < h561.MINIMUM_ORDER_USD - 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        fraction, depth_fraction, profit_fraction, ratio = combined_fraction(
            row, cash, consecutive_released_profits
        )
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
        profitable = payout > float(terms["reserved"]) + 1e-9
        pending = (pd.Timestamp(row.cash_release_ts), payout, profitable)
        chain.append(
            {
                "run_name": str(row.run_name),
                "slug": str(row.slug),
                "entry_ts": entry.isoformat(),
                "fraction": fraction,
                "depth_fraction": depth_fraction,
                "released_profit_fraction": profit_fraction,
                "depth_capacity_ratio": ratio,
                "cash_before_entry": before,
                "cash_after_entry": cash,
                "payout": payout,
                "strictly_profitable_exit": profitable,
                "cash_release_ts": pd.Timestamp(row.cash_release_ts).isoformat(),
                "exit_ts": str(exit_event["exit_ts"]),
            }
        )
        if len(chain) >= REQUIRED_FILLS:
            break
    if pending is not None:
        cash += pending[1]
    prefix = chain[:REQUIRED_FILLS]
    ending = float(cash) if len(prefix) == REQUIRED_FILLS else float(
        prefix[-1]["cash_after_entry"] if prefix else h561.INITIAL_CASH
    )
    minimum_cash = min([h561.INITIAL_CASH] + [float(item["cash_before_entry"]) for item in prefix])
    passed = bool(
        len(prefix) == REQUIRED_FILLS
        and minimum_cash >= h561.MINIMUM_ORDER_USD
        and ending >= h561.INITIAL_CASH
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


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    by_slippage, runs, feature_rows = h561._candidate_frames(source_root)
    all_candidates = pd.concat(
        [frame for frames in by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = h561._quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = h561.rolling_windows(runs[4:], window_runs=h561.WINDOW_RUNS, stride=h561.WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    for slippage in h561.SLIPPAGES:
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
                    )
                    rows.append(
                        {
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
    report = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": feature_rows,
        "rule": {
            "signal": "frozen H434 high-confidence ExtraTrees",
            "depth_thresholds": list(THRESHOLDS),
            "depth_fraction_source": "entry sell_up_size_2m or sell_down_size_2m",
            "released_profit_base_fraction": BASE_FRACTION,
            "released_profit_boost_fraction": BOOST_FRACTION,
            "boost_after_strictly_profitable_released_exits": BOOST_AFTER_PROFITS,
            "combination": "max(depth_proxy_fraction, released_profit_fraction)",
            "profit_state_updates_only_after_cash_release": True,
        },
        "protocol": {
            "initial_cash_usd": h561.INITIAL_CASH,
            "required_consecutive_fills": REQUIRED_FILLS,
            "single_evolving_cash_ledger": True,
            "no_reset": True,
            "no_cross_window_concatenation": True,
            "taker_fee_rate": h561.h508.TAKER_FEE_RATE,
            "cash_release_delay_seconds": h561.h508.CASH_RELEASE_SECONDS,
            "paper_only": True,
        },
        "selection": {
            "rows": int(len(selection)),
            "all_rows_passed": bool(selection["first_ten_passed"].all()),
            "min_cash": float(selection["first_ten_ending_cash"].min()),
        },
        "holdout": {
            "rows": int(len(holdout)),
            "all_rows_passed": bool(holdout["first_ten_passed"].all()),
            "min_cash": float(holdout["first_ten_ending_cash"].min()),
            "median_cash": float(holdout["first_ten_ending_cash"].median()),
        },
        "non_degradation_reference": H561_BASELINE,
        "non_degradation_gate": {
            "holdout_min_cash_at_least_h561": float(holdout["first_ten_ending_cash"].min()) >= H561_BASELINE["holdout_min_cash"],
            "holdout_median_cash_at_least_h561": float(holdout["first_ten_ending_cash"].median()) >= H561_BASELINE["holdout_median_cash"],
            "all_408_stress_rows_passed": bool(frame["first_ten_passed"].all()),
        },
        "new_data_collected": False,
        "model_refit": True,
        "replay_rerun": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "decision": "pending_cloud_validation",
        "evidence_boundary": "strict-quality old-clean paper replay; no authenticated order, fill, settlement, released-cash or balance receipt",
    }
    output.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output / "rolling-metrics.csv", index=False)
    encoded = json.dumps(report, ensure_ascii=False, indent=2).encode() + b"\n"
    report["summary_sha256"] = hashlib.sha256(encoded).hexdigest()
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(json.dumps({key: result[key] for key in ("selection", "holdout", "non_degradation_gate")}, sort_keys=True))


if __name__ == "__main__":
    main()

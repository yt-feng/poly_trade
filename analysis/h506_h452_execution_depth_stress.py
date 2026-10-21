"""H506: execution-depth stress for the frozen H452/H453 paper frontier.

The H452 signal, full-cash sizing, fee arithmetic, settlement delay and
single evolving cash ledger remain fixed.  This audit changes only the
observability of a full-size break-even exit: the first eligible quote must be
after a predeclared latency and its displayed size is multiplied by a depth
haircut.  It is a paper diagnostic over the pinned old-clean archive; it does
not infer an authenticated fill or submit an order.
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
from h452_old_clean_cash_fraction import EXIT_CUTOFF_SECONDS, _size_from_cash


HYPOTHESIS_ID = "H506_H452_EXECUTION_LATENCY_DEPTH_STRESS"
RESULT_FORMAT = "old-clean-h506-h452-execution-depth-stress-v1"
GROSS_CASH_FRACTION = 1.0
LATENCIES_SECONDS = (0.0, 0.25, 0.50, 1.0)
DEPTH_HAIRCUTS = (1.0, 0.75, 0.50)
REQUIRED_FILLS = 10


def _validate_stress(latency_seconds: float, depth_haircut: float) -> None:
    if float(latency_seconds) < 0.0:
        raise ValueError("latency_seconds must be nonnegative")
    if not 0.0 < float(depth_haircut) <= 1.0:
        raise ValueError("depth_haircut must be in (0, 1]")


def _first_stressed_exit(
    row: Any,
    quotes: pd.DataFrame | None,
    *,
    shares: float,
    entry_outlay: float,
    latency_seconds: float,
    depth_haircut: float,
    cutoff_seconds: float = EXIT_CUTOFF_SECONDS,
) -> dict[str, Any] | None:
    """Return the first causal full-size break-even exit under stress."""
    _validate_stress(latency_seconds, depth_haircut)
    if quotes is None or quotes.empty:
        return None
    side = str(row.side)
    bid_column = "sell_up_cents" if side == "up" else "sell_down_cents"
    size_column = "sell_up_size" if side == "up" else "sell_down_size"
    entry = pd.Timestamp(row.entry_ts) + pd.Timedelta(seconds=float(latency_seconds))
    market_end = pd.Timestamp(row.settlement_ts) + pd.Timedelta(seconds=300.0)
    cutoff = market_end - pd.Timedelta(seconds=float(cutoff_seconds))
    observed = quotes[(quotes["ts_iso"] >= entry) & (quotes["ts_iso"] < cutoff)]
    if observed.empty:
        return None
    bid = pd.to_numeric(observed[bid_column], errors="coerce") / 100.0
    size = pd.to_numeric(observed[size_column], errors="coerce")
    exit_fill = bid - float(row.slippage)
    exit_fee = shares * TAKER_FEE_RATE * exit_fill * (1.0 - exit_fill)
    proceeds = shares * exit_fill - exit_fee
    eligible = (
        bid.notna()
        & size.notna()
        & size.mul(float(depth_haircut)).ge(float(shares))
        & exit_fill.gt(0.0)
        & exit_fill.lt(1.0)
        & proceeds.ge(float(entry_outlay) - 1e-9)
    )
    if not bool(eligible.any()):
        return None
    index = eligible[eligible].index[0]
    return {
        "exit_used": True,
        "exit_payout": float(proceeds.loc[index]),
        "exit_ts": pd.Timestamp(observed.loc[index, "ts_iso"]).isoformat(),
        "exit_fill_price": float(exit_fill.loc[index]),
        "latency_seconds": float(latency_seconds),
        "depth_haircut": float(depth_haircut),
    }


def _replay_stress(
    candidates: pd.DataFrame,
    run_names: list[str],
    quotes: dict[str, pd.DataFrame],
    *,
    latency_seconds: float,
    depth_haircut: float,
    exit_cache: dict[tuple[Any, ...], dict[str, Any] | None],
) -> dict[str, Any]:
    """Replay one chronological, no-reset, full-cash chain."""
    _validate_stress(latency_seconds, depth_haircut)
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
        terms = _size_from_cash(row, cash, GROSS_CASH_FRACTION)
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
            exit_cache[key] = _first_stressed_exit(
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
        "chain": prefix,
    }


def _strip_chain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_chain(item) for key, item in value.items() if key != "chain"}
    if isinstance(value, list):
        return [_strip_chain(item) for item in value]
    return value


def evaluate(
    source_root: Path,
    *,
    source_commit: str,
    output: Path,
    latencies_seconds: tuple[float, ...] = LATENCIES_SECONDS,
    depth_haircuts: tuple[float, ...] = DEPTH_HAIRCUTS,
) -> dict[str, Any]:
    if not latencies_seconds or not depth_haircuts:
        raise ValueError("stress grid cannot be empty")
    for latency in latencies_seconds:
        for haircut in depth_haircuts:
            _validate_stress(latency, haircut)
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {
        name: features[features["run_name"].astype(str).eq(str(name))].copy()
        for name in runs
    }
    candidates_by_slippage: dict[float, list[pd.DataFrame]] = {
        float(s): [] for s in SLIPPAGES
    }
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[
            features["run_name"].astype(str).isin([str(item) for item in runs[:index]])
        ]
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
        [frame for frames in candidates_by_slippage.values() for frame in frames],
        ignore_index=True,
    )
    quotes = (
        _quote_index(source_root, set(all_candidates["slug"].astype(str)))
        if not all_candidates.empty
        else {}
    )
    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for latency in latencies_seconds:
        for haircut in depth_haircuts:
            for slippage in SLIPPAGES:
                frames = candidates_by_slippage[float(slippage)]
                base = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
                selected: list[dict[str, Any]] = []
                for window in windows:
                    result = _replay_stress(
                        base,
                        window["runs"],
                        quotes,
                        latency_seconds=float(latency),
                        depth_haircut=float(haircut),
                        exit_cache=cache,
                    )
                    row = {
                        "latency_seconds": float(latency),
                        "depth_haircut": float(haircut),
                        "slippage": float(slippage),
                        "window_index": window["window_index"],
                        "start_run": window["start_run"],
                        "end_run": window["end_run"],
                        **_strip_chain(result),
                    }
                    rows.append(row)
                    selected.append(row)
                passed = [row for row in selected if bool(row["first_ten_passed"])]
                cells.append(
                    {
                        "latency_seconds": float(latency),
                        "depth_haircut": float(haircut),
                        "slippage": float(slippage),
                        "window_count": len(selected),
                        "all_windows_passed": bool(selected)
                        and len(passed) == len(selected),
                        "growth_window_fraction": len(passed) / len(selected)
                        if selected
                        else 0.0,
                        "minimum_first_ten_fills": min(
                            (int(row["first_ten_fills"]) for row in selected), default=0
                        ),
                        "minimum_first_ten_cash_usd": min(
                            (float(row["first_ten_ending_cash"]) for row in selected),
                            default=INITIAL_CASH,
                        ),
                        "failure_reasons": sorted(
                            {
                                str(row["failure_reason"])
                                for row in selected
                                if row["failure_reason"]
                            }
                        ),
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
        "candidate": "H434_H447_H452_FIXED_SIGNAL_FULL_CASH",
        "latencies_seconds": list(latencies_seconds),
        "depth_haircuts": list(depth_haircuts),
        "exit_cutoff_seconds_before_market_end": EXIT_CUTOFF_SECONDS,
        "window_count": len(windows),
        "cell_summaries": cells,
        "all_cells_passed": bool(cells)
        and all(bool(cell["all_windows_passed"]) for cell in cells),
        "decision": "h506_execution_depth_stress_passes_paper_gate"
        if cells and all(bool(cell["all_windows_passed"]) for cell in cells)
        else "h506_execution_depth_stress_rejects_or_weakens_paper_frontier",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_usd": MINIMUM_ORDER_USD,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_SECONDS,
            "gross_cash_fraction": GROSS_CASH_FRACTION,
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
        "evidence_boundary": "strict-quality old-clean quote replay; latency/depth stress is not authenticated execution or reusable cash",
        "root_cause": "tests whether the H452 displayed full-size exit survives explicit observation latency and depth haircut without changing the frozen signal or ledger",
        "window_rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(
        pd.DataFrame(rows).to_csv(index=False), encoding="utf-8"
    )
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result["summary_sha256"] = hashlib.sha256(
        (output / "summary.json").read_bytes()
    ).hexdigest()
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"cell_summaries": cells, "decision": result["decision"]},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--latencies-seconds", type=float, nargs="+", default=list(LATENCIES_SECONDS))
    parser.add_argument("--depth-haircuts", type=float, nargs="+", default=list(DEPTH_HAIRCUTS))
    args = parser.parse_args()
    evaluate(
        args.source_root,
        source_commit=args.source_commit,
        output=args.output,
        latencies_seconds=tuple(args.latencies_seconds),
        depth_haircuts=tuple(args.depth_haircuts),
    )


if __name__ == "__main__":
    main()

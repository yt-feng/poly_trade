"""H450: causal late-window depth-decay gate on the frozen H447 replay.

The source archive, H434 prequential entry model, q>=0.80 gate, fee, slippage,
minimum order, 900-second cash-release clock and one evolving $10 ledger stay
fixed.  Only the latest admissible exit timestamp changes: a break-even exit
must be observed before market end, market end minus 30 seconds, or market end
minus 60 seconds.  This is a pre-entry-independent diagnostic of whether
late-window displayed depth is carrying the H447 result.

Displayed bid and size remain paper evidence only.  No account fill,
settlement, released cash or Canary count is created by this replay.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from h447_old_clean_breakeven_exit import (
    SOURCE_COMMIT,
    EXPECTED_COMPLETE_RUNS,
    SLIPPAGES,
    WINDOW_RUNS,
    WINDOW_STRIDE,
    _attach_exit_events,
    _candidate_frame,
    _fit,
    _quote_index,
    _replay,
    _strip_chain,
)
from h434_old_clean_extratrees_high_confidence import apply_confidence_gate
import v1_execution_walk_forward as study


EXIT_CUTOFF_SECONDS = (0.0, 30.0, 60.0)


def _first_break_even_exit_cutoff(row: Any, quotes: pd.DataFrame | None, cutoff_seconds: float) -> dict[str, Any]:
    """Find the first causal full-size break-even bid before the fixed cutoff."""
    if quotes is None or quotes.empty:
        return {"exit_used": False, "exit_payout": None, "exit_ts": None, "exit_fill_price": None}
    side = str(row.side)
    bid_column = "sell_up_cents" if side == "up" else "sell_down_cents"
    size_column = "sell_up_size" if side == "up" else "sell_down_size"
    entry = pd.Timestamp(row.entry_ts)
    market_end = pd.Timestamp(row.settlement_ts) + pd.Timedelta(seconds=300.0)
    cutoff = market_end - pd.Timedelta(seconds=float(cutoff_seconds))
    observed = quotes[(quotes["ts_iso"] >= entry) & (quotes["ts_iso"] < cutoff)].copy()
    if observed.empty:
        return {"exit_used": False, "exit_payout": None, "exit_ts": None, "exit_fill_price": None}
    shares = float(row.shares)
    entry_outlay = float(row.cost + row.fee)
    bid = pd.to_numeric(observed[bid_column], errors="coerce") / 100.0
    size = pd.to_numeric(observed[size_column], errors="coerce")
    exit_fill = bid - float(row.slippage)
    exit_fee = shares * 0.07 * exit_fill * (1.0 - exit_fill)
    proceeds = shares * exit_fill - exit_fee
    eligible = (
        bid.notna() & size.notna() & size.ge(shares)
        & exit_fill.gt(0.0) & exit_fill.lt(1.0)
        & proceeds.ge(entry_outlay - 1e-9)
    )
    if not bool(eligible.any()):
        return {"exit_used": False, "exit_payout": None, "exit_ts": None, "exit_fill_price": None}
    index = eligible[eligible].index[0]
    return {
        "exit_used": True,
        "exit_payout": float(proceeds.loc[index]),
        "exit_ts": pd.Timestamp(observed.loc[index, "ts_iso"]).isoformat(),
        "exit_fill_price": float(exit_fill.loc[index]),
    }


def _attach_cutoff_exits(candidates: pd.DataFrame, quotes: dict[str, pd.DataFrame], cutoff_seconds: float) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    out = candidates.copy()
    events = [
        _first_break_even_exit_cutoff(row, quotes.get(str(row.slug)), cutoff_seconds)
        for row in out.itertuples(index=False)
    ]
    event_frame = pd.DataFrame(events, index=out.index)
    for column in event_frame.columns:
        out[column] = event_frame[column]
    return out


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = __import__("subprocess").check_output(
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
            candidate = apply_confidence_gate(_candidate_frame(model, medians, by_run[run_name], float(slippage)))
            if not candidate.empty:
                current = candidate.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)
    all_candidates = pd.concat(
        [frame for frames in candidates_by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = _quote_index(source_root, set(all_candidates["slug"].astype(str)))
    windows = __import__("h440_h434_rolling_stability", fromlist=["rolling_windows"]).rolling_windows(
        runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE
    )
    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for cutoff in EXIT_CUTOFF_SECONDS:
        for slippage in SLIPPAGES:
            base = pd.concat(candidates_by_slippage[float(slippage)], ignore_index=True) if candidates_by_slippage[float(slippage)] else pd.DataFrame()
            candidates = _attach_cutoff_exits(base, quotes, cutoff)
            for window in windows:
                result = _replay(candidates, window["runs"], early_exit=True)
                rows.append({
                    "cutoff_seconds_before_market_end": float(cutoff),
                    "window_index": window["window_index"],
                    "start_run": window["start_run"], "end_run": window["end_run"],
                    "slippage": float(slippage), **_strip_chain(result),
                })
            selected = [r for r in rows if r["cutoff_seconds_before_market_end"] == float(cutoff) and r["slippage"] == float(slippage)]
            passed = [r for r in selected if bool(r["first_ten_passed"])]
            cells.append({
                "cutoff_seconds_before_market_end": float(cutoff), "slippage": float(slippage),
                "window_count": len(selected), "all_windows_passed": bool(selected) and len(passed) == len(selected),
                "growth_window_fraction": len(passed) / len(selected) if selected else 0.0,
                "minimum_first_ten_fills": min((int(r["first_ten_fills"]) for r in selected), default=0),
                "minimum_first_ten_cash_usd": min((float(r["first_ten_ending_cash"]) for r in selected), default=10.0),
                "total_first_ten_early_exits": sum(int(r["first_ten_early_exits"]) for r in selected),
            })
    all_cutoff_cells_pass = bool(cells) and all(c["all_windows_passed"] for c in cells)
    result: dict[str, Any] = {
        "format": "old-clean-h450-h447-depth-decay-gate-v1",
        "hypothesis_id": "H450_H447_LATE_WINDOW_DEPTH_DECAY_GATE",
        "source_commit": source_commit, "observed_source_commit": observed,
        "strict_quality": True, "complete_runs": len(runs), "feature_rows": int(len(features)),
        "candidate": "H447_H434_CAUSAL_BREAKEVEN_EXIT",
        "cutoff_seconds_before_market_end": list(EXIT_CUTOFF_SECONDS), "window_count": len(windows),
        "cell_summaries": cells, "all_cutoff_cells_pass": all_cutoff_cells_pass,
        "decision": "h450_late_depth_cutoff_non_falsifying_h447" if all_cutoff_cells_pass else "h450_late_depth_cutoff_rejects_or_weakens_h447",
        "protocol": {
            "initial_cash_usd": 10.0, "required_fills": 10, "minimum_order_usd": 5.0,
            "minimum_order_shares": 5.0, "taker_fee_rate": 0.07, "cash_release_delay_seconds": 900.0,
            "slippages": list(SLIPPAGES), "same_ledger_within_window": True,
            "no_cross_window_concatenation": True, "old_clean_dataset_only": True,
            "exit_cutoff_is_predeclared": True, "paper_only": True,
        },
        "actual_fills": 0, "verified_canary_fills": 0, "same_ledger_compounding_rows": 0,
        "new_data_collected": False, "model_refit": True, "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean quote replay; displayed bid/size is not authenticated execution or reusable cash",
        "root_cause": "tests whether late-window displayed depth, rather than entry signal or fee arithmetic, carries the H447 paper result",
        "window_rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(pd.DataFrame(rows).to_csv(index=False), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cell_summaries": cells, "decision": result["decision"]}, ensure_ascii=False, sort_keys=True))
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

"""Causal fixed-bin Beta-Binomial calibration for the frozen old-clean data.

The current run receives a probability formed from outcomes in strictly prior
runs within a fixed H38-score bin, shrunk toward its current two-minute market
probability.  No current or future outcome is used.  A conservative 1%
slippage cost is included in the signal edge; paper results are evaluated with
both frozen slippages and one evolving cash ledger.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain


STRATEGY = "h43_betabinomial_residual"
SLIPPAGES = (0.005, 0.010)
EDGE_BUFFERS = (0.0, 0.01, 0.02, 0.03)
CONSERVATIVE_EDGE_SLIPPAGE = 0.010
PRIOR_STRENGTH = 20.0
SCORE_EDGES = np.asarray([-100.0, -20.0, -10.0, -5.0, 0.0, 5.0, 10.0, 20.0, 100.0])
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
FRACTION = 0.04
CASH_RELEASE_DELAY_SECONDS = 600.0
SETTLEMENT_SECONDS = 300.0
SELECTION_BLOCK_SIZES = (4, 4, 4, 4, 4)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def score(frame: pd.DataFrame) -> pd.Series:
    move = pd.to_numeric(frame["btc_move_2m"], errors="coerce")
    market = pd.to_numeric(frame["mid_up_prob_2m"], errors="coerce")
    return move * (1.0 - 2.0 * (market - 0.5).abs())


def score_bin(values: pd.Series) -> pd.Series:
    clipped = pd.to_numeric(values, errors="coerce").clip(float(SCORE_EDGES[0]) + 1e-9, float(SCORE_EDGES[-1]) - 1e-9)
    return pd.Series(np.digitize(clipped.to_numpy(float), SCORE_EDGES[1:-1], right=False), index=values.index, dtype="Int64")


def expanding_probabilities(frame: pd.DataFrame, runs: list[str]) -> pd.Series:
    ordered = frame.sort_values("first_quote_ts", kind="stable")
    current_score = score(ordered)
    current_market = pd.to_numeric(ordered["mid_up_prob_2m"], errors="coerce")
    current_bin = score_bin(current_score)
    labels = pd.to_numeric(ordered["outcome_up"], errors="coerce")
    probabilities = pd.Series(np.nan, index=ordered.index, dtype=float)
    run_series = ordered["run_name"].astype(str)
    for index, run_name in enumerate(runs):
        current_mask = run_series.eq(str(run_name))
        if index == 0 or not current_mask.any():
            continue
        prior_mask = run_series.isin([str(x) for x in runs[:index]])
        valid = prior_mask & current_bin.notna() & labels.isin([0.0, 1.0])
        counts = labels.loc[valid].groupby(current_bin.loc[valid]).agg(["sum", "count"])
        for row_index in ordered.index[current_mask]:
            bucket = current_bin.loc[row_index]
            market_prob = current_market.loc[row_index]
            if pd.isna(bucket) or pd.isna(market_prob):
                continue
            if bucket in counts.index:
                success = float(counts.loc[bucket, "sum"])
                total = float(counts.loc[bucket, "count"])
            else:
                success = 0.0
                total = 0.0
            probabilities.loc[row_index] = (success + 1.0 + PRIOR_STRENGTH * float(market_prob)) / (total + 2.0 + PRIOR_STRENGTH)
    return probabilities.reindex(frame.index)


def add_edge_signal(frame: pd.DataFrame, probabilities: pd.Series, edge_buffer: float) -> pd.DataFrame:
    out = frame.copy()
    p_up = pd.to_numeric(probabilities, errors="coerce")
    up_quote = pd.to_numeric(out["buy_up_price_2m"], errors="coerce")
    down_quote = pd.to_numeric(out["buy_down_price_2m"], errors="coerce")
    up_fill = up_quote + CONSERVATIVE_EDGE_SLIPPAGE
    down_fill = down_quote + CONSERVATIVE_EDGE_SLIPPAGE
    up_edge = p_up - up_fill - 0.07 * up_fill * (1.0 - up_fill)
    down_edge = (1.0 - p_up) - down_fill - 0.07 * down_fill * (1.0 - down_fill)
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[up_edge.ge(edge_buffer) & up_edge.ge(down_edge)] = "buy_up"
    side.loc[down_edge.ge(edge_buffer) & down_edge.gt(up_edge)] = "buy_down"
    out[f"{STRATEGY}_side"] = side
    out[f"{STRATEGY}_minute"] = 2
    out[f"{STRATEGY}_fraction"] = FRACTION
    out[f"{STRATEGY}_component"] = f"beta_bin_edge_{edge_buffer:.3f}"
    return out


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    return [
        {"status": "paper", "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()), "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()), "entry_cost": str(row.filled_cost), "fee": str(row.fee_usd), "payout": str(row.payout_at_settlement)}
        for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)
    ]


def replay(frame: pd.DataFrame, run_names: list[str], edge_buffer: float, slippage: float) -> tuple[dict, pd.DataFrame]:
    logs, metrics = simulate_cash_locked(frame[frame["run_name"].astype(str).isin([str(x) for x in run_names])], STRATEGY, study.ExecutionConfig(fixed_slippage=slippage), INITIAL_CASH, MINIMUM_ORDER, CASH_RELEASE_DELAY_SECONDS, cooldown_events=0, capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07)
    events = paper_events(logs)
    chain = evaluate_compound_chain(events, initial_cash="10", minimum_order="5", required_fills=10, evidence_mode="paper_upper_bound")
    prefix = evaluate_compound_chain(events[:10], initial_cash="10", minimum_order="5", required_fills=10, evidence_mode="paper_upper_bound")
    return {"edge_buffer": edge_buffer, "slippage": slippage, **metrics, "paper_chain_fills": chain["fills"], "paper_chain_ending_cash": chain["ending_cash"], "paper_chain_minimum_cash": chain["minimum_cash"], "paper_chain_passed": chain["paper_replay_passed"], "failure_reason": chain["failure_reason"], "first_ten_fills": prefix["fills"], "first_ten_ending_cash": prefix["ending_cash"], "first_ten_minimum_cash": prefix["minimum_cash"], "first_ten_passed": prefix["paper_replay_passed"]}, logs


def blocks(runs: list[str], sizes: tuple[int, ...]) -> list[tuple[int, list[str]]]:
    out, cursor = [], 0
    for index, size in enumerate(sizes, start=1):
        out.append((index, runs[cursor : cursor + size]))
        cursor += size
    return out


def evaluate(source_root: Path, *, source_commit: str) -> dict:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, _, runs = study.prepare_data(source_root)
    probabilities = expanding_probabilities(frame, runs)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    rows, block_rows = [], []
    for edge_buffer in EDGE_BUFFERS:
        candidate = add_edge_signal(frame, probabilities, edge_buffer)
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            for slippage in SLIPPAGES:
                metrics, _ = replay(candidate, names, edge_buffer, slippage)
                rows.append({"split": split, **metrics})
        for split, run_names, sizes in (("selection_block", selection_runs, SELECTION_BLOCK_SIZES), ("holdout_block", holdout_runs, HOLDOUT_BLOCK_SIZES)):
            for block, names in blocks(run_names, sizes):
                for slippage in SLIPPAGES:
                    metrics, _ = replay(candidate, names, edge_buffer, slippage)
                    block_rows.append({"split": split, "block": block, "run_count": len(names), **metrics})
    table = pd.DataFrame(rows)
    block_table = pd.DataFrame(block_rows)
    admissible = []
    for edge_buffer in EDGE_BUFFERS:
        selection = table[(table.split == "selection20") & (table.edge_buffer == edge_buffer)]
        selection_blocks = block_table[(block_table.split == "selection_block") & (block_table.edge_buffer == edge_buffer)]
        if len(selection) == len(SLIPPAGES) and bool(selection["paper_chain_passed"].all()) and len(selection_blocks) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES) and bool((selection_blocks["ending_cash"] >= 5.0).all()) and bool((selection_blocks["net_return"] >= 0.0).all()):
            admissible.append(edge_buffer)
    selected = None
    if admissible:
        selected = float(table[(table.split == "selection20") & table.edge_buffer.isin(admissible)].groupby("edge_buffer")["ending_cash"].mean().idxmax())
    holdout = table[(table.split == "holdout59") & (table.edge_buffer == selected)] if selected is not None else table.iloc[0:0]
    holdout_blocks = block_table[(block_table.split == "holdout_block") & (block_table.edge_buffer == selected)] if selected is not None else block_table.iloc[0:0]
    gate = bool(selected is not None and len(holdout) == len(SLIPPAGES) and bool(holdout["paper_chain_passed"].all()) and bool((holdout["trades"] >= 10).all()) and len(holdout_blocks) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) and bool((holdout_blocks["ending_cash"] >= 5.0).all()) and bool((holdout_blocks["net_return"] >= 0.0).all()))
    return {
        "format": "old-clean-h43-betabinomial-residual-v1", "candidate": "H43_score_bin_beta_binomial_market_prior", "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed, "complete_runs": len(runs), "feature_rows": len(frame), "score_bins": SCORE_EDGES.tolist(), "prior_strength": PRIOR_STRENGTH, "smoothing": "(successes + 1 + 20*market_probability)/(count + 2 + 20)", "training_rule": "each run uses only completed prior runs", "edge_rule": "conservative 1% slippage fill price plus fee", "edge_buffers": list(EDGE_BUFFERS), "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER, "fee_rate": 0.07, "settlement_seconds": SETTLEMENT_SECONDS, "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS, "slippages": list(SLIPPAGES), "entry_minute": 2, "fraction": FRACTION, "same_ledger": True}, "selection_rule": "selection20 same-ledger pass at both slippages and all five selection blocks at or above $5 with nonnegative PnL; maximize mean selection ending cash", "admissible_buffers": admissible, "selected_buffer": selected, "results": rows, "blocks": block_rows, "promotion_gate_passed": gate, "interpretation": "old-clean causal paper replay only; no new data, live orders or authenticated Canary evidence", "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"admissible_buffers": result["admissible_buffers"], "selected_buffer": result["selected_buffer"], "promotion_gate_passed": result["promotion_gate_passed"]}, sort_keys=True))


if __name__ == "__main__":
    main()

"""Causal expanding probability calibration with a fee-adjusted edge gate.

The model is fitted separately for each test run using only completed earlier
runs.  It uses the frozen H38 directional score and the two-minute market
probability; the market outcome is never available to the current prediction.
The entry gate is fixed before evaluation and uses a conservative 1% slippage
edge estimate, while the replay is checked at both 0.5% and 1% slippage.
Results are paper upper bounds on one cash ledger and never authenticate a
Canary fill.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain


STRATEGY = "h42_causal_calibrated_edge"
SLIPPAGES = (0.005, 0.010)
EDGE_BUFFERS = (0.0, 0.01, 0.02, 0.03)
CONSERVATIVE_EDGE_SLIPPAGE = 0.010
MIN_HISTORY_ROWS = 50
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
FRACTION = 0.04
SETTLEMENT_SECONDS = 300.0
CASH_RELEASE_DELAY_SECONDS = 600.0
BLOCK_SIZES = (12, 12, 12, 12, 11)
SELECTION_BLOCK_SIZES = (4, 4, 4, 4, 4)


def _score(frame: pd.DataFrame) -> pd.Series:
    move = pd.to_numeric(frame["btc_move_2m"], errors="coerce")
    market = pd.to_numeric(frame["mid_up_prob_2m"], errors="coerce")
    return move * (1.0 - 2.0 * (market - 0.5).abs())


def _model_features(frame: pd.DataFrame) -> pd.DataFrame:
    market = pd.to_numeric(frame["mid_up_prob_2m"], errors="coerce").clip(0.001, 0.999)
    score = _score(frame).clip(-100.0, 100.0) / 10.0
    market_logit = np.log(market / (1.0 - market))
    return pd.DataFrame({"score_scaled": score, "market_logit": market_logit}, index=frame.index)


def expanding_probabilities(frame: pd.DataFrame, runs: list[str]) -> tuple[pd.Series, pd.Series]:
    """Predict each run from strictly prior runs and return p plus history rows."""
    ordered = frame.sort_values("first_quote_ts", kind="stable")
    probabilities = pd.Series(np.nan, index=ordered.index, dtype=float)
    history_counts = pd.Series(0, index=ordered.index, dtype=int)
    features = _model_features(ordered)
    labels = pd.to_numeric(ordered["outcome_up"], errors="coerce")
    for index, run_name in enumerate(runs):
        current_mask = ordered["run_name"].astype(str).eq(str(run_name))
        current = features.loc[current_mask]
        if current.empty or index == 0:
            continue
        training_mask = ordered["run_name"].astype(str).isin([str(x) for x in runs[:index]])
        valid = training_mask & features.notna().all(axis=1) & labels.isin([0.0, 1.0])
        train_x = features.loc[valid]
        train_y = labels.loc[valid]
        current_valid = current.notna().all(axis=1)
        if train_x.empty or int(len(train_x)) < MIN_HISTORY_ROWS:
            probabilities.loc[current.index[current_valid]] = ordered.loc[current.index[current_valid], "mid_up_prob_2m"].astype(float)
            history_counts.loc[current.index] = int(len(train_x))
            continue
        if train_y.nunique() < 2:
            value = float(train_y.mean())
            probabilities.loc[current.index[current_valid]] = value
            history_counts.loc[current.index] = int(len(train_x))
            continue
        model = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=1.0, max_iter=300, solver="lbfgs", random_state=0),
        )
        model.fit(train_x.to_numpy(float), train_y.to_numpy(float))
        probabilities.loc[current.index[current_valid]] = model.predict_proba(current.loc[current_valid].to_numpy(float))[:, 1]
        history_counts.loc[current.index] = int(len(train_x))
    return probabilities.reindex(frame.index), history_counts.reindex(frame.index)


def add_edge_signal(frame: pd.DataFrame, probabilities: pd.Series, edge_buffer: float) -> pd.DataFrame:
    out = frame.copy()
    p_up = pd.to_numeric(probabilities, errors="coerce")
    up_quote = pd.to_numeric(out["buy_up_price_2m"], errors="coerce")
    down_quote = pd.to_numeric(out["buy_down_price_2m"], errors="coerce")
    up_fill = up_quote + CONSERVATIVE_EDGE_SLIPPAGE
    down_fill = down_quote + CONSERVATIVE_EDGE_SLIPPAGE
    up_fee = 0.07 * up_fill * (1.0 - up_fill)
    down_fee = 0.07 * down_fill * (1.0 - down_fill)
    up_edge = p_up - up_fill - up_fee
    down_edge = (1.0 - p_up) - down_fill - down_fee
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[up_edge.ge(edge_buffer) & up_edge.ge(down_edge)] = "buy_up"
    side.loc[down_edge.ge(edge_buffer) & down_edge.gt(up_edge)] = "buy_down"
    out[f"{STRATEGY}_side"] = side
    out[f"{STRATEGY}_minute"] = 2
    out[f"{STRATEGY}_fraction"] = FRACTION
    out[f"{STRATEGY}_component"] = f"expanding_logistic_edge_{edge_buffer:.3f}"
    out["h42_predicted_up_probability"] = p_up
    out["h42_up_edge_after_conservative_cost"] = up_edge
    out["h42_down_edge_after_conservative_cost"] = down_edge
    return out


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)
    ]


def replay(frame: pd.DataFrame, run_names: list[str], edge_buffer: float, slippage: float) -> tuple[dict, pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(x) for x in run_names])],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=slippage),
        INITIAL_CASH,
        MINIMUM_ORDER,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
    )
    events = paper_events(logs)
    chain = evaluate_compound_chain(events, initial_cash="10", minimum_order="5", required_fills=10, evidence_mode="paper_upper_bound")
    prefix = evaluate_compound_chain(events[:10], initial_cash="10", minimum_order="5", required_fills=10, evidence_mode="paper_upper_bound")
    return {
        "edge_buffer": edge_buffer,
        "slippage": slippage,
        **metrics,
        "paper_chain_fills": chain["fills"],
        "paper_chain_ending_cash": chain["ending_cash"],
        "paper_chain_minimum_cash": chain["minimum_cash"],
        "paper_chain_passed": chain["paper_replay_passed"],
        "failure_reason": chain["failure_reason"],
        "first_ten_fills": prefix["fills"],
        "first_ten_ending_cash": prefix["ending_cash"],
        "first_ten_minimum_cash": prefix["minimum_cash"],
        "first_ten_passed": prefix["paper_replay_passed"],
    }, logs


def block_names(runs: list[str], sizes: tuple[int, ...]) -> list[tuple[int, list[str]]]:
    out = []
    cursor = 0
    for index, size in enumerate(sizes, start=1):
        out.append((index, runs[cursor : cursor + size]))
        cursor += size
    return out


def evaluate(source_root: Path, *, source_commit: str) -> dict:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, _, runs = study.prepare_data(source_root)
    probabilities, history_counts = expanding_probabilities(frame, runs)
    frame = frame.copy()
    frame["h42_history_rows"] = history_counts
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    rows: list[dict] = []
    blocks: list[dict] = []
    cache: dict[float, pd.DataFrame] = {}
    for edge_buffer in EDGE_BUFFERS:
        candidate = add_edge_signal(frame, probabilities, edge_buffer)
        cache[edge_buffer] = candidate
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            for slippage in SLIPPAGES:
                metrics, _ = replay(candidate, names, edge_buffer, slippage)
                rows.append({"split": split, **metrics})
        for block, names in block_names(selection_runs, SELECTION_BLOCK_SIZES):
            for slippage in SLIPPAGES:
                metrics, _ = replay(candidate, names, edge_buffer, slippage)
                blocks.append({"split": "selection_block", "block": block, "run_count": len(names), **metrics})
        for block, names in block_names(holdout_runs, BLOCK_SIZES):
            for slippage in SLIPPAGES:
                metrics, _ = replay(candidate, names, edge_buffer, slippage)
                blocks.append({"split": "holdout_block", "block": block, "run_count": len(names), **metrics})
    rows_df = pd.DataFrame(rows)
    blocks_df = pd.DataFrame(blocks)
    admissible: list[float] = []
    for edge_buffer in EDGE_BUFFERS:
        sel = rows_df[(rows_df.split == "selection20") & (rows_df.edge_buffer == edge_buffer)]
        sel_blocks = blocks_df[(blocks_df.split == "selection_block") & (blocks_df.edge_buffer == edge_buffer)]
        if (
            len(sel) == len(SLIPPAGES)
            and bool(sel["paper_chain_passed"].all())
            and len(sel_blocks) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES)
            and bool((sel_blocks["ending_cash"] >= 5.0).all())
            and bool((sel_blocks["net_return"] >= 0.0).all())
        ):
            admissible.append(edge_buffer)
    if admissible:
        selection_rows = rows_df[(rows_df.split == "selection20") & rows_df.edge_buffer.isin(admissible)]
        selected_buffer = float(selection_rows.groupby("edge_buffer")["ending_cash"].mean().idxmax())
    else:
        selected_buffer = None
    holdout = rows_df[(rows_df.split == "holdout59") & (rows_df.edge_buffer == selected_buffer)] if selected_buffer is not None else rows_df.iloc[0:0]
    holdout_blocks = blocks_df[(blocks_df.split == "holdout_block") & (blocks_df.edge_buffer == selected_buffer)] if selected_buffer is not None else blocks_df.iloc[0:0]
    gate = bool(
        selected_buffer is not None
        and len(holdout) == len(SLIPPAGES)
        and bool(holdout["paper_chain_passed"].all())
        and bool((holdout["trades"] >= 10).all())
        and len(holdout_blocks) == len(BLOCK_SIZES) * len(SLIPPAGES)
        and bool((holdout_blocks["ending_cash"] >= 5.0).all())
        and bool((holdout_blocks["net_return"] >= 0.0).all())
    )
    return {
        "format": "old-clean-h42-causal-calibrated-edge-v1",
        "candidate": "H42_expanding_logistic_score_market_probability_edge",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(frame),
        "history_rows_minimum": MIN_HISTORY_ROWS,
        "training_rule": "each run predicted only from completed earlier runs",
        "model": {"kind": "expanding LogisticRegression with StandardScaler", "C": 1.0, "features": ["score_scaled", "market_logit"], "random_state": 0},
        "edge_rule": "max(p_up - conservative_fill_price - fee_per_share, (1-p_up) - conservative_fill_price - fee_per_share) >= edge_buffer",
        "conservative_edge_slippage": CONSERVATIVE_EDGE_SLIPPAGE,
        "edge_buffers": list(EDGE_BUFFERS),
        "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER, "fee_rate": 0.07, "settlement_seconds": SETTLEMENT_SECONDS, "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS, "slippages": list(SLIPPAGES), "entry_minute": 2, "fraction": FRACTION, "same_ledger": True},
        "selection_rule": "selection20 same-ledger pass at both slippages and all five selection blocks at or above $5 with nonnegative PnL; maximize mean selection ending cash",
        "admissible_buffers": admissible,
        "selected_buffer": selected_buffer,
        "results": rows,
        "blocks": blocks,
        "promotion_gate_passed": gate,
        "interpretation": "old-clean causal paper replay only; no new data, live orders or authenticated Canary evidence",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
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
    print(json.dumps({"selected_buffer": result["selected_buffer"], "admissible_buffers": result["admissible_buffers"], "promotion_gate_passed": result["promotion_gate_passed"]}, sort_keys=True))


if __name__ == "__main__":
    main()

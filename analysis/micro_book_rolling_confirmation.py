"""Rolling, fixed-threshold confirmation for the micro-book price cap."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import prepare


def make_variant(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    keep = (
        out["micro_book_consensus_up_side"].astype(str).eq("buy_up")
        & (out["buy_up_price_2m"] <= 0.65)
    )
    out["micro_price65_side"] = out["micro_book_consensus_up_side"].where(keep, "skip")
    out["micro_price65_minute"] = 2
    out["micro_price65_fraction"] = 0.04
    out["micro_price65_component"] = "micro_book_price65"
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    features, runs = prepare(Path(args.source_root), ["micro_book_consensus_up"])
    features = make_variant(features)
    folds = study.make_run_folds(runs)
    config = study.ExecutionConfig()
    _, global_metrics = study.simulate(
        features,
        "micro_price65",
        config,
        10.0,
        5.0,
        capture_logs=False,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
        fee_exponent=1.0,
        min_cost_policy="raise_to_min",
        min_order_mode="notional",
        min_order_shares=5.0,
    )
    bankroll = 10.0
    rows = []
    for fold in folds:
        frame = features[features["run_name"] == fold["test_run"]].copy()
        _, metrics = study.simulate(
            frame,
            "micro_price65",
            config,
            bankroll,
            5.0,
            capture_logs=False,
            fee_mode="polymarket_taker",
            fee_rate=0.07,
            fee_exponent=1.0,
            min_cost_policy="raise_to_min",
            min_order_mode="notional",
            min_order_shares=5.0,
        )
        rows.append({
            "fold_id": fold["fold_id"],
            "test_run": fold["test_run"],
            "starting_bankroll": bankroll,
            "ending_bankroll": metrics["ending_bankroll"],
            "total_return": metrics["total_return"],
            "trades": metrics["trades"],
            "max_drawdown": metrics["max_drawdown"],
            "profit_factor": metrics["profit_factor"],
        })
        bankroll = float(metrics["ending_bankroll"])
    result = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    blocks = []
    for block_id, start, end in [("first20", 1, 20), ("next20", 21, 40), ("next20", 41, 60), ("last19", 61, 79)]:
        block = result[result["fold_id"].between(start, end)]
        blocks.append({
            "block": block_id,
            "start_fold": start,
            "end_fold": end,
            "starting_bankroll": float(block["starting_bankroll"].iloc[0]),
            "ending_bankroll": float(block["ending_bankroll"].iloc[-1]),
            "trades": int(block["trades"].sum()),
            "positive_folds": int((block["total_return"] > 0).sum()),
            "zero_trade_folds": int((block["trades"] == 0).sum()),
            "max_drawdown": float(block["max_drawdown"].max()),
        })
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    print(result.tail(10).to_string(index=False))
    print(pd.DataFrame(blocks).to_string(index=False))
    print({"folds": len(result), "rolling_ending_bankroll": float(result["ending_bankroll"].iloc[-1]), "rolling_trades": int(result["trades"].sum()), "global_continuous_ending_bankroll": global_metrics["ending_bankroll"], "global_continuous_trades": global_metrics["trades"], "global_continuous_drawdown": global_metrics["max_drawdown"]})


if __name__ == "__main__":
    main()

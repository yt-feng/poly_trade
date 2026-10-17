"""Predeclared threshold ablation for the registered micro-book candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import prepare


VARIANTS = {
    "base": {},
    "price_70": {"price_max": 0.70},
    "price_65": {"price_max": 0.65},
    "imbalance_20": {"imbalance_min": 0.20},
    "move_20": {"move_min": 20.0},
    "strict_gate": {"spread_max": 0.04, "overround_max": 0.03, "size_min": 180.0},
    "combined_strict": {
        "price_max": 0.70,
        "imbalance_min": 0.20,
        "move_min": 20.0,
        "spread_max": 0.04,
        "overround_max": 0.03,
        "size_min": 180.0,
    },
}


def variant_frame(frame: pd.DataFrame, variant: dict, name: str) -> pd.DataFrame:
    out = frame.copy()
    base_signal = out["micro_book_consensus_up_side"].astype(str).eq("buy_up")
    signal = base_signal.copy()
    if "price_max" in variant:
        signal &= out["buy_up_price_2m"] <= variant["price_max"]
    if "imbalance_min" in variant:
        signal &= out["size_imbalance_updown_2m"] >= variant["imbalance_min"]
    if "move_min" in variant:
        signal &= out["btc_move_2m"] >= variant["move_min"]
    if "spread_max" in variant:
        signal &= out["spread_up_median_first2m"] <= variant["spread_max"]
    if "overround_max" in variant:
        signal &= out["overround_median_first2m"] <= variant["overround_max"]
    if "size_min" in variant:
        signal &= out["buy_up_size_2m"] >= variant["size_min"]
    out[f"{name}_side"] = out["micro_book_consensus_up_side"].where(signal, "skip")
    out[f"{name}_minute"] = 2
    out[f"{name}_fraction"] = 0.04
    out[f"{name}_component"] = name
    return out


def simulate(frame: pd.DataFrame, name: str, config: study.ExecutionConfig, bankroll: float) -> dict:
    _, metrics = study.simulate(
        frame,
        name,
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
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--train-runs", type=int, default=20)
    parser.add_argument("--validation-runs", type=int, default=20)
    args = parser.parse_args()
    features, runs = prepare(Path(args.source_root), ["micro_book_consensus_up"])
    needed = args.train_runs + args.validation_runs + 1
    if len(runs) < needed:
        raise SystemExit(f"need at least {needed} complete runs, found {len(runs)}")
    train = features[features["run_name"].isin(runs[:args.train_runs])].copy()
    validation = features[features["run_name"].isin(runs[args.train_runs:args.train_runs + args.validation_runs])].copy()
    test = features[features["run_name"].isin(runs[args.train_runs + args.validation_runs:])].copy()
    config = study.ExecutionConfig()
    rows = []
    for name, variant in VARIANTS.items():
        train_v = variant_frame(train, variant, name)
        validation_v = variant_frame(validation, variant, name)
        test_v = variant_frame(test, variant, name)
        train_m = simulate(train_v, name, config, 10.0)
        validation_m = simulate(validation_v, name, config, 10.0)
        test_m = simulate(test_v, name, config, float(validation_m["ending_bankroll"]))
        rows.append({
            "variant": name,
            "train_end": train_m["ending_bankroll"],
            "train_trades": train_m["trades"],
            "validation_end": validation_m["ending_bankroll"],
            "validation_trades": validation_m["trades"],
            "validation_drawdown": validation_m["max_drawdown"],
            "test_end": test_m["ending_bankroll"],
            "test_trades": test_m["trades"],
            "test_drawdown": test_m["max_drawdown"],
            "test_profit_factor": test_m["profit_factor"],
        })
    result = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    print(result.to_string(index=False))
    print(json.dumps({"variants": len(result), "train_runs": args.train_runs, "validation_runs": args.validation_runs, "test_runs": len(runs) - args.train_runs - args.validation_runs}, sort_keys=True))


if __name__ == "__main__":
    main()

"""Old-clean-data price-cap sweep with one continuous execution state.

This is a bounded ablation of the predeclared micro-book consensus signal.  It
does not refit a model or collect data: only the entry price cap changes.  Each
cap is replayed over the same complete runs with one global bankroll, cooldown,
and trade-window state so per-run resets cannot manufacture a result.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import prepare


def make_variant(frame: pd.DataFrame, cap: float) -> pd.DataFrame:
    out = frame.copy()
    keep = (
        out["micro_book_consensus_up_side"].astype(str).eq("buy_up")
        & (out["buy_up_price_2m"] <= cap)
    )
    name = f"micro_price_cap_{cap:.2f}".replace(".", "p")
    out[f"{name}_side"] = out["micro_book_consensus_up_side"].where(keep, "skip")
    out[f"{name}_minute"] = 2
    out[f"{name}_fraction"] = 0.04
    out[f"{name}_component"] = "micro_book_price_cap"
    return out


def run_one(frame: pd.DataFrame, strategy: str, slip: float, args: argparse.Namespace) -> dict:
    config = study.ExecutionConfig(fixed_slippage=slip)
    _, metrics = study.simulate(
        frame,
        strategy,
        config,
        args.bankroll0,
        args.min_cost,
        capture_logs=False,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
        fee_exponent=1.0,
        min_cost_policy="raise_to_min",
        min_order_mode=args.min_order_mode,
        min_order_shares=args.min_order_shares,
    )
    return {
        "price_cap": args._cap,
        "slippage": slip,
        "min_cost": args.min_cost,
        "min_order_mode": args.min_order_mode,
        "min_order_shares": args.min_order_shares,
        "ending_bankroll": float(metrics["ending_bankroll"]),
        "trades": int(metrics["trades"]),
        "max_drawdown": float(metrics["max_drawdown"]),
        "profit_factor": float(metrics["profit_factor"]),
        "win_rate": float(metrics["win_rate"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--price-caps", default="0.45,0.50,0.55,0.60,0.65")
    parser.add_argument("--stress-slippages", default="0.0025,0.0050")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--min-order-mode", choices=("notional", "shares", "both"), default="notional")
    parser.add_argument("--min-order-shares", type=float, default=5.0)
    args = parser.parse_args()
    caps = [float(x.strip()) for x in args.price_caps.split(",") if x.strip()]
    slips = [float(x.strip()) for x in args.stress_slippages.split(",") if x.strip()]
    if not caps or not slips:
        raise SystemExit("price caps and slippages must be non-empty")
    base, _ = prepare(Path(args.source_root), ["micro_book_consensus_up"])
    rows = []
    for cap in caps:
        variant = make_variant(base, cap)
        args._cap = cap
        strategy = f"micro_price_cap_{cap:.2f}".replace(".", "p")
        for slip in slips:
            rows.append(run_one(variant, strategy, slip, args))
    result = pd.DataFrame(rows).sort_values(["slippage", "ending_bankroll"], ascending=[True, False]).reset_index(drop=True)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    meta = {
        "complete_runs": int(base["run_name"].nunique()),
        "price_caps": caps,
        "stress_slippages": slips,
        "continuous_state": True,
        "old_clean_dataset_only": True,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "min_cost": args.min_cost,
        "min_order_mode": args.min_order_mode,
        "min_order_shares": args.min_order_shares,
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()

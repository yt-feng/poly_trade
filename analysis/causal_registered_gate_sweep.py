"""Apply the same causal net-positive history gate to frozen candidates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import prepare


DEFAULT_CANDIDATES = ("micro_book_consensus_up", "classic_breakout_up", "classic_early_drop_down")


def prior_net_return(row: pd.Series, candidate: str, fixed_slippage: float) -> float | None:
    side = str(row.get(f"{candidate}_side", "skip"))
    if side == "skip":
        return None
    minute = int(row.get(f"{candidate}_minute", -1))
    if side not in {"buy_up", "buy_down"} or minute not in {1, 2, 4}:
        return None
    prefix = f"exec_{side}_{minute}m_d4"
    values = [pd.to_numeric(row.get(f"{prefix}_{field}"), errors="coerce") for field in ("quote_price", "quote_size", "quote_spread", "quote_overround", "adverse_slippage")]
    outcome = pd.to_numeric(row.get("outcome_up"), errors="coerce")
    if not all(np.isfinite(float(x)) for x in values) or not np.isfinite(float(outcome)):
        return None
    price, size, spread, overround, adverse = [float(x) for x in values]
    fill = price + fixed_slippage
    if not (0.05 <= price <= 0.95 and fill < 0.98 and size >= 100.0 and spread <= 0.04 and overround <= 0.03 and adverse <= 0.015):
        return None
    payout = float(outcome) if side == "buy_up" else 1.0 - float(outcome)
    fee = study.trade_fee_usd(1.0, fill, "polymarket_taker", 0.07, 1.0)
    return (payout - fill - fee) / fill


def attach_gate(features: pd.DataFrame, candidate: str, min_history: int, lookback: int) -> tuple[pd.DataFrame, dict]:
    out = features.sort_values("first_quote_ts").copy()
    name = f"{candidate}_history_net_positive"
    history: list[float] = []
    for idx in out.index:
        recent = history[-lookback:]
        allow = len(recent) >= min_history and float(np.mean(recent)) > 0.0
        out.at[idx, f"{name}_side"] = out.at[idx, f"{candidate}_side"] if allow else "skip"
        out.at[idx, f"{name}_minute"] = int(out.at[idx, f"{candidate}_minute"])
        out.at[idx, f"{name}_fraction"] = float(out.at[idx, f"{candidate}_fraction"])
        out.at[idx, f"{name}_component"] = name
        value = prior_net_return(out.loc[idx], candidate, 0.0025)
        if value is not None:
            history.append(float(value))
    return out, {"candidate": candidate, "min_history": min_history, "lookback": lookback, "history_observations": len(history)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--min-history", type=int, default=20)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    candidates = tuple(x.strip() for x in args.candidates.split(",") if x.strip())
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), list(candidates))
    test_runs = runs[4:]
    rows, blocks = [], []
    run_index = {name: i for i, name in enumerate(test_runs)}
    for candidate in candidates:
        features, diagnostic = attach_gate(base, candidate, args.min_history, args.lookback)
        test = features[features["run_name"].isin(test_runs)].copy()
        strategy = f"{candidate}_history_net_positive"
        for slip in slippages:
            logs, metrics = study.simulate(test, strategy, study.ExecutionConfig(fixed_slippage=slip), args.bankroll0, args.min_cost, capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07, fee_exponent=1.0, min_cost_policy="raise_to_min", min_order_mode="notional", min_order_shares=5.0)
            rows.append({"candidate":candidate,"slippage":slip,"ending_bankroll":float(metrics["ending_bankroll"]),"total_return":float(metrics["total_return"]),"trades":int(metrics["trades"]),"max_drawdown":float(metrics["max_drawdown"]),"profit_factor":float(metrics["profit_factor"]),"win_rate":float(metrics["win_rate"]),"history_observations":diagnostic["history_observations"]})
            if not logs.empty:
                logs=logs.copy(); logs["test_index"]=logs["run_name"].map(run_index)
                for block,start,end in [("first20",0,20),("next20",20,40),("next20b",40,60),("last19",60,79)]:
                    part=logs[logs["test_index"].between(start,end-1)]
                    blocks.append({"candidate":candidate,"slippage":slip,"block":block,"trades":int(len(part)),"ending_bankroll":float(part["bankroll_after"].iloc[-1]) if not part.empty else float("nan"),"sum_pnl":float(part["pnl_usd"].sum()) if not part.empty else 0.0,"win_rate":float((part["pnl_usd"]>0).mean()) if not part.empty else float("nan")})
    result=pd.DataFrame(rows).sort_values(["slippage","ending_bankroll"],ascending=[True,False]).reset_index(drop=True)
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True); result.to_csv(out,index=False); pd.DataFrame(blocks).to_csv(out.with_name(out.stem+"_blocks.csv"),index=False)
    meta={"complete_runs":int(len(runs)),"test_runs":int(len(test_runs)),"candidates":list(candidates),"min_history":args.min_history,"lookback":args.lookback,"slippages":slippages,"bankroll0":args.bankroll0,"min_cost":args.min_cost,"gate":"causal_net_positive_history","continuous_state":True,"causal_history_only":True,"old_clean_dataset_only":True,"new_data_collected":False,"live_orders_enabled":False}
    out.with_name(out.stem+"_meta.json").write_text(json.dumps(meta,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(meta,sort_keys=True)); print(result.to_string(index=False))


if __name__ == "__main__": main()

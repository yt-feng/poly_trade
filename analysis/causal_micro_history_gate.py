"""Causal history gates for the frozen micro-book signal.

The gate sees only already-closed prior markets.  It is computed once before
the execution replay and then held fixed for each row; the current market's
outcome is appended only after its gate decision.  This tests whether a
rolling signal-health filter can avoid a bad regime without using future
labels or selecting on the test result.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import v1_execution_walk_forward as study
from classic_min_order_chain import prepare


GATES = ("net_positive", "win_rate_055")


def prior_net_return(row: pd.Series, fixed_slippage: float) -> float | None:
    if str(row.get("micro_book_consensus_up_side", "skip")) != "buy_up":
        return None
    price = pd.to_numeric(row.get("exec_buy_up_2m_d4_quote_price"), errors="coerce")
    size = pd.to_numeric(row.get("exec_buy_up_2m_d4_quote_size"), errors="coerce")
    spread = pd.to_numeric(row.get("exec_buy_up_2m_d4_quote_spread"), errors="coerce")
    overround = pd.to_numeric(row.get("exec_buy_up_2m_d4_quote_overround"), errors="coerce")
    adverse = pd.to_numeric(row.get("exec_buy_up_2m_d4_adverse_slippage"), errors="coerce")
    outcome = pd.to_numeric(row.get("outcome_up"), errors="coerce")
    if not all(np.isfinite(float(x)) for x in (price, size, spread, overround, adverse, outcome)):
        return None
    fill = float(price) + fixed_slippage
    if not (0.05 <= float(price) <= 0.95 and fill < 0.98 and float(size) >= 100.0 and float(spread) <= 0.04 and float(overround) <= 0.03 and float(adverse) <= 0.015):
        return None
    fee = study.trade_fee_usd(1.0, fill, "polymarket_taker", 0.07, 1.0)
    return (float(outcome) - fill - fee) / fill


def attach_gate(features: pd.DataFrame, gate: str, min_history: int = 20, lookback: int = 100, fixed_slippage: float = 0.0025) -> tuple[pd.DataFrame, dict]:
    out = features.sort_values("first_quote_ts").copy()
    history: list[float] = []
    enabled = 0
    decisions = 0
    for idx in out.index:
        decisions += 1
        recent = history[-lookback:]
        allow = False
        if len(recent) >= min_history:
            if gate == "net_positive":
                allow = float(np.mean(recent)) > 0.0
            elif gate == "win_rate_055":
                allow = float(np.mean(np.asarray(recent) > 0.0)) >= 0.55
            else:
                raise ValueError(gate)
        if allow:
            enabled += 1
        row = out.loc[idx]
        base_side = str(row.get("micro_book_consensus_up_side", "skip"))
        name = f"micro_history_{gate}"
        out.at[idx, f"{name}_side"] = base_side if allow else "skip"
        out.at[idx, f"{name}_minute"] = 2
        out.at[idx, f"{name}_fraction"] = 0.04
        out.at[idx, f"{name}_component"] = f"micro_history_{gate}"
        result = prior_net_return(row, fixed_slippage)
        if result is not None:
            history.append(float(result))
    return out, {"gate": gate, "min_history": min_history, "lookback": lookback, "decision_rows": decisions, "enabled_rows": enabled, "history_observations": len(history)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--slippages", default="0.0025,0.0050")
    parser.add_argument("--min-history", type=int, default=20)
    parser.add_argument("--lookback", type=int, default=100)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--min-order-mode", choices=("notional", "shares", "both"), default="notional")
    parser.add_argument("--min-order-shares", type=float, default=5.0)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), ["micro_book_consensus_up"])
    test_runs = runs[4:]
    rows = []
    trade_logs = []
    diagnostics = []
    for gate in GATES:
        features, diag = attach_gate(base, gate, args.min_history, args.lookback)
        diagnostics.append(diag)
        test = features[features["run_name"].isin(test_runs)].copy()
        strategy = f"micro_history_{gate}"
        for slip in slippages:
            logs, metrics = study.simulate(
                test,
                strategy,
                study.ExecutionConfig(fixed_slippage=slip),
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
            rows.append({
                "gate": gate,
                "slippage": slip,
                "ending_bankroll": float(metrics["ending_bankroll"]),
                "total_return": float(metrics["total_return"]),
                "trades": int(metrics["trades"]),
                "max_drawdown": float(metrics["max_drawdown"]),
                "profit_factor": float(metrics["profit_factor"]),
                "win_rate": float(metrics["win_rate"]),
            })
            if not logs.empty:
                logs = logs.copy()
                logs["gate"] = gate
                logs["slippage"] = slip
                trade_logs.append(logs)
    result = pd.DataFrame(rows).sort_values(["slippage", "ending_bankroll"], ascending=[True, False]).reset_index(drop=True)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    if trade_logs:
        pd.concat(trade_logs, ignore_index=True).to_csv(out.with_name(out.stem + "_trade_logs.csv"), index=False)
    blocks = []
    if trade_logs:
        all_logs = pd.concat(trade_logs, ignore_index=True)
        block_ranges = [("first20", 0, 20), ("next20", 20, 40), ("next20b", 40, 60), ("last19", 60, 79)]
        run_index = {name: i for i, name in enumerate(test_runs)}
        all_logs["test_index"] = all_logs["run_name"].map(run_index)
        for gate in GATES:
            for slip in slippages:
                sub = all_logs[(all_logs["gate"] == gate) & (all_logs["slippage"] == slip)].copy()
                for block, start, end in block_ranges:
                    part = sub[sub["test_index"].between(start, end - 1)]
                    blocks.append({
                        "gate": gate, "slippage": slip, "block": block,
                        "start_test_index": start, "end_test_index": end - 1,
                        "trades": int(len(part)),
                        "ending_bankroll": float(part["bankroll_after"].iloc[-1]) if not part.empty else float("nan"),
                        "sum_pnl": float(part["pnl_usd"].sum()) if not part.empty else 0.0,
                        "win_rate": float((part["pnl_usd"] > 0).mean()) if not part.empty else float("nan"),
                    })
        pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    meta = {
        "complete_runs": int(len(runs)), "test_runs": int(len(test_runs)), "gates": list(GATES),
        "min_history": args.min_history, "lookback": args.lookback, "slippages": slippages,
        "bankroll0": args.bankroll0, "min_cost": args.min_cost, "min_order_mode": args.min_order_mode,
        "min_order_shares": args.min_order_shares, "history_slippage": 0.0025,
        "continuous_state": True, "causal_history_only": True, "old_clean_dataset_only": True,
        "new_data_collected": False, "live_orders_enabled": False, "diagnostics": diagnostics,
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()

"""Compare a frozen four-minute persistence rule with the frozen early-drop gate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_robust_gate_sweep import attach_robust_gate
from classic_min_order_chain import prepare
from execution_envelope_depth_sweep import bootstrap_run_pnl
from v1_persisted_4m_entry_walk_forward import apply_persisted_entry


PROFILES = {
    "baseline": {"min_depth": 100.0, "max_spread": 0.04, "max_overround": 0.03, "max_adverse_slippage": 0.015},
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--selection-slippage", type=float, default=0.005)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--selection-runs", type=int, default=20)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--replicates", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]

    candidates = ["classic_early_drop_down", "v1_conservative_mix", "v1_adaptive_mix"]
    base, runs = prepare(Path(args.source_root), candidates)
    test_runs = runs[4:]
    prefix_runs = test_runs[: args.selection_runs]

    early_features, early_diag = attach_robust_gate(base, "mean_positive", min_history=20, lookback=20)
    prepared = {
        "early_drop_history_net_positive_cooldown12": (
            early_features,
            "classic_early_drop_down_mean_positive",
            early_diag,
            "classic_early_drop_down",
        ),
    }
    for strategy in ("v1_conservative_mix", "v1_adaptive_mix"):
        persisted = apply_persisted_entry(base, strategy)
        prepared[f"{strategy}_persisted_4m_cooldown12"] = (
            persisted,
            strategy,
            {"candidate": strategy, "rule": "buy_down minute-2 signal retained only when btc_move_4m < 0; entry minute moved to 4"},
            strategy,
        )

    selection_rows = []
    envelope = PROFILES["baseline"]
    for label, (features, strategy, diagnostic, _) in prepared.items():
        diagnostic_fields = {f"diagnostic_{key}": value for key, value in diagnostic.items() if key != "candidate"}
        prefix = features[features["run_name"].isin(prefix_runs)]
        _, metrics = simulate_cash_locked(
            prefix, strategy, study.ExecutionConfig(fixed_slippage=args.selection_slippage, **envelope),
            args.bankroll0, args.min_cost, 0.0, 12,
        )
        selection_rows.append({
            "candidate": label, "selection_slippage": args.selection_slippage, "selection_runs": args.selection_runs,
            "selection_score": float(metrics["net_return"] - 0.5 * metrics["max_cash_drawdown"]),
            **envelope, **metrics, **diagnostic_fields,
        })
    selection = pd.DataFrame(selection_rows).sort_values(["selection_score", "ending_cash"], ascending=[False, False]).reset_index(drop=True)
    selected = str(selection.iloc[0]["candidate"])

    rows, blocks, confidence = [], [], []
    order = {name: i for i, name in enumerate(test_runs)}
    for label, (features, strategy, diagnostic, _) in prepared.items():
        score = float(selection.loc[selection["candidate"] == label, "selection_score"].iloc[0])
        frame = features[features["run_name"].isin(test_runs)]
        for slip in slippages:
            logs, metrics = simulate_cash_locked(
                frame, strategy, study.ExecutionConfig(fixed_slippage=slip, **envelope),
                args.bankroll0, args.min_cost, 0.0, 12,
            )
            diagnostic_fields = {f"diagnostic_{key}": value for key, value in diagnostic.items() if key != "candidate"}
            rows.append({"candidate": label, "slippage": slip, "selected_by_prefix": label == selected, "selection_score": score, **envelope, **metrics, **diagnostic_fields})
            logged = logs.copy()
            if not logged.empty:
                logged["test_index"] = logged["run_name"].map(order)
            for block, lo, hi in [("selection20", 0, args.selection_runs), ("holdout59", args.selection_runs, len(test_runs))]:
                part = logged[logged["test_index"].between(lo, hi - 1)] if not logged.empty else logged
                blocks.append({
                    "candidate": label, "slippage": slip, "block": block, "trades": int(len(part)),
                    "sum_pnl": float(part["pnl_usd"].sum()) if not part.empty else 0.0,
                    "win_rate": float((part["pnl_usd"] > 0).mean()) if not part.empty else float("nan"),
                })
            confidence.append({"candidate": label, "slippage": slip, **bootstrap_run_pnl(logs, test_runs, args.bankroll0, args.replicates, args.seed)})

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(out.with_name(out.stem + "_selection.csv"), index=False)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    pd.DataFrame(confidence).to_csv(out.with_name(out.stem + "_confidence.csv"), index=False)
    meta = {
        "format": "old-data-persisted-transition-prefix-selection-v1",
        "source_root": str(Path(args.source_root)), "complete_runs": len(runs), "test_runs": len(test_runs),
        "selection_runs": args.selection_runs, "holdout_runs": len(test_runs) - args.selection_runs,
        "candidates": list(prepared), "selected_candidate": selected,
        "selection_rule": "prefix net_return - 0.5 * max_cash_drawdown at fixed 0.50% slippage",
        "persistence_rule": "existing v1 buy-down minute-2 signals move to minute 4 only when btc_move_4m < 0; otherwise skipped",
        "execution_profile": envelope, "slippages": slippages, "selection_slippage": args.selection_slippage,
        "bankroll0": args.bankroll0, "min_cost": args.min_cost, "cooldown_events": 12,
        "promotion_rule": "candidate may replace early-drop baseline only if holdout PnL is nonnegative at every tested slippage and q025 is at least $5 at 1.00% slippage",
        "old_clean_dataset_only": True, "new_data_collected": False, "causal_history_only": True,
        "cash_release_delay_seconds": 0.0, "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "official_sources": [
            "https://docs.polymarket.com/market-data/market-details#trading-constraints",
            "https://docs.polymarket.com/trading/place-orders",
            "https://docs.polymarket.com/trading/fees",
        ],
        "interpretation": "predeclared transition-persistence paper comparison; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(selection.to_string(index=False))
    print(pd.DataFrame(rows).to_string(index=False))
    print(pd.DataFrame(confidence).to_string(index=False))


if __name__ == "__main__":
    main()

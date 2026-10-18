"""Evaluate a predeclared session gate on the causal minute-one entry."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import v1_execution_walk_forward as study
from causal_entry_gate_prefix_selection import CAUSAL_CANDIDATE, PROFILES, add_causal_candidate, prepare_once
from causal_registered_gate_sweep import attach_gate
from cash_release_sensitivity import simulate_cash_locked
from compound_old_clean_replay import _paper_events
from execution_envelope_depth_sweep import bootstrap_run_pnl
from sequential_compound_gate import evaluate_compound_chain


SESSION_CANDIDATE = "classic_early_drop_down_causal_1m_spread_london_us_afternoon"
ALLOWED_SESSIONS = ("london", "us_afternoon")


def add_session_candidate(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    base_signal = out[f"{CAUSAL_CANDIDATE}_side"].astype(str).eq("buy_down")
    signal = base_signal & out["session_et"].astype(str).isin(ALLOWED_SESSIONS)
    out[f"{SESSION_CANDIDATE}_side"] = np.where(signal, "buy_down", "skip")
    out[f"{SESSION_CANDIDATE}_minute"] = 1
    out[f"{SESSION_CANDIDATE}_fraction"] = np.where(signal, 0.04, 0.0)
    out[f"{SESSION_CANDIDATE}_component"] = np.where(signal, "early_drop_down_causal_1m_session", "none")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--selection-slippage", type=float, default=0.005)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--selection-runs", type=int, default=20)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--cash-release-delay-seconds", type=float, default=600.0)
    parser.add_argument("--replicates", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261024)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    source_root = Path(args.source_root)
    features, runs = prepare_once(source_root)
    features = add_session_candidate(add_causal_candidate(features))
    test_runs = runs[4:]
    prefix_runs = test_runs[: args.selection_runs]
    envelope = PROFILES["baseline"]

    prepared = {}
    for label, candidate, rule in [
        ("causal_1m_spread", CAUSAL_CANDIDATE, "btc_move_1m <= -20 and spread_down_1m <= 0.06"),
        ("causal_1m_spread_london_us_afternoon", SESSION_CANDIDATE, "base rule and session_et in {london, us_afternoon}"),
    ]:
        gated, diagnostic = attach_gate(features, candidate, min_history=20, lookback=20)
        prepared[label] = (gated, f"{candidate}_history_net_positive", {"signal_rule": rule, **diagnostic})

    selection_rows = []
    for label, (frame, strategy, diagnostic) in prepared.items():
        _, metrics = simulate_cash_locked(
            frame[frame["run_name"].isin(prefix_runs)], strategy,
            study.ExecutionConfig(fixed_slippage=args.selection_slippage, **envelope),
            args.bankroll0, args.min_cost, args.cash_release_delay_seconds, 12, capture_logs=True,
        )
        selection_rows.append({
            "candidate": label, "selection_slippage": args.selection_slippage,
            "selection_runs": args.selection_runs,
            "selection_score": float(metrics["net_return"] - 0.5 * metrics["max_cash_drawdown"]),
            **envelope, **metrics,
            **{f"diagnostic_{k}": v for k, v in diagnostic.items()},
        })
    selection = pd.DataFrame(selection_rows).sort_values(["selection_score", "ending_cash"], ascending=[False, False]).reset_index(drop=True)
    selected = str(selection.iloc[0]["candidate"])

    rows, blocks, confidence = [], [], []
    for label, (frame, strategy, diagnostic) in prepared.items():
        score = float(selection.loc[selection["candidate"] == label, "selection_score"].iloc[0])
        for slip in slippages:
            for split, names in (("selection20", prefix_runs), ("holdout59", test_runs[args.selection_runs:])):
                logs, metrics = simulate_cash_locked(
                    frame[frame["run_name"].isin(names)], strategy,
                    study.ExecutionConfig(fixed_slippage=slip, **envelope),
                    args.bankroll0, args.min_cost, args.cash_release_delay_seconds, 12, capture_logs=True,
                )
                chain = evaluate_compound_chain(_paper_events(logs), initial_cash=args.bankroll0, minimum_order=args.min_cost, required_fills=10, evidence_mode="paper_upper_bound", stop_after_required_fills=True)
                rows.append({"candidate": label, "split": split, "slippage": slip, "selected_by_prefix": label == selected, "selection_score": score, "paper_chain_fills": int(chain["fills"]), "paper_chain_ending_cash": float(chain["ending_cash"]), "paper_chain_minimum_cash": float(chain["minimum_cash"]), "paper_chain_passed": bool(chain["paper_replay_passed"]), "paper_chain_failure_reason": chain["failure_reason"], **envelope, **metrics, **{f"diagnostic_{k}": v for k, v in diagnostic.items()}})
                blocks.append({"candidate": label, "split": split, "slippage": slip, "trades": int(len(logs)), "sum_pnl": float(logs["pnl_usd"].sum()) if not logs.empty else 0.0, "win_rate": float((logs["pnl_usd"] > 0).mean()) if not logs.empty else float("nan"), "paper_chain_fills": int(chain["fills"]), "paper_chain_ending_cash": float(chain["ending_cash"]), "paper_chain_passed": bool(chain["paper_replay_passed"])})
                confidence.append({"candidate": label, "split": split, "slippage": slip, **bootstrap_run_pnl(logs, names, args.bankroll0, args.replicates, args.seed)})

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(out.with_name(out.stem + "_selection.csv"), index=False)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    pd.DataFrame(confidence).to_csv(out.with_name(out.stem + "_confidence.csv"), index=False)
    meta = {
        "format": "old-data-causal-entry-session-prefix-selection-v2", "source_root": str(source_root),
        "complete_runs": len(runs), "test_runs": len(test_runs), "selection_runs": args.selection_runs,
        "holdout_runs": len(test_runs) - args.selection_runs, "candidates": list(prepared), "selected_candidate": selected,
        "selection_rule": "prefix net_return - 0.5 * max_cash_drawdown at fixed 0.50% slippage",
        "causal_base": "btc_move_1m <= -20 and spread_down_1m <= 0.06; enter at minute 1",
        "session_hypothesis": "retain only London and US afternoon sessions at the same minute-one signal",
        "allowed_sessions": list(ALLOWED_SESSIONS), "execution_profile": envelope, "slippages": slippages,
        "selection_slippage": args.selection_slippage, "bankroll0": args.bankroll0, "min_cost": args.min_cost,
        "cooldown_events": 12, "fee_contract": "polymarket_taker_rate_0.07_exponent_1.0",
        "promotion_rule": "candidate may replace causal base only if holdout PnL is nonnegative at every tested slippage and q025 is at least $5 at 1.00% slippage",
        "old_clean_dataset_only": True, "new_data_collected": False, "causal_history_only": True,
        "cash_release_delay_seconds": args.cash_release_delay_seconds, "independent_split_ledgers": True, "compound_chain_contract": "ten chronological paper rows per fresh split ledger; no selection-to-holdout bankroll carryover", "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "official_sources": ["https://docs.polymarket.com/market-data/market-details#trading-constraints", "https://docs.polymarket.com/trading/place-orders", "https://docs.polymarket.com/trading/fees"],
        "interpretation": "predeclared timestamp-causal session paper comparison; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True)); print(selection.to_string(index=False)); print(pd.DataFrame(rows).to_string(index=False)); print(pd.DataFrame(confidence).to_string(index=False))


if __name__ == "__main__":
    main()

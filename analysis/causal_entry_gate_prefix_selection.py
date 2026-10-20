"""Compare the frozen early-drop gate with a timestamp-causal entry filter.

The historical ``classic_early_drop_down`` signal enters at minute one but
its signal gate used the median Down spread through minute two.  This script
predeclares one causal correction: keep the same BTC drop and the same 0.06
spread threshold, but read the Down spread at the minute-one snapshot only.
Both candidates then receive their own causal history-net-positive lookback
gate and are evaluated with the already frozen cash, fee, cooldown and
execution contract.  This is an old-clean-dataset paper diagnostic; it never
submits an order and cannot produce canary evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1
import monthly_runs_quant_research_framework as qrf
import v1_execution_walk_forward as study
from causal_registered_gate_sweep import attach_gate
from cash_release_sensitivity import simulate_cash_locked
from compound_old_clean_replay import _paper_events
from execution_envelope_depth_sweep import bootstrap_run_pnl
from quantml_candidate_execution_walk_forward import attach_execution_snapshots
from sequential_compound_gate import evaluate_compound_chain


BASE_CANDIDATE = "classic_early_drop_down"
CAUSAL_CANDIDATE = "classic_early_drop_down_causal_1m_spread"
CAUSAL_THRESHOLD = 0.06
PROFILES = {
    "baseline": {
        "min_depth": 100.0,
        "max_spread": 0.04,
        "max_overround": 0.03,
        "max_adverse_slippage": 0.015,
    }
}


def prepare_once(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    """Build the normal feature frame and the causal field in one raw pass."""
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    quotes = base.prepare_quotes(raw[raw["run_name"].astype(str).isin(runs)].copy())
    features = base.build_features(quotes)
    features["pred_prob_up_logit"] = qrf.rolling_logit_safe(features)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    rows: list[dict[str, object]] = []
    for market_id, group in quotes.groupby("market_id", dropna=False):
        group = group.sort_values("ts_utc")
        row = base.snapshot_row(group, 1)
        spread = np.nan if row is None else pd.to_numeric(row.get("spread_down_cents"), errors="coerce") / 100.0
        rows.append({"market_id": market_id, "spread_down_1m": spread})
    features = features.merge(pd.DataFrame(rows), on="market_id", how="left", validate="one_to_one")
    choices = features.apply(lambda row: qrf.choose_trade(row, BASE_CANDIDATE), axis=1)
    features[f"{BASE_CANDIDATE}_side"] = choices.map(lambda x: x[0])
    features[f"{BASE_CANDIDATE}_minute"] = choices.map(lambda x: x[1]).astype(int)
    features[f"{BASE_CANDIDATE}_fraction"] = choices.map(lambda x: x[2]).fillna(0.0).astype(float)
    features[f"{BASE_CANDIDATE}_component"] = choices.map(lambda x: x[3])
    return features, runs


def add_causal_candidate(features: pd.DataFrame) -> pd.DataFrame:
    """Create a minute-one-only signal with the same drop and spread cutoff."""
    out = features.copy()
    signal = (
        pd.to_numeric(out["btc_move_1m"], errors="coerce").le(-20.0)
        & pd.to_numeric(out["spread_down_1m"], errors="coerce").le(CAUSAL_THRESHOLD)
    )
    out[f"{CAUSAL_CANDIDATE}_side"] = np.where(signal, "buy_down", "skip")
    out[f"{CAUSAL_CANDIDATE}_minute"] = 1
    out[f"{CAUSAL_CANDIDATE}_fraction"] = np.where(signal, 0.04, 0.0)
    out[f"{CAUSAL_CANDIDATE}_component"] = np.where(signal, "early_drop_down_causal_1m_spread", "none")
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
    parser.add_argument("--seed", type=int, default=20261022)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    source_root = Path(args.source_root)

    base_features, runs = prepare_once(source_root)
    base_features = add_causal_candidate(base_features)
    test_runs = runs[4:]
    prefix_runs = test_runs[: args.selection_runs]
    envelope = PROFILES["baseline"]

    prepared: dict[str, tuple[pd.DataFrame, str, dict[str, object]]] = {}
    baseline_gated, baseline_diag = attach_gate(
        base_features, BASE_CANDIDATE, min_history=20, lookback=20
    )
    prepared["baseline_2m_spread"] = (
        baseline_gated,
        f"{BASE_CANDIDATE}_history_net_positive",
        {"signal_rule": "btc_move_1m <= -20 and spread_down_median_first2m <= 0.06", **baseline_diag},
    )
    causal_gated, causal_diag = attach_gate(
        base_features, CAUSAL_CANDIDATE, min_history=20, lookback=20
    )
    prepared["causal_1m_spread"] = (
        causal_gated,
        f"{CAUSAL_CANDIDATE}_history_net_positive",
        {"signal_rule": "btc_move_1m <= -20 and spread_down_1m <= 0.06", **causal_diag},
    )

    selection_rows: list[dict[str, object]] = []
    for label, (frame, strategy, diagnostic) in prepared.items():
        prefix = frame[frame["run_name"].isin(prefix_runs)]
        prefix_logs, metrics = simulate_cash_locked(
            prefix,
            strategy,
            study.ExecutionConfig(fixed_slippage=args.selection_slippage, **envelope),
            args.bankroll0,
            args.min_cost,
            args.cash_release_delay_seconds,
            12,
            capture_logs=True,
        )
        prefix_chain = evaluate_compound_chain(
            _paper_events(prefix_logs),
            initial_cash=args.bankroll0,
            minimum_order=args.min_cost,
            required_fills=10,
            evidence_mode="paper_upper_bound",
            stop_after_required_fills=True,
        )
        selection_rows.append(
            {
                "candidate": label,
                "selection_slippage": args.selection_slippage,
                "selection_runs": args.selection_runs,
                "selection_score": float(metrics["net_return"] - 0.5 * metrics["max_cash_drawdown"]),
                "selection_compound_eligible": bool(prefix_chain["paper_replay_passed"]),
                "selection_compound_fills": int(prefix_chain["fills"]),
                "selection_compound_ending_cash": float(prefix_chain["ending_cash"]),
                "selection_compound_failure_reason": prefix_chain["failure_reason"],
                **envelope,
                **metrics,
                **{f"diagnostic_{k}": v for k, v in diagnostic.items()},
            }
        )
    selection = pd.DataFrame(selection_rows).sort_values(
        ["selection_compound_eligible", "selection_score", "ending_cash"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    selected = str(selection.iloc[0]["candidate"]) if bool(selection.iloc[0]["selection_compound_eligible"]) else None

    rows: list[dict[str, object]] = []
    blocks: list[dict[str, object]] = []
    confidence: list[dict[str, object]] = []
    for label, (frame, strategy, diagnostic) in prepared.items():
        score = float(selection.loc[selection["candidate"] == label, "selection_score"].iloc[0])
        for slip in slippages:
            for split, names in (("selection20", prefix_runs), ("holdout59", test_runs[args.selection_runs:])):
                logs, metrics = simulate_cash_locked(
                    frame[frame["run_name"].isin(names)],
                    strategy,
                    study.ExecutionConfig(fixed_slippage=slip, **envelope),
                    args.bankroll0,
                    args.min_cost,
                    args.cash_release_delay_seconds,
                    12,
                    capture_logs=True,
                )
                chain = evaluate_compound_chain(
                    _paper_events(logs),
                    initial_cash=args.bankroll0,
                    minimum_order=args.min_cost,
                    required_fills=10,
                    evidence_mode="paper_upper_bound",
                    stop_after_required_fills=True,
                )
                rows.append(
                    {
                        "candidate": label,
                        "split": split,
                        "slippage": slip,
                        "selected_by_prefix": selected is not None and label == selected,
                        "selection_score": score,
                        "paper_chain_fills": int(chain["fills"]),
                        "paper_chain_ending_cash": float(chain["ending_cash"]),
                        "paper_chain_minimum_cash": float(chain["minimum_cash"]),
                        "paper_chain_passed": bool(chain["paper_replay_passed"]),
                        "paper_chain_failure_reason": chain["failure_reason"],
                        **envelope,
                        **metrics,
                        **{f"diagnostic_{k}": v for k, v in diagnostic.items()},
                    }
                )
                blocks.append(
                    {
                        "candidate": label,
                        "split": split,
                        "slippage": slip,
                        "trades": int(len(logs)),
                        "sum_pnl": float(logs["pnl_usd"].sum()) if not logs.empty else 0.0,
                        "win_rate": float((logs["pnl_usd"] > 0).mean()) if not logs.empty else float("nan"),
                        "paper_chain_fills": int(chain["fills"]),
                        "paper_chain_ending_cash": float(chain["ending_cash"]),
                        "paper_chain_passed": bool(chain["paper_replay_passed"]),
                    }
                )
                confidence.append({
                    "candidate": label,
                    "split": split,
                    "slippage": slip,
                    **bootstrap_run_pnl(logs, names, args.bankroll0, args.replicates, args.seed),
                })

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(out.with_name(out.stem + "_selection.csv"), index=False)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    pd.DataFrame(confidence).to_csv(out.with_name(out.stem + "_confidence.csv"), index=False)
    meta = {
        "format": "old-data-causal-entry-gate-prefix-selection-v2",
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "test_runs": len(test_runs),
        "selection_runs": args.selection_runs,
        "holdout_runs": len(test_runs) - args.selection_runs,
        "candidates": list(prepared),
        "selected_candidate": selected,
        "selection_rule": "compound-eligible prefix first, then net_return - 0.5 * max_cash_drawdown at fixed 0.50% slippage",
        "causal_hypothesis": "replace the minute-two median Down spread gate with the minute-one Down spread at the minute-one entry",
        "spread_threshold": CAUSAL_THRESHOLD,
        "execution_profile": envelope,
        "slippages": slippages,
        "selection_slippage": args.selection_slippage,
        "bankroll0": args.bankroll0,
        "min_cost": args.min_cost,
        "cooldown_events": 12,
        "fee_contract": "polymarket_taker_rate_0.07_exponent_1.0",
        "promotion_rule": "candidate may replace baseline only if holdout PnL is nonnegative at every tested slippage and q025 is at least $5 at 1.00% slippage",
        "old_clean_dataset_only": True,
        "new_data_collected": False,
        "causal_history_only": True,
        "cash_release_delay_seconds": args.cash_release_delay_seconds,
        "independent_split_ledgers": True,
        "compound_chain_contract": "ten chronological paper rows per fresh split ledger; no selection-to-holdout bankroll carryover",
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "official_sources": [
            "https://docs.polymarket.com/market-data/market-details#trading-constraints",
            "https://docs.polymarket.com/trading/place-orders",
            "https://docs.polymarket.com/trading/fees",
        ],
        "interpretation": "predeclared timestamp-causal paper comparison; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(selection.to_string(index=False))
    print(pd.DataFrame(rows).to_string(index=False))
    print(pd.DataFrame(confidence).to_string(index=False))


if __name__ == "__main__":
    main()

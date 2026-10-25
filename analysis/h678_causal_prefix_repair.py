"""Evaluate the preregistered H678 causal-prefix repair on the old-clean data.

This is a paper-only comparison.  The candidate signal is built only from
minute-one observations; execution snapshots are delayed four seconds from
that same minute-one entry.  No four-minute feature is used by the candidate,
history gate, or cash replay.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1
import v1_execution_walk_forward as study
from causal_registered_gate_sweep import attach_gate
from cash_release_sensitivity import simulate_cash_locked
from compound_old_clean_replay import _paper_events
from execution_envelope_depth_sweep import bootstrap_run_pnl
from quantml_candidate_execution_walk_forward import attach_execution_snapshots
from sequential_compound_gate import evaluate_compound_chain


LEGACY = "legacy_2m_spread"
CAUSAL = "causal_1m_spread"
CAUSAL_THRESHOLD = 0.06
FEATURE_ALLOWLIST = (
    "btc_move_1m", "btc_move_2m", "mid_up_prob_open", "mid_up_prob_2m",
    "mid_up_prob_change_2m", "buy_up_price_2m", "buy_down_price_2m",
    "buy_up_size_2m", "buy_down_size_2m", "sell_up_size_2m",
    "sell_down_size_2m", "size_imbalance_updown_2m",
    "sell_size_imbalance_updown_2m", "bid_depth_imbalance_updown_2m",
    "book_pressure_up_2m", "book_pressure_down_2m",
    "spread_up_median_first2m", "spread_down_median_first2m",
    "overround_median_first2m", "quote_count_first2m", "trade_count_sum_first2m",
    "trade_volume_sum_first2m", "realized_vol_first2m", "path_efficiency_first2m",
    "max_drawdown_first2m", "max_rebound_first2m",
)


def _minute_one_spread(quotes: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for market_id, group in quotes.groupby("market_id", dropna=False):
        group = group.sort_values("ts_utc")
        first = group["ts_utc"].min()
        snap = group[group["ts_utc"] <= first + pd.Timedelta(minutes=1)]
        row = snap.iloc[-1] if not snap.empty else None
        spread = np.nan if row is None else pd.to_numeric(row.get("spread_down_cents"), errors="coerce") / 100.0
        rows.append({"market_id": market_id, "spread_down_1m": spread})
    return pd.DataFrame(rows)


def prepare_once(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    # Strict quality is the admission gate; no model fit or future-feature
    # probability is computed in this repair.
    features = base.build_features(quotes, strict_quality=True, max_snapshot_age_seconds=90.0)
    features = v1.add_session_labels(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1,))
    features = features.merge(_minute_one_spread(quotes), on="market_id", how="left", validate="one_to_one")
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)

    legacy_signal = (
        pd.to_numeric(features["btc_move_1m"], errors="coerce").le(-20.0)
        & pd.to_numeric(features["spread_down_median_first2m"], errors="coerce").le(CAUSAL_THRESHOLD)
    )
    causal_signal = (
        pd.to_numeric(features["btc_move_1m"], errors="coerce").le(-20.0)
        & pd.to_numeric(features["spread_down_1m"], errors="coerce").le(CAUSAL_THRESHOLD)
    )
    for name, signal, component in (
        (LEGACY, legacy_signal, "early_drop_down_2m_spread"),
        (CAUSAL, causal_signal, "early_drop_down_1m_spread"),
    ):
        features[f"{name}_side"] = np.where(signal, "buy_down", "skip")
        features[f"{name}_minute"] = 1
        features[f"{name}_fraction"] = np.where(signal, 0.04, 0.0)
        features[f"{name}_component"] = np.where(signal, component, "none")
    return features, runs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--selection-slippage", type=float, default=0.005)
    parser.add_argument("--slippages", default="0.0025,0.0050,0.0100")
    parser.add_argument("--selection-runs", type=int, default=20)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--cash-release-delay-seconds", type=float, default=900.0)
    parser.add_argument("--replicates", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=20261025)
    args = parser.parse_args()
    slippages = [float(x.strip()) for x in args.slippages.split(",") if x.strip()]
    features, runs = prepare_once(Path(args.source_root))
    test_runs = runs[4:]
    prefix_runs = test_runs[: args.selection_runs]
    envelope = {"min_depth": 100.0, "max_spread": 0.04, "max_overround": 0.03, "max_adverse_slippage": 0.015}

    prepared: dict[str, tuple[pd.DataFrame, str, dict[str, object]]] = {}
    for label, candidate, rule in (
        (LEGACY, LEGACY, "btc_move_1m <= -20 and spread_down_median_first2m <= 0.06"),
        (CAUSAL, CAUSAL, "btc_move_1m <= -20 and spread_down_1m <= 0.06"),
    ):
        gated, diagnostic = attach_gate(features, candidate, min_history=20, lookback=20)
        prepared[label] = (gated, f"{candidate}_history_net_positive", {"signal_rule": rule, **diagnostic})

    selection_rows: list[dict[str, object]] = []
    for label, (frame, strategy, diagnostic) in prepared.items():
        logs, metrics = simulate_cash_locked(
            frame[frame["run_name"].isin(prefix_runs)], strategy,
            study.ExecutionConfig(fixed_slippage=args.selection_slippage, **envelope),
            args.bankroll0, args.min_cost, args.cash_release_delay_seconds, 12, capture_logs=True,
        )
        chain = evaluate_compound_chain(_paper_events(logs), initial_cash=args.bankroll0, minimum_order=args.min_cost, required_fills=10, evidence_mode="paper_upper_bound", stop_after_required_fills=True)
        selection_rows.append({"candidate": label, "selection_score": float(metrics["net_return"] - 0.5 * metrics["max_cash_drawdown"]), "selection_compound_eligible": bool(chain["paper_replay_passed"]), "selection_compound_fills": int(chain["fills"]), "selection_compound_ending_cash": float(chain["ending_cash"]), "selection_compound_failure_reason": chain["failure_reason"], **diagnostic, **metrics})
    selection = pd.DataFrame(selection_rows).sort_values(["selection_compound_eligible", "selection_score", "ending_cash"], ascending=[False, False, False]).reset_index(drop=True)
    selected = str(selection.iloc[0]["candidate"]) if bool(selection.iloc[0]["selection_compound_eligible"]) else None

    rows: list[dict[str, object]] = []
    blocks: list[dict[str, object]] = []
    confidence: list[dict[str, object]] = []
    for label, (frame, strategy, diagnostic) in prepared.items():
        score = float(selection.loc[selection["candidate"] == label, "selection_score"].iloc[0])
        for slip in slippages:
            for split, names in (("selection20", prefix_runs), ("holdout59", test_runs[args.selection_runs:])):
                logs, metrics = simulate_cash_locked(frame[frame["run_name"].isin(names)], strategy, study.ExecutionConfig(fixed_slippage=slip, **envelope), args.bankroll0, args.min_cost, args.cash_release_delay_seconds, 12, capture_logs=True)
                chain = evaluate_compound_chain(_paper_events(logs), initial_cash=args.bankroll0, minimum_order=args.min_cost, required_fills=10, evidence_mode="paper_upper_bound", stop_after_required_fills=True)
                rows.append({"candidate": label, "split": split, "slippage": slip, "selected_by_prefix": selected is not None and label == selected, "selection_score": score, "paper_chain_fills": int(chain["fills"]), "paper_chain_ending_cash": float(chain["ending_cash"]), "paper_chain_minimum_cash": float(chain["minimum_cash"]), "paper_chain_passed": bool(chain["paper_replay_passed"]), "paper_chain_failure_reason": chain["failure_reason"], **metrics})
                blocks.append({"candidate": label, "split": split, "slippage": slip, "trades": int(len(logs)), "sum_pnl": float(logs["pnl_usd"].sum()) if not logs.empty else 0.0, "win_rate": float((logs["pnl_usd"] > 0).mean()) if not logs.empty else float("nan"), "paper_chain_fills": int(chain["fills"]), "paper_chain_ending_cash": float(chain["ending_cash"]), "paper_chain_passed": bool(chain["paper_replay_passed"])})
                confidence.append({"candidate": label, "split": split, "slippage": slip, **bootstrap_run_pnl(logs, names, args.bankroll0, args.replicates, args.seed)})

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(out.with_name(out.stem + "_selection.csv"), index=False)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(blocks).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    pd.DataFrame(confidence).to_csv(out.with_name(out.stem + "_confidence.csv"), index=False)
    meta = {
        "format": "h678-causal-prefix-repair-v1", "complete_runs": len(runs), "test_runs": len(test_runs), "selection_runs": args.selection_runs,
        "holdout_runs": len(test_runs) - args.selection_runs, "candidates": list(prepared), "selected_candidate": selected,
        "selection_rule": "compound-eligible prefix first, then net_return - 0.5 * max_cash_drawdown at fixed 0.50% slippage",
        "causal_feature_allowlist": list(FEATURE_ALLOWLIST), "decision_cutoff_seconds": 120.0, "entry_delay_seconds": 4.0,
        "strict_quality": True, "maximum_snapshot_age_seconds": 90.0, "execution_profile": envelope,
        "slippages": slippages, "selection_slippage": args.selection_slippage, "bankroll0": args.bankroll0, "min_cost": args.min_cost,
        "cash_release_delay_seconds": args.cash_release_delay_seconds, "cooldown_events": 12,
        "fee_contract": "polymarket_taker_rate_0.07_exponent_1.0", "independent_split_ledgers": True,
        "compound_chain_contract": "ten chronological paper rows per fresh split ledger; no selection-to-holdout bankroll carryover",
        "old_clean_dataset_only": True, "new_data_collected": False, "new_model_fit_calls": 0,
        "causal_history_only": True, "future_feature_used_by_candidate": False,
        "promotion_rule": "holdout PnL nonnegative at every tested slippage and q025 at least $5 at 1.00%; then separately require authenticated owner-scoped fills",
        "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "interpretation": "predeclared timestamp-causal minute-one prefix comparison; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(selection.to_string(index=False))
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()

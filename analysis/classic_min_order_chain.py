"""Chronological $5-minimum-order benchmark for two predeclared old-data candidates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1
import monthly_runs_quant_research_framework as qrf
import v1_execution_walk_forward as study
from quantml_candidate_execution_walk_forward import attach_execution_snapshots

CANDIDATES = tuple(spec.name for spec in qrf.strategy_registry())


def prepare(source_root: Path, selected):
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    features["pred_prob_up_logit"] = qrf.rolling_logit_safe(features)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    for strategy in selected:
        choices = features.apply(lambda row, name=strategy: qrf.choose_trade(row, name), axis=1)
        features[f"{strategy}_side"] = choices.map(lambda x: x[0])
        features[f"{strategy}_minute"] = choices.map(lambda x: x[1]).astype(int)
        features[f"{strategy}_fraction"] = choices.map(lambda x: x[2]).fillna(0.0).astype(float)
        features[f"{strategy}_component"] = choices.map(lambda x: x[3])
    return features, runs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--fee", type=float, default=0.07)
    parser.add_argument("--candidates", default=",")
    args = parser.parse_args()
    selected = [x.strip() for x in args.candidates.split(",") if x.strip()] if args.candidates.strip(",") else list(CANDIDATES)
    unknown = sorted(set(selected) - set(CANDIDATES))
    if unknown:
        raise SystemExit(f"unknown candidates: {unknown}")
    features, runs = prepare(Path(args.source_root), selected)
    folds = study.make_run_folds(runs)
    config = study.ExecutionConfig()
    rows = []
    for strategy in selected:
        bankroll = float(args.bankroll0)
        for fold in folds:
            frame = features[features["run_name"] == fold["test_run"]].copy()
            _, metrics = study.simulate(
                frame,
                strategy,
                config,
                bankroll,
                args.min_cost,
                capture_logs=False,
                fee_mode="polymarket_taker",
                fee_rate=args.fee,
                fee_exponent=1.0,
                min_cost_policy="raise_to_min",
                min_order_mode="notional",
            )
            rows.append({
                "strategy": strategy,
                "fold_id": fold["fold_id"],
                "test_run": fold["test_run"],
                "starting_bankroll": bankroll,
                "ending_bankroll": metrics["ending_bankroll"],
                "total_return": metrics["total_return"],
                "trades": metrics["trades"],
                "max_drawdown": metrics["max_drawdown"],
                "win_rate": metrics["win_rate"],
            })
            bankroll = float(metrics["ending_bankroll"])
    result = pd.DataFrame(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    summary = result.groupby("strategy", as_index=False).agg(
        test_folds=("fold_id", "nunique"),
        ending_bankroll=("ending_bankroll", "last"),
        total_trades=("trades", "sum"),
        positive_folds=("total_return", lambda s: int((s > 0).sum())),
        zero_trade_folds=("trades", lambda s: int((s == 0).sum())),
        minimum_bankroll=("ending_bankroll", "min"),
        maximum_drawdown=("max_drawdown", "max"),
    )
    summary_path = out.with_name(out.stem + "_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(json.dumps({"complete_runs": len(runs), "fold_count": len(folds), "bankroll0": args.bankroll0, "min_cost": args.min_cost, "fee": args.fee}, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()

"""Efficient old-clean compound variants audit.

The feature tape is built once without the unrelated rolling-logit fit.  Ten
pre-registered price-cap variants then reuse that tape under the same
cash-locked executor and compound ledger.  This is paper evidence only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1
import monthly_runs_quant_research_framework as qrf
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from compound_old_clean_replay import _paper_events
from quantml_candidate_execution_walk_forward import attach_execution_snapshots
from sequential_compound_gate import evaluate_compound_chain


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
CAPS = (0.55, 0.60, 0.65, 0.70, 0.75)
FAMILIES = ("micro", "timeseries")
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
SLIPPAGE = 0.005
TAKER_FEE_RATE = 0.07
DELAY_SECONDS = 600.0


def prepare_fast(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    if len(runs) != 83:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    if features.empty:
        raise RuntimeError("NO_USABLE_COMPLETE_RUN_FEATURES")
    return features, runs


def add_base_signals(features: pd.DataFrame, family: str) -> tuple[pd.DataFrame, str]:
    base_name = "micro_book_consensus_up" if family == "micro" else "timeseries_momentum_up"
    out = features.copy()
    choices = out.apply(lambda row: qrf.choose_trade(row, base_name), axis=1)
    out[f"{base_name}_side"] = choices.map(lambda x: x[0])
    out[f"{base_name}_minute"] = choices.map(lambda x: x[1]).astype(int)
    out[f"{base_name}_fraction"] = choices.map(lambda x: x[2]).fillna(0.0).astype(float)
    out[f"{base_name}_component"] = choices.map(lambda x: x[3])
    return out, base_name


def add_variant(base_frame: pd.DataFrame, base_name: str, family: str, cap: float) -> tuple[pd.DataFrame, str]:
    out = base_frame.copy()
    name = f"{family}_price_cap_{cap:.2f}".replace(".", "p")
    keep = out[f"{base_name}_side"].astype(str).eq("buy_up") & (pd.to_numeric(out["buy_up_price_2m"], errors="coerce") <= cap)
    out[f"{name}_side"] = out[f"{base_name}_side"].where(keep, "skip")
    out[f"{name}_minute"] = out[f"{base_name}_minute"]
    out[f"{name}_fraction"] = out[f"{base_name}_fraction"]
    out[f"{name}_component"] = f"{family}_price_cap"
    return out, name


def run_split(frame: pd.DataFrame, strategy: str, names: list[str]) -> dict[str, object]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].isin(names)], strategy,
        study.ExecutionConfig(fixed_slippage=SLIPPAGE), INITIAL_CASH,
        MINIMUM_ORDER, DELAY_SECONDS, cooldown_events=0, capture_logs=True,
        fee_mode="polymarket_taker", fee_rate=TAKER_FEE_RATE,
    )
    chain = evaluate_compound_chain(
        _paper_events(logs), initial_cash=INITIAL_CASH,
        minimum_order=MINIMUM_ORDER, required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        "paper_fills": int(metrics["trades"]),
        "paper_ending_cash": float(metrics["ending_cash"]),
        "paper_chain_fills": int(chain["fills"]),
        "paper_chain_ending_cash": float(chain["ending_cash"]),
        "paper_chain_minimum_cash": float(chain["minimum_cash"]),
        "paper_chain_passed": bool(chain["paper_replay_passed"]),
        "chain_failure_reason": chain["failure_reason"],
    }


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    if source_commit != SOURCE_COMMIT:
        raise RuntimeError("UNEXPECTED_SOURCE_COMMIT")
    features, runs = prepare_fast(source_root)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    rows: list[dict[str, object]] = []
    base_frames = {family: add_base_signals(features, family) for family in FAMILIES}
    for family in FAMILIES:
        base_frame, base_name = base_frames[family]
        for cap in CAPS:
            frame, name = add_variant(base_frame, base_name, family, cap)
            for split, names in (("selection", selection_runs), ("holdout", holdout_runs), ("all_test", test_runs)):
                rows.append({"family": family, "cap": cap, "strategy": name, "split": split, **run_split(frame, name, names)})
    table = pd.DataFrame(rows)
    eligible = table[(table["split"] == "selection") & table["paper_chain_fills"].ge(10) & table["paper_chain_ending_cash"].ge(INITIAL_CASH)]
    selected = None if eligible.empty else str(eligible.sort_values(["paper_chain_ending_cash", "paper_chain_fills", "strategy"], ascending=[False, False, True]).iloc[0]["strategy"])
    selected_holdout = table[(table["strategy"] == selected) & (table["split"] == "holdout")] if selected else pd.DataFrame()
    holdout_passed = bool(not selected_holdout.empty and selected_holdout.iloc[0]["paper_chain_fills"] >= 10 and selected_holdout.iloc[0]["paper_chain_ending_cash"] >= INITIAL_CASH)
    output.mkdir(parents=True, exist_ok=True)
    table.to_csv(output / "variant_metrics.csv", index=False)
    meta = {
        "format": "old-clean-compound-price-cap-variants-v1",
        "source_commit": source_commit, "complete_runs": len(runs),
        "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs),
        "families": list(FAMILIES), "caps": list(CAPS),
        "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER, "slippage": SLIPPAGE, "taker_fee_rate": TAKER_FEE_RATE, "cash_release_delay_seconds": DELAY_SECONDS, "required_consecutive_fills": 10},
        "selected_strategy": selected, "selection_eligible_candidates": int(len(eligible)), "selected_holdout_passed": holdout_passed,
        "paper_only": True, "new_data_collected": False, "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "evidence_boundary": "Old-clean paper labels and quote-derived execution only; no authenticated order, fill, settlement, cash-release or balance receipt.",
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()

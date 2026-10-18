"""Pre-registered strategy-registry search under the strict small-cash contract.

The strategy names and signal rules come from the existing quant research
registry.  Selection is restricted to the first 20 test runs; the remaining
59 runs are never used to choose a candidate and are split into five
chronological holdout blocks.  This is old-data paper evidence only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import monthly_runs_quant_research_framework as qrf
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from quantml_candidate_execution_walk_forward import attach_execution_snapshots


EXPECTED_COMPLETE_RUNS = 83
SELECTION_RUNS = 20
BLOCK_SIZES = (12, 12, 12, 12, 11)
SLIPPAGE = 0.005
DELAY_SECONDS = 600.0
MINIMUM_ORDER = 5.0
INITIAL_CASH = 10.0
TAKER_FEE_RATE = 0.07
REGISTRY_NAMES = tuple(spec.name for spec in qrf.strategy_registry())


def holdout_blocks(runs: list[str]) -> list[list[str]]:
    if len(runs) != sum(BLOCK_SIZES):
        raise ValueError("BLOCK_INPUT_MUST_BE_HOLDOUT59")
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


def prepare_registry_data(source_root: Path) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    features["pred_prob_up_logit"] = qrf.rolling_logit_safe(features)
    features = qrf.v1.add_session_labels(features)
    features = qrf.v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    if features.empty:
        raise RuntimeError("NO_USABLE_COMPLETE_RUN_FEATURES")
    return features, coverage, runs


def add_signals(features: pd.DataFrame, strategy: str) -> pd.DataFrame:
    out = features.copy()
    choices = out.apply(lambda row: qrf.choose_trade(row, strategy), axis=1)
    out[f"{strategy}_side"] = choices.map(lambda item: item[0])
    out[f"{strategy}_minute"] = choices.map(lambda item: item[1]).astype(int)
    out[f"{strategy}_fraction"] = choices.map(lambda item: item[2]).fillna(0.0).astype(float)
    out[f"{strategy}_component"] = choices.map(lambda item: item[3])
    return out


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed_commit = subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_commit != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")
    features, _coverage, runs = prepare_registry_data(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    blocks = holdout_blocks(holdout_runs)

    rows: list[dict[str, object]] = []
    for strategy in REGISTRY_NAMES:
        frame = add_signals(features, strategy)
        splits = [("selection20", selection_runs), ("holdout59", holdout_runs)]
        splits.extend((f"block{index}", names) for index, names in enumerate(blocks, 1))
        for split, names in splits:
            _logs, metrics = simulate_cash_locked(
                frame[frame["run_name"].isin(names)],
                strategy,
                study.ExecutionConfig(fixed_slippage=SLIPPAGE),
                INITIAL_CASH,
                MINIMUM_ORDER,
                DELAY_SECONDS,
                cooldown_events=0,
                capture_logs=False,
                fee_mode="polymarket_taker",
                fee_rate=TAKER_FEE_RATE,
            )
            rows.append({"strategy": strategy, "split": split, **metrics})

    result = pd.DataFrame(rows)
    selection = result[result["split"].eq("selection20")].copy()
    eligible = selection[
        selection["trades"].ge(10) & selection["ending_cash"].ge(MINIMUM_ORDER)
    ].sort_values(["ending_cash", "trades", "strategy"], ascending=[False, False, True])
    selected_strategy = None if eligible.empty else str(eligible.iloc[0]["strategy"])
    selected = result[result["strategy"].eq(selected_strategy)] if selected_strategy else pd.DataFrame()
    holdout = selected[selected["split"].eq("holdout59")].iloc[0] if not selected.empty else None
    block_rows = selected[selected["split"].str.startswith("block")] if not selected.empty else pd.DataFrame()
    gate_passed = bool(
        holdout is not None
        and int(holdout["trades"]) >= 10
        and len(block_rows) == len(BLOCK_SIZES)
        and (block_rows["ending_cash"] >= MINIMUM_ORDER).all()
        and (block_rows["net_return"] >= 0.0).all()
    )

    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "candidate_metrics.csv", index=False)
    if not selected.empty:
        selected.to_csv(output / "selected_metrics.csv", index=False)
    selection.to_csv(output / "selection_metrics.csv", index=False)
    meta: dict[str, object] = {
        "format": "old-data-registered-strategy-small-cash-holdout-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "registry_names": list(REGISTRY_NAMES),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "block_sizes": list(BLOCK_SIZES),
        "selection_rule": "among registry candidates with >=10 selection trades and ending cash >=$5, choose highest selection20 ending cash; ties by trades then name",
        "protocol": {
            "initial_cash": INITIAL_CASH,
            "minimum_order_notional": MINIMUM_ORDER,
            "taker_fee_rate": TAKER_FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippage": SLIPPAGE,
            "cooldown_events": 0,
        },
        "selected_strategy": selected_strategy,
        "summary": {
            "selection_eligible_candidates": int(len(eligible)),
            "holdout_trades": None if holdout is None else int(holdout["trades"]),
            "holdout_ending_cash": None if holdout is None else float(holdout["ending_cash"]),
            "minimum_block_ending_cash": None if block_rows.empty else float(block_rows["ending_cash"].min()),
            "minimum_block_net_return": None if block_rows.empty else float(block_rows["net_return"].min()),
            "gate_passed": gate_passed,
        },
        "decision": "promote_only_if_independent_holdout_gate_passes",
        "candidate_promoted_to_canary": False,
        "evidence_boundary": "Historical old-data paper replay only; no order acceptance, settlement, reusable-cash receipt, or Canary fill.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
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

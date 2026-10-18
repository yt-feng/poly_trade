"""H60: fast, focused verification of the two H59 stable registry candidates.

H59 spent most of its runtime constructing a rolling-logit feature that neither
of its two stable candidates uses.  H60 freezes that candidate set and builds
only the causal session, quality, research and execution fields needed by
``micro_book_consensus_up`` and ``v1_active_fill_mix``.  It keeps the same
two selection blocks, nominal/stress execution contract and nine rolling
holdout windows, so this is an efficiency optimization rather than a relaxed
acceptance gate.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import monthly_runs_quant_research_framework as qrf
import final_v1_live_candidate_search as v1
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from h55_tick_depth_prefix_audit import (
    CASH_RELEASE_DELAY_SECONDS,
    FEE_RATE,
    INITIAL_CASH,
    MINIMUM_ORDER_NOTIONAL,
    MINIMUM_ORDER_SHARES,
    chain_summary,
    paper_events,
    round_quote_prices,
)
from h56_rolling_window_stability import rolling_windows, WINDOW_SIZE, WINDOW_STRIDE, CONFIGS
from quantml_candidate_execution_walk_forward import attach_execution_snapshots

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
CANDIDATES = ("micro_book_consensus_up", "v1_active_fill_mix")
SELECTION_BLOCK_SIZE = 10
SELECTION_BLOCK_COUNT = 2


def prepare_fast_data(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    if features.empty:
        raise RuntimeError("NO_USABLE_FAST_REGISTRY_FEATURES")
    return features, runs


def contiguous_selection_blocks(names: list[str]) -> list[tuple[int, list[str]]]:
    expected = SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT
    if len(names) != expected:
        raise ValueError(f"SELECTION_RUN_COUNT_MISMATCH:{len(names)}:{expected}")
    return [
        (index + 1, names[index * SELECTION_BLOCK_SIZE : (index + 1) * SELECTION_BLOCK_SIZE])
        for index in range(SELECTION_BLOCK_COUNT)
    ]


def add_signals(features: pd.DataFrame, strategy: str) -> pd.DataFrame:
    out = features.copy()
    choices = out.apply(lambda row: qrf.choose_trade(row, strategy), axis=1)
    out[f"{strategy}_side"] = choices.map(lambda item: item[0])
    out[f"{strategy}_minute"] = choices.map(lambda item: item[1]).astype(int)
    out[f"{strategy}_fraction"] = choices.map(lambda item: item[2]).fillna(0.0).astype(float)
    out[f"{strategy}_component"] = choices.map(lambda item: item[3])
    return out


def replay(frame: pd.DataFrame, strategy: str, names: list[str], config: dict[str, Any]) -> dict[str, object]:
    rounded = round_quote_prices(frame, config["tick_size"])
    logs, metrics = simulate_cash_locked(
        rounded[rounded["run_name"].astype(str).isin([str(name) for name in names])],
        strategy,
        study.ExecutionConfig(fixed_slippage=float(config["slippage"]), min_depth=float(config["depth_floor"])),
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    return {**metrics, **chain_summary(paper_events(logs))}


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, runs = prepare_fast_data(source_root)
    frames = {candidate: add_signals(features, candidate) for candidate in CANDIDATES}
    test_runs = runs[4:]
    selection_runs = test_runs[: SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT]
    holdout_runs = test_runs[SELECTION_BLOCK_SIZE * SELECTION_BLOCK_COUNT :]
    selection_blocks = contiguous_selection_blocks(selection_runs)
    holdout_windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, object]] = []
    stable: list[dict[str, object]] = []
    for candidate in CANDIDATES:
        for block_index, names in selection_blocks:
            for config in CONFIGS:
                metrics = replay(frames[candidate], candidate, names, config)
                selection_rows.append({"strategy": candidate, "selection_block": block_index, "split": f"selection_block_{block_index}", **config, **metrics})
        rows = pd.DataFrame([r for r in selection_rows if r["strategy"] == candidate])
        if bool(rows["first_ten_passed"].all()) and bool((rows["first_ten_fills"] == 10).all()):
            stable.append({"strategy": candidate, "min_selection_cash": float(rows["first_ten_ending_cash_usd"].min()), "selection_cash_sum": float(rows["first_ten_ending_cash_usd"].sum())})
    stable = sorted(stable, key=lambda row: (-row["min_selection_cash"], -row["selection_cash_sum"], row["strategy"]))
    selected = stable[0]["strategy"] if stable else None

    holdout_rows: list[dict[str, object]] = []
    if selected is not None:
        for window_index, (_start, names) in enumerate(holdout_windows, start=1):
            for config in CONFIGS:
                metrics = replay(frames[selected], selected, names, config)
                holdout_rows.append({"strategy": selected, "window_index": window_index, "split": f"holdout_window_{window_index}", **config, **metrics})
    holdout = pd.DataFrame(holdout_rows)
    holdout_gate = bool(selected is not None and len(holdout) == len(holdout_windows) * len(CONFIGS) and holdout["first_ten_passed"].all() and (holdout["first_ten_fills"] == 10).all())
    summary = {
        "format": "old-clean-h60-fast-focused-registry-verification-v1",
        "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed,
        "complete_runs": len(runs), "feature_rows": len(features), "candidates": list(CANDIDATES),
        "feature_optimization": "omits rolling_logit_safe because neither frozen candidate reads pred_prob_up_logit",
        "selection_blocks": [{"block_index": i, "run_count": len(names), "first_run": names[0], "last_run": names[-1]} for i, names in selection_blocks],
        "selection_rule": "require both candidates to pass every nominal/stress selection block; rank by worst-block ending cash then sum; evaluate one choice on untouched rolling holdout",
        "protocol": {"initial_cash_usd": INITIAL_CASH, "minimum_order_shares": MINIMUM_ORDER_SHARES, "fee_rate": FEE_RATE, "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS, "configs": list(CONFIGS), "same_ledger": True, "required_fills": 10, "no_concatenation": True, "no_refit": True},
        "window_size_runs": WINDOW_SIZE, "window_stride_runs": WINDOW_STRIDE, "window_count": len(holdout_windows),
        "stable_candidates": stable, "selected_strategy": selected, "holdout_gate": holdout_gate,
        "selection_rows": selection_rows, "holdout_rows": holdout.to_dict(orient="records"),
        "paper_only": True, "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(selection_rows).to_csv(output / "selection_block_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"stable_candidates": [row["strategy"] for row in stable], "selected_strategy": selected, "holdout_gate": holdout_gate}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()

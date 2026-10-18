"""Old-clean quality-aligned sensitivity audit for the frozen H38 candidate.

This is a fixed data-quality sensitivity analysis, not a live quality gate:
the final-price timestamp is known only after a five-minute market closes.
The signal, thresholds, execution contract, chronological split and single
cash ledger are otherwise unchanged.  Paper replay is never Canary evidence.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_path_efficiency_threshold_grid import gated
from h50_causal_label_state_audit import build_market_quality
from sequential_compound_gate import evaluate_compound_chain


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
STRATEGY = "v1_adaptive_mix"
PATH_THRESHOLD = 0.50
SLIPPAGES = (0.005, 0.010)
DELAY_SECONDS = 600.0
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
FEE_RATE = 0.07
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
QUALITY_CLASS = "final_near_window_end"


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    sizes = tuple(int(size) for size in sizes)
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    out: list[list[str]] = []
    cursor = 0
    for size in sizes:
        out.append(runs[cursor : cursor + size])
        cursor += size
    return out


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    rows = logs.sort_values("first_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in rows.itertuples(index=False)
    ]


def replay(frame: pd.DataFrame, run_names: list[str], slippage: float) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin(run_names)],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=float(slippage)),
        INITIAL_CASH,
        MINIMUM_ORDER,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
    )
    events = paper_events(logs)
    chain = evaluate_compound_chain(
        events,
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    prefix = evaluate_compound_chain(
        events[:10],
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        **metrics,
        "paper_chain_fills": int(chain["fills"]),
        "paper_chain_ending_cash": str(chain["ending_cash"]),
        "paper_chain_minimum_cash": str(chain["minimum_cash"]),
        "paper_chain_passed": bool(chain["paper_replay_passed"]),
        "paper_chain_failure": chain["failure_reason"],
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash": str(prefix["ending_cash"]),
        "first_ten_minimum_cash": str(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure": prefix["failure_reason"],
    }, logs


def gate(rows: pd.DataFrame, split_prefix: str, block_count: int) -> bool:
    total = rows[rows["split"].eq(split_prefix) & rows["slippage"].eq(SLIPPAGES[0])]
    blocks = rows[rows["split"].str.startswith(split_prefix.replace("20", "_block").replace("59", "_block"))]
    return bool(
        len(total) == 1
        and int(total.iloc[0]["trades"]) >= 10
        and bool(total.iloc[0]["paper_chain_passed"])
        and bool(total.iloc[0]["first_ten_passed"])
        and len(blocks) == block_count * len(SLIPPAGES)
        and bool((blocks["ending_cash"] >= INITIAL_CASH / 2).all())
        and bool((blocks["net_return"] >= 0.0).all())
    )


def evaluate(source_root: Path, output: Path, source_commit: str = SOURCE_COMMIT) -> dict[str, object]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_SNAPSHOT_COMMIT_MISMATCH:{observed}:{source_commit}")

    features, coverage, runs = study.prepare_data(source_root)
    raw, _ = base.read_all_monthly_runs(source_root)
    complete = coverage.loc[coverage["run_name"].astype(str).isin(runs)].copy()
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    quality = build_market_quality(quotes)
    quality_ids = set(quality.loc[quality["quality_class"].eq(QUALITY_CLASS), "market_id"].astype(str))
    quality_frame = gated(features, PATH_THRESHOLD)
    quality_frame = quality_frame[quality_frame["market_id"].astype(str).isin(quality_ids)].copy()

    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    selection_blocks = chronological_blocks(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    split_names: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    split_names += [(f"selection_block{i}", names) for i, names in enumerate(selection_blocks, 1)]
    split_names += [(f"holdout_block{i}", names) for i, names in enumerate(holdout_blocks, 1)]

    rows: list[dict[str, object]] = []
    for split, names in split_names:
        for slippage in SLIPPAGES:
            metrics, _logs = replay(quality_frame, names, slippage)
            rows.append({"split": split, "slippage": float(slippage), **metrics})
    result = pd.DataFrame(rows)
    selection_rows = result[result["split"].str.startswith("selection")]
    holdout_rows = result[result["split"].str.startswith("holdout")]
    selection_gate = gate(selection_rows, "selection20", len(SELECTION_BLOCK_SIZES))
    holdout_gate = gate(holdout_rows, "holdout59", len(HOLDOUT_BLOCK_SIZES))

    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    quality.groupby(["run_name", "quality_class"], dropna=False).size().reset_index(name="markets").to_csv(output / "quality_by_run.csv", index=False)
    quality.groupby(["quality_class"], dropna=False).agg(markets=("market_id", "size"), outcome_rate=("outcome_up", "mean")).reset_index().to_csv(output / "quality_counts.csv", index=False)
    meta: dict[str, object] = {
        "format": "old-clean-h51-quality-aligned-subset-sensitivity-v1",
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "raw_market_rows": int(len(quality)),
        "usable_feature_rows_before_quality_filter": int(len(features)),
        "quality_aligned_market_rows": int(len(quality_ids)),
        "quality_class": QUALITY_CLASS,
        "quality_rule": "target_price non-null and final observation within +/-30 seconds of slug_start+300 seconds",
        "quality_filter_is_post_outcome": True,
        "strategy": STRATEGY,
        "path_threshold": PATH_THRESHOLD,
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "selection_block_sizes": list(SELECTION_BLOCK_SIZES),
        "holdout_block_sizes": list(HOLDOUT_BLOCK_SIZES),
        "protocol": {
            "initial_cash": INITIAL_CASH,
            "minimum_order_notional": MINIMUM_ORDER,
            "fee_rate": FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippages": list(SLIPPAGES),
            "same_ledger": True,
            "required_fills": 10,
        },
        "selection_gate": bool(selection_gate),
        "holdout_gate": bool(holdout_gate),
        "hypothesis": "A fixed highest-quality label subset may reduce source noise while preserving the frozen H38 path signal's ten-fill same-ledger chain.",
        "counter_hypothesis": "The quality-aligned subset still fails independent blocks; the root cause is signal, execution, or cash recycling rather than label clock coverage.",
        "interpretation": "paper sensitivity only; the post-outcome quality class must not be used as a live entry gate",
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    print(result.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()

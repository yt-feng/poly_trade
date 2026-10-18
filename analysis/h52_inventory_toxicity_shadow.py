"""Old-clean H52 inventory/toxicity shadow audit.

This audit replays the frozen H38 path-efficiency candidate on the existing
clean monthly_runs snapshot.  It tests two predeclared, entry-time filters:
an execution-toxicity filter and a directional book-pressure filter.  Both
filters are shadows only; they do not use the post-settlement outcome and do
not enable an order.  Every split uses one evolving $10 ledger and the
chronological first ten fills are evaluated without reset.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Iterable

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_path_efficiency_threshold_grid import gated
from sequential_compound_gate import evaluate_compound_chain


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
STRATEGY = "v1_adaptive_mix"
PATH_THRESHOLD = 0.50
SLIPPAGES = (0.005, 0.010)
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
DELAY_SECONDS = 600.0
FEE_RATE = 0.07
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def chronological_blocks(runs: list[str], sizes: Iterable[int]) -> list[list[str]]:
    sizes = tuple(int(size) for size in sizes)
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    blocks: list[list[str]] = []
    cursor = 0
    for size in sizes:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


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


def selected_execution_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    side = out[f"{STRATEGY}_side"].astype(str)
    minute = pd.to_numeric(out[f"{STRATEGY}_minute"], errors="coerce").fillna(-1).astype(int)
    values: dict[str, list[float]] = {name: [] for name in ("adverse_slippage", "quote_spread", "quote_size")}
    for index, row in out.iterrows():
        current_side, current_minute = side.loc[index], int(minute.loc[index])
        if current_side not in {"buy_up", "buy_down"} or current_minute not in {1, 2, 4}:
            for name in values:
                values[name].append(np.nan)
            continue
        prefix = f"exec_{current_side}_{current_minute}m_d4_"
        for name in values:
            values[name].append(pd.to_numeric(row.get(prefix + name), errors="coerce"))
    for name, values_list in values.items():
        out[f"selected_{name}"] = values_list
    return out


def apply_variant(frame: pd.DataFrame, variant: str) -> pd.DataFrame:
    out = selected_execution_columns(frame)
    side = out[f"{STRATEGY}_side"].astype(str)
    active = side.ne("skip")
    if variant == "baseline":
        keep = active
    elif variant == "toxicity_filter_v1":
        keep = active & out["selected_adverse_slippage"].le(0.005) & out["selected_quote_spread"].le(0.01) & out["selected_quote_size"].ge(100.0)
    elif variant == "inventory_filter_v1":
        pressure = np.where(
            side.eq("buy_down"),
            pd.to_numeric(out["book_pressure_down_2m"], errors="coerce"),
            pd.to_numeric(out["book_pressure_up_2m"], errors="coerce"),
        )
        keep = active & pd.Series(pressure, index=out.index).ge(-0.04)
    else:
        raise ValueError(f"unknown variant: {variant}")
    out.loc[~keep, f"{STRATEGY}_side"] = "skip"
    out.loc[~keep, f"{STRATEGY}_minute"] = -1
    out.loc[~keep, f"{STRATEGY}_fraction"] = 0.0
    return out


def gate(rows: pd.DataFrame, total_name: str, block_prefix: str, block_count: int) -> bool:
    total = rows[rows["split"].eq(total_name) & rows["slippage"].eq(SLIPPAGES[0])]
    blocks = rows[rows["split"].str.startswith(block_prefix)]
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
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frozen = gated(features, PATH_THRESHOLD)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    selection_blocks = chronological_blocks(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)
    variants = ("baseline", "toxicity_filter_v1", "inventory_filter_v1")
    rows: list[dict[str, object]] = []
    variant_counts: dict[str, int] = {}
    for variant in variants:
        frame = apply_variant(frozen, variant)
        variant_counts[variant] = int(frame[f"{STRATEGY}_side"].ne("skip").sum())
        splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs)]
        splits += [(f"selection_block{i}", names) for i, names in enumerate(selection_blocks, 1)]
        splits += [(f"holdout_block{i}", names) for i, names in enumerate(holdout_blocks, 1)]
        for split, names in splits:
            for slippage in SLIPPAGES:
                metrics, _logs = replay(frame, names, slippage)
                rows.append({"variant": variant, "split": split, "slippage": float(slippage), **metrics})
    result = pd.DataFrame(rows)
    gates: dict[str, dict[str, bool]] = {}
    for variant in variants:
        subset = result[result["variant"].eq(variant)]
        gates[variant] = {
            "selection_gate": gate(subset, "selection20", "selection_block", len(SELECTION_BLOCK_SIZES)),
            "holdout_gate": gate(subset, "holdout59", "holdout_block", len(HOLDOUT_BLOCK_SIZES)),
        }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    meta: dict[str, object] = {
        "format": "old-clean-h52-inventory-toxicity-shadow-v1",
        "source_commit": source_commit,
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "strategy": STRATEGY,
        "path_threshold": PATH_THRESHOLD,
        "variants": list(variants),
        "variant_signal_counts": variant_counts,
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
        "hypotheses": {
            "toxicity_filter_v1": "Entry execution adverse slippage, spread and available size identify toxic fills that can strand a $10 ledger behind the $5 minimum order.",
            "inventory_filter_v1": "Directional book pressure at entry identifies inventory imbalance that makes a one-sided fill more likely to lose before cash is reusable.",
        },
        "filter_definitions": {
            "toxicity_filter_v1": "selected_adverse_slippage <= 0.005 and selected_quote_spread <= 0.01 and selected_quote_size >= 100",
            "inventory_filter_v1": "side-specific book_pressure_{up,down}_2m >= -0.04",
        },
        "gates": gates,
        "candidate_promoted_to_canary": False,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "evidence_boundary": "Historical old-clean paper replay only; post-settlement outcome is used only for evaluation, never for entry filtering.",
        "decision": "promote_only_if_one_predeclared_variant_passes_selection_and_independent_holdout_gates",
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

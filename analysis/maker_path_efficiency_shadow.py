"""Old-data maker-path upper bound for the frozen v1 adaptive candidate.

This audit reuses the already cleaned 83-run source snapshot.  It replaces the
taker execution quote with a resting-bid quote and only counts a historical
"touch" supplied by :mod:`maker_shadow_chain`.  A touch is an optimistic
proxy: it is not an authenticated order acceptance, queue position, fill, or
settlement receipt.  The result can reject a path, or bound its best possible
case, but it cannot create a Canary fill.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from maker_shadow_chain import attach_maker_snapshots
from v1_state_filter_walk_forward import filtered_frame


EXPECTED_COMPLETE_RUNS = 83
SELECTION_RUNS = 20
BLOCK_SIZES = (12, 12, 12, 12, 11)
STRATEGY = "v1_adaptive_mix"
FILTER = "path_efficiency_ge_050"
DELAY_SECONDS = 600.0
MAKER_FEE_RATE = 0.0


def holdout_blocks(runs: list[str]) -> list[list[str]]:
    if len(runs) != sum(BLOCK_SIZES):
        raise ValueError("BLOCK_INPUT_MUST_BE_HOLDOUT59")
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


def maker_touch_mask(frame: pd.DataFrame, strategy: str = STRATEGY) -> pd.Series:
    """Return the preregistered touch proxy for the strategy's chosen side."""
    side = frame[f"{strategy}_side"].fillna("skip").astype(str)
    minute = pd.to_numeric(frame[f"{strategy}_minute"], errors="coerce")
    touch = pd.Series(False, index=frame.index)
    for side_name in ("buy_up", "buy_down"):
        for entry_minute in (1, 2, 4):
            rows = side.eq(side_name) & minute.eq(entry_minute)
            prefix = f"maker_{side_name}_{entry_minute}m_d4"
            if f"{prefix}_touch" in frame:
                touch.loc[rows] = pd.to_numeric(
                    frame.loc[rows, f"{prefix}_touch"], errors="coerce"
                ).fillna(0).eq(1)
    return touch


def inject_maker_execution(frame: pd.DataFrame, strategy: str = STRATEGY) -> pd.DataFrame:
    """Map maker proxy snapshots into the common cash-locked simulator schema."""
    out = frame.copy()
    side = out[f"{strategy}_side"].fillna("skip").astype(str)
    minute = pd.to_numeric(out[f"{strategy}_minute"], errors="coerce")
    for side_name in ("buy_up", "buy_down"):
        for entry_minute in (1, 2, 4):
            rows = side.eq(side_name) & minute.eq(entry_minute)
            maker_prefix = f"maker_{side_name}_{entry_minute}m_d4"
            exec_prefix = f"exec_{side_name}_{entry_minute}m_d4"
            for field in ("price", "size", "spread", "overround"):
                if f"{maker_prefix}_{field}" not in out:
                    continue
                out.loc[rows, f"{exec_prefix}_quote_{field}"] = pd.to_numeric(
                    out.loc[rows, f"{maker_prefix}_{field}"], errors="coerce"
                )
            if f"{maker_prefix}_price" not in out:
                continue
            out.loc[rows, f"{exec_prefix}_signal_price"] = pd.to_numeric(
                out.loc[rows, f"{maker_prefix}_price"], errors="coerce"
            )
            out.loc[rows, f"{exec_prefix}_adverse_slippage"] = 0.0
    return out


def prepare_maker_data(source_root: Path) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    features, coverage, runs = study.prepare_data(source_root)
    raw, _ = base.read_all_monthly_runs(source_root)
    quotes = base.prepare_quotes(raw[raw["run_name"].astype(str).isin(runs)].copy())
    features = attach_maker_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    return features, coverage, runs


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed_commit = subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_commit != source_commit:
        raise RuntimeError("SOURCE_SNAPSHOT_COMMIT_MISMATCH")

    features, _coverage, runs = prepare_maker_data(source_root)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    blocks = holdout_blocks(holdout_runs)

    gated = filtered_frame(features, STRATEGY, FILTER)
    touch = maker_touch_mask(gated, STRATEGY)
    gated.loc[~touch, f"{STRATEGY}_side"] = "skip"
    gated.loc[~touch, f"{STRATEGY}_minute"] = -1
    gated.loc[~touch, f"{STRATEGY}_fraction"] = 0.0
    maker = inject_maker_execution(gated, STRATEGY)

    splits = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    splits.extend((f"block{index}", names) for index, names in enumerate(blocks, 1))
    rows: list[dict[str, object]] = []
    logs: list[pd.DataFrame] = []
    for split, names in splits:
        frame = maker[maker["run_name"].isin(names)]
        trade_log, metrics = simulate_cash_locked(
            frame,
            STRATEGY,
            study.ExecutionConfig(fixed_slippage=0.0),
            10.0,
            5.0,
            DELAY_SECONDS,
            cooldown_events=0,
            capture_logs=True,
            fee_mode="polymarket_taker",
            fee_rate=MAKER_FEE_RATE,
        )
        rows.append(
            {
                "split": split,
                "maker_touch_rows": int(touch[frame.index].sum()),
                **metrics,
            }
        )
        if not trade_log.empty:
            trade_log = trade_log.copy()
            trade_log["split"] = split
            logs.append(trade_log)

    result = pd.DataFrame(rows)
    holdout = result[result["split"].eq("holdout59")].iloc[0]
    block_rows = result[result["split"].str.startswith("block")]
    gate_passed = bool(
        int(holdout["trades"]) >= 10
        and (block_rows["ending_cash"] >= 5.0).all()
        and (block_rows["net_return"] >= 0.0).all()
    )
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    if logs:
        pd.concat(logs, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)

    meta: dict[str, object] = {
        "format": "old-data-maker-path-efficiency-shadow-v1",
        "source_repo_commit": source_commit,
        "expected_complete_runs": EXPECTED_COMPLETE_RUNS,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "strategy": STRATEGY,
        "filter": FILTER,
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "block_sizes": list(BLOCK_SIZES),
        "protocol": {
            "initial_cash": 10.0,
            "minimum_order_notional": 5.0,
            "maker_execution_fee_rate": MAKER_FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "slippage": 0.0,
            "fill_ratio": study.FILL_RATIO,
            "cooldown_events": 0,
        },
        "summary": {
            "holdout_trades": int(holdout["trades"]),
            "holdout_ending_cash": float(holdout["ending_cash"]),
            "holdout_net_return": float(holdout["net_return"]),
            "minimum_block_ending_cash": float(block_rows["ending_cash"].min()),
            "minimum_block_net_return": float(block_rows["net_return"].min()),
            "gate_passed": gate_passed,
        },
        "decision": "proxy_only_never_promote_without_authenticated_maker_receipts",
        "candidate_promoted_to_canary": False,
        "fill_interpretation": "historical quote-touch proxy, not an actual fill",
        "proxy_rule": "after the delayed signal, a later observed ask crosses the resting bid by at least one cent before close",
        "evidence_boundary": "Old cleaned-data upper-bound replay; no queue position, order acceptance, fill, settlement, or reusable-cash receipt.",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
    }
    (output / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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

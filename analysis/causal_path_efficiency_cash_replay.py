"""Causal path-efficiency state filter under the frozen small-cash contract.

The existing v1 adaptive/conservative signal family was promising in the
immediate-cash diagnostic.  This replay keeps only the causal
``path_efficiency_first2m >= 0.50`` state and evaluates both registered base
components under the documented $5 notional, crypto taker fee, settlement and
cash-release rules.  Selection is limited to the first 20 test runs; the
remaining 59 runs and five chronological blocks are held out.  No order is
submitted and no simulated fill is a canary.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain
from v1_state_filter_walk_forward import filtered_frame

CANDIDATES = (
    "v1_adaptive_mix__unfiltered",
    "v1_conservative_mix__unfiltered",
    "v1_adaptive_mix__path_efficiency_ge_050",
    "v1_conservative_mix__path_efficiency_ge_050",
)
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
# Compatibility alias for callers that only need the independent holdout shape.
BLOCK_SIZES = HOLDOUT_BLOCK_SIZES


def chronological_blocks(runs: list[str], sizes: tuple[int, ...]) -> list[list[str]]:
    """Partition exactly the supplied split without importing earlier runs."""
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    blocks: list[list[str]] = []
    cursor = 0
    for size in sizes:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    if len({item for block in blocks for item in block}) != len(runs):
        raise ValueError("BLOCKS_MUST_NOT_OVERLAP")
    return blocks


def paper_prefix_summary(logs: pd.DataFrame, required_fills: int = 10) -> dict[str, object]:
    """Evaluate the first N paper rows on one evolving cash ledger."""
    if logs.empty:
        events: list[dict[str, object]] = []
    else:
        rows = logs.sort_values("first_quote_ts", kind="stable")
        events = [
            {
                "status": "paper",
                "entry_ts": float(pd.Timestamp(row.first_quote_ts).timestamp()),
                "cash_available_ts": float(pd.Timestamp(row.release_ts).timestamp()),
                "entry_cost": str(row.filled_cost),
                "fee": str(row.fee_usd),
                "payout": str(row.payout_at_settlement),
            }
            for row in rows.itertuples(index=False)
        ]
    result = evaluate_compound_chain(
        events[:required_fills],
        initial_cash="10",
        minimum_order="5",
        required_fills=required_fills,
        evidence_mode="paper_upper_bound",
    )
    return {
        "fills": int(result["fills"]),
        "ending_cash": result["ending_cash"],
        "minimum_cash": result["minimum_cash"],
        "paper_replay_passed": bool(result["paper_replay_passed"]),
        "failure_reason": result["failure_reason"],
    }


def evaluate(source_root: Path, output: Path, slippages: tuple[float, ...]) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    selection_blocks = chronological_blocks(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)

    rows: list[dict[str, object]] = []
    logs: list[pd.DataFrame] = []
    for candidate in CANDIDATES:
        strategy, filter_name = candidate.split("__", 1)
        gated = features.copy() if filter_name == "unfiltered" else filtered_frame(features, strategy, filter_name)
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            config = study.ExecutionConfig(fixed_slippage=slippages[0])
            trade_log, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(names)], strategy, config, 10.0, 5.0,
                600.0, cooldown_events=0, capture_logs=True,
            )
            rows.append({"candidate": candidate, "split": split, "slippage": slippages[0], **metrics})
            if not trade_log.empty:
                trade_log = trade_log.copy(); trade_log["candidate"] = candidate; trade_log["split"] = split; logs.append(trade_log)

        for slippage in slippages:
            config = study.ExecutionConfig(fixed_slippage=slippage)
            for block_index, names in enumerate(selection_blocks, 1):
                trade_log, metrics = simulate_cash_locked(
                    gated[gated["run_name"].isin(names)], strategy, config, 10.0, 5.0,
                    600.0, cooldown_events=0, capture_logs=True,
                )
                rows.append({"candidate": candidate, "split": f"selection_block{block_index}", "slippage": slippage, **metrics})
                if not trade_log.empty:
                    trade_log = trade_log.copy(); trade_log["candidate"] = candidate; trade_log["split"] = f"selection_block{block_index}"; logs.append(trade_log)
            for block_index, names in enumerate(holdout_blocks, 1):
                trade_log, metrics = simulate_cash_locked(
                    gated[gated["run_name"].isin(names)], strategy, config, 10.0, 5.0,
                    600.0, cooldown_events=0, capture_logs=True,
                )
                rows.append({"candidate": candidate, "split": f"holdout_block{block_index}", "slippage": slippage, **metrics})
                if not trade_log.empty:
                    trade_log = trade_log.copy(); trade_log["candidate"] = candidate; trade_log["split"] = f"holdout_block{block_index}"; logs.append(trade_log)
            trade_log, metrics = simulate_cash_locked(
                gated[gated["run_name"].isin(test_runs)], strategy, config, 10.0, 5.0,
                600.0, cooldown_events=0, capture_logs=True,
            )
            rows.append({"candidate": candidate, "split": "all_test79", "slippage": slippage, **metrics})
            if not trade_log.empty:
                trade_log = trade_log.copy(); trade_log["candidate"] = candidate; trade_log["split"] = "all_test79"; logs.append(trade_log)

    result = pd.DataFrame(rows)
    selection = result[(result["split"] == "selection20") & result["slippage"].eq(slippages[0])]
    selected_candidate = str(selection.sort_values(["ending_cash", "trades"], ascending=[False, False]).iloc[0]["candidate"])
    selected_rows = result[result["candidate"].eq(selected_candidate)].copy()
    holdout = selected_rows[selected_rows["split"].eq("holdout59")]
    blocks_selected = selected_rows[selected_rows["split"].str.startswith("holdout_block")]
    all_logs = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()
    selected_selection_logs = all_logs[
        all_logs.get("candidate", pd.Series(dtype=object)).astype(str).eq(selected_candidate)
        & all_logs.get("split", pd.Series(dtype=object)).astype(str).eq("selection20")
    ] if not all_logs.empty else pd.DataFrame()
    selected_holdout_logs = all_logs[
        all_logs.get("candidate", pd.Series(dtype=object)).astype(str).eq(selected_candidate)
        & all_logs.get("split", pd.Series(dtype=object)).astype(str).eq("holdout59")
    ] if not all_logs.empty else pd.DataFrame()
    selection_prefix = paper_prefix_summary(selected_selection_logs)
    holdout_prefix = paper_prefix_summary(selected_holdout_logs)
    same_ledger_prefix_gate = bool(
        selection_prefix["paper_replay_passed"] and holdout_prefix["paper_replay_passed"]
    )
    gate = bool(
        int(holdout["trades"].iloc[0]) >= 10
        and same_ledger_prefix_gate
        and len(blocks_selected) == len(HOLDOUT_BLOCK_SIZES) * len(slippages)
        and bool((blocks_selected["ending_cash"] >= 5.0).all())
        and bool((blocks_selected["net_return"] >= 0.0).all())
    )

    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    if logs:
        pd.concat(logs, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    selected_rows.to_csv(output / "selected_metrics.csv", index=False)
    meta = {
        "format": "old-data-causal-path-efficiency-cash-replay-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "feature_rows": len(features),
        "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs),
        "candidates": list(CANDIDATES), "selected_candidate": selected_candidate,
        "filter": "path_efficiency_first2m >= 0.50",
        "protocol": {"initial_cash": 10.0, "min_cost": 5.0, "fee_rate": 0.07,
                     "settlement_seconds": 300.0, "cash_release_delay_seconds": 600.0,
                     "slippages": list(slippages), "fill_ratio": 0.25, "cooldown_events": 0},
        "promotion_rule": "selection and holdout must each pass one same-ledger ten-fill paper prefix; selected holdout must have >=10 trades and all five holdout-only blocks must end >=$5 with nonnegative PnL at both slippages",
        "selection_block_sizes": list(SELECTION_BLOCK_SIZES),
        "holdout_block_sizes": list(HOLDOUT_BLOCK_SIZES),
        "selection_prefix": selection_prefix,
        "holdout_prefix": holdout_prefix,
        "same_ledger_prefix_gate": same_ledger_prefix_gate,
        "selected_gate_passed": gate, "new_data_collected": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(selected_rows.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--slippages", default="0.005,0.010")
    args = parser.parse_args()
    slippages = tuple(float(value.strip()) for value in args.slippages.split(",") if value.strip())
    if slippages != (0.005, 0.01):
        raise SystemExit("This preregistered replay requires 0.005 and 0.01 slippage")
    evaluate(args.source_root, args.output, slippages)


if __name__ == "__main__":
    main()

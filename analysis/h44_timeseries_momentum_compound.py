"""Strict same-ledger replay of the old-clean time-series momentum family.

H44 is deliberately a structural candidate rather than another probability
calibration.  The signal and execution fields are frozen from the existing
time-series replay.  The additional gate evaluates the actual chronological
paper events through ``evaluate_compound_chain`` so a reset between runs cannot
inflate the ten-fill count.  This module never submits orders and does not
collect data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import monthly_runs_quant_research_framework as research
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain


CANDIDATES = ("timeseries_momentum_up", "timeseries_momentum_down")
SLIPPAGES = (0.005, 0.010)
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
CASH_RELEASE_DELAY_SECONDS = 600.0
SELECTION_RUNS = 20
SELECTION_BLOCK_SIZES = (4, 4, 4, 4, 4)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def attach_candidate(features: pd.DataFrame, strategy: str) -> pd.DataFrame:
    out = features.copy()
    choices = out.apply(lambda row: research.choose_trade(row, strategy), axis=1)
    out[f"{strategy}_side"] = choices.map(lambda value: value[0])
    out[f"{strategy}_minute"] = choices.map(lambda value: value[1]).astype(int)
    out[f"{strategy}_fraction"] = choices.map(lambda value: value[2]).fillna(0.0).astype(float)
    out[f"{strategy}_component"] = choices.map(lambda value: value[3])
    return out


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)
    ]


def chain_summary(logs: pd.DataFrame, required_fills: int = 10) -> dict[str, object]:
    events = paper_events(logs)
    chain = evaluate_compound_chain(
        events,
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=required_fills,
        evidence_mode="paper_upper_bound",
    )
    return {
        "paper_chain_fills": chain["fills"],
        "paper_chain_ending_cash": chain["ending_cash"],
        "paper_chain_minimum_cash": chain["minimum_cash"],
        "paper_chain_passed": chain["paper_replay_passed"],
        "paper_chain_failure_reason": chain["failure_reason"],
        "first_ten_fills": min(len(events), required_fills),
        "first_ten_ending_cash": None,
        "first_ten_minimum_cash": None,
        "first_ten_passed": False,
    }


def first_ten_summary(logs: pd.DataFrame) -> dict[str, object]:
    events = paper_events(logs)
    if len(events) < 10:
        return {
            "first_ten_fills": len(events),
            "first_ten_ending_cash": None,
            "first_ten_minimum_cash": None,
            "first_ten_passed": False,
            "first_ten_failure_reason": "fewer_than_required_consecutive_fills",
        }
    chain = evaluate_compound_chain(
        events[:10], initial_cash=str(INITIAL_CASH), minimum_order=str(MINIMUM_ORDER),
        required_fills=10, evidence_mode="paper_upper_bound",
    )
    return {
        "first_ten_fills": chain["fills"],
        "first_ten_ending_cash": chain["ending_cash"],
        "first_ten_minimum_cash": chain["minimum_cash"],
        "first_ten_passed": chain["paper_replay_passed"],
        "first_ten_failure_reason": chain["failure_reason"],
    }


def block_names(runs: list[str], sizes: tuple[int, ...]) -> list[tuple[int, list[str]]]:
    out = []
    cursor = 0
    for index, size in enumerate(sizes, start=1):
        out.append((index, runs[cursor : cursor + size]))
        cursor += size
    return out


def replay(frame: pd.DataFrame, strategy: str, names: list[str], slippage: float) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(x) for x in names])],
        strategy,
        study.ExecutionConfig(fixed_slippage=slippage),
        INITIAL_CASH,
        MINIMUM_ORDER,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
    )
    summary = {**metrics, **chain_summary(logs)}
    summary.update(first_ten_summary(logs))
    return summary, logs


ALL_TEST_RUNS: list[str] = []


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    global ALL_TEST_RUNS
    features, _coverage, runs = study.prepare_data(source_root)
    features = research.add_research_features(features)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    ALL_TEST_RUNS = list(test_runs)

    selection_blocks = block_names(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = block_names(holdout_runs, HOLDOUT_BLOCK_SIZES)
    rows: list[dict[str, object]] = []
    logs_by_key: dict[tuple[str, str, float], pd.DataFrame] = {}
    for strategy in CANDIDATES:
        gated = attach_candidate(features, strategy)
        for split, names in (("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs)):
            for slippage in SLIPPAGES:
                summary, logs = replay(gated, strategy, names, slippage)
                rows.append({"candidate": strategy, "split": split, "slippage": slippage, **summary})
                logs_by_key[(strategy, split, slippage)] = logs
        for split, blocks in (("selection_block", selection_blocks), ("holdout_block", holdout_blocks)):
            for block_index, names in blocks:
                for slippage in SLIPPAGES:
                    summary, logs = replay(gated, strategy, names, slippage)
                    rows.append({"candidate": strategy, "split": split, "block": block_index, "slippage": slippage, **summary})
                    logs_by_key[(strategy, f"{split}{block_index}", slippage)] = logs

    result = pd.DataFrame(rows)
    sel = result[(result["split"] == "selection20") & result["slippage"].eq(SLIPPAGES[0])]
    eligible: list[str] = []
    for candidate in CANDIDATES:
        current = result[result["candidate"].eq(candidate)]
        s = current[(current["split"] == "selection20") & current["slippage"].eq(SLIPPAGES[0])].iloc[0]
        blocks = current[(current["split"] == "selection_block")]
        eligible.append(candidate) if (
            bool(s["paper_chain_passed"])
            and bool((blocks["ending_cash"] >= 5.0).all())
            and bool((blocks["net_return"] >= 0.0).all())
        ) else None
    selected_candidate = str(
        sel[sel["candidate"].isin(eligible)].sort_values(["ending_cash", "trades"], ascending=[False, False]).iloc[0]["candidate"]
    ) if eligible else None

    selected = result[result["candidate"].eq(selected_candidate)].copy() if selected_candidate else result.iloc[0:0].copy()
    holdout = selected[selected["split"] == "holdout59"]
    holdout_blocks_selected = selected[selected["split"] == "holdout_block"]
    gate = bool(
        selected_candidate
        and bool((holdout["paper_chain_passed"]).all())
        and bool((holdout_blocks_selected["ending_cash"] >= 5.0).all())
        and bool((holdout_blocks_selected["net_return"] >= 0.0).all())
        and bool((holdout["first_ten_passed"]).all())
    )

    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    if selected_candidate:
        selected.to_csv(output / "selected_metrics.csv", index=False)
    log_rows = []
    for (candidate, split, slippage), logs in logs_by_key.items():
        if logs.empty:
            continue
        copy = logs.copy()
        copy["candidate"] = candidate; copy["split"] = split; copy["slippage"] = slippage
        log_rows.append(copy)
    if log_rows:
        pd.concat(log_rows, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    meta = {
        "format": "old-clean-h44-timeseries-momentum-compound-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "feature_rows": len(features),
        "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs),
        "candidates": list(CANDIDATES), "eligible_candidates": eligible,
        "selected_candidate": selected_candidate,
        "hypothesis": "A frozen time-series momentum direction should survive a single $10 ledger when the earlier H42/H43 probability-calibration families do not.",
        "counter_hypothesis": "The first chronological loss or sparse selection makes the apparent independent holdout edge unusable for a continuous compounding chain.",
        "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER, "fee_rate": 0.07,
                     "settlement_seconds": 300.0, "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
                     "slippages": list(SLIPPAGES), "cooldown_events": 0},
        "selection_rule": "candidate must pass ten-fills same-ledger paper chain and every selection block at both slippages; otherwise reject without selecting a holdout winner",
        "promotion_rule": "independent holdout must pass the same-ledger ten-fill chain, first-ten prefix, and every holdout block at both slippages",
        "selected_gate_passed": gate, "new_data_collected": False, "live_orders_enabled": False,
        "actual_fills": 0, "verified_canary_fills": 0,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(result.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()

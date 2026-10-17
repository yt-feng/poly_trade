"""Audit the minimum-order unit assumption under the frozen compound protocol.

The old replays imposed both a five-dollar notional floor and a five-share
floor.  The repository's source audit records that the CLOB book field is
denominated in shares while the separate Gamma orderMinSize unit is unresolved.
H45 therefore compares the frozen conservative dollar-floor ledger with a
conditional share-only counterfactual.  It does not claim that the latter is
executable until the exact market's current order-type minimum is observed.

Signals are frozen H38 path-efficiency >= 0.50 and the existing time-series
momentum Up/Down candidates.  No threshold, model, label, or data window is
changed, and no order is submitted.
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
from causal_path_efficiency_threshold_grid import gated as path_gate
from sequential_compound_gate import evaluate_compound_chain


INITIAL_CASH = 10.0
MINIMUM_SHARES = 5.0
SLIPPAGES = (0.005, 0.010)
DELAY_SECONDS = 600.0
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
PROTOCOLS = {
    "frozen_dollar_floor": {"minimum_order_notional": 5.0, "minimum_order_shares": 5.0},
    "conditional_share_floor": {"minimum_order_notional": 0.0, "minimum_order_shares": 5.0},
}
CANDIDATES = (
    "h38_path_efficiency_ge_050",
    "timeseries_momentum_up",
    "timeseries_momentum_down",
)


def chronological_blocks(runs: list[str], sizes: tuple[int, ...]) -> list[list[str]]:
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    out: list[list[str]] = []
    cursor = 0
    for size in sizes:
        out.append(runs[cursor : cursor + size])
        cursor += size
    return out


def attach_timeseries(features: pd.DataFrame, strategy: str) -> pd.DataFrame:
    out = features.copy()
    choices = out.apply(lambda row: research.choose_trade(row, strategy), axis=1)
    out[f"{strategy}_side"] = choices.map(lambda value: value[0])
    out[f"{strategy}_minute"] = choices.map(lambda value: value[1]).astype(int)
    out[f"{strategy}_fraction"] = choices.map(lambda value: value[2]).fillna(0.0).astype(float)
    out[f"{strategy}_component"] = choices.map(lambda value: value[3])
    return out


def candidate_frames(features: pd.DataFrame) -> dict[str, tuple[pd.DataFrame, str]]:
    frames: dict[str, tuple[pd.DataFrame, str]] = {}
    path = path_gate(features, 0.50).copy()
    frames["h38_path_efficiency_ge_050"] = (path, "v1_adaptive_mix")
    for strategy in ("timeseries_momentum_up", "timeseries_momentum_down"):
        frames[strategy] = (attach_timeseries(features, strategy), strategy)
    return frames


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


def chain_metrics(logs: pd.DataFrame) -> dict[str, object]:
    events = paper_events(logs)
    chain = evaluate_compound_chain(
        # The pure ledger validator requires a positive floor.  The simulator
        # enforces the actual share floor; this tiny placeholder represents
        # "no additional dollar notional floor" without changing any event.
        events, initial_cash=str(INITIAL_CASH), minimum_order="0.0000001",
        required_fills=10, evidence_mode="paper_upper_bound",
    )
    prefix = evaluate_compound_chain(
        events[:10], initial_cash=str(INITIAL_CASH), minimum_order="0.0000001",
        required_fills=10, evidence_mode="paper_upper_bound",
    )
    return {
        "paper_chain_fills": int(chain["fills"]),
        "paper_chain_ending_cash": chain["ending_cash"],
        "paper_chain_minimum_cash": chain["minimum_cash"],
        "paper_chain_passed": bool(chain["paper_replay_passed"]),
        "paper_chain_failure_reason": chain["failure_reason"],
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash": prefix["ending_cash"],
        "first_ten_minimum_cash": prefix["minimum_cash"],
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(frame: pd.DataFrame, strategy: str, names: list[str], slip: float, protocol: dict[str, float]) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(x) for x in names])],
        strategy,
        study.ExecutionConfig(fixed_slippage=slip),
        INITIAL_CASH,
        protocol["minimum_order_notional"],
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=0.07,
        min_order_shares=protocol["minimum_order_shares"],
    )
    return {**metrics, **chain_metrics(logs)}, logs


def gate_selection(rows: pd.DataFrame) -> bool:
    total = rows[(rows["split"] == "selection20") & rows["slippage"].eq(SLIPPAGES[0])]
    blocks = rows[rows["split"] == "selection_block"]
    return bool(
        not total.empty and bool(total.iloc[0]["paper_chain_passed"])
        and len(blocks) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES)
        and bool((blocks["ending_cash"] >= 5.0).all())
        and bool((blocks["net_return"] >= 0.0).all())
    )


def gate_holdout(rows: pd.DataFrame) -> bool:
    total = rows[(rows["split"] == "holdout59")]
    blocks = rows[rows["split"] == "holdout_block"]
    return bool(
        len(total) == len(SLIPPAGES)
        and bool(total["paper_chain_passed"].all())
        and bool(total["first_ten_passed"].all())
        and len(blocks) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES)
        and bool((blocks["ending_cash"] >= 5.0).all())
        and bool((blocks["net_return"] >= 0.0).all())
    )


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    # Keep the time-series comparison on the exact feature construction used
    # by H44; the path candidate only consumes already-present v1 fields.
    features = research.add_research_features(features)
    frames = candidate_frames(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    selection_blocks = chronological_blocks(selection_runs, SELECTION_BLOCK_SIZES)
    holdout_blocks = chronological_blocks(holdout_runs, HOLDOUT_BLOCK_SIZES)

    rows: list[dict[str, object]] = []
    logs_out: list[pd.DataFrame] = []
    for protocol_name, protocol in PROTOCOLS.items():
        for candidate, (frame, strategy) in frames.items():
            splits: list[tuple[str, list[str]]] = [
                ("selection20", selection_runs), ("holdout59", holdout_runs), ("all_test79", test_runs),
            ]
            splits.extend(("selection_block", block) for block in selection_blocks)
            splits.extend(("holdout_block", block) for block in holdout_blocks)
            for split, names in splits:
                for slip in SLIPPAGES:
                    summary, logs = replay(frame, strategy, names, slip, protocol)
                    row = {"protocol": protocol_name, "candidate": candidate, "split": split, "slippage": slip, **summary}
                    if split.endswith("_block"):
                        row["block"] = len([r for r in rows if r.get("protocol") == protocol_name and r.get("candidate") == candidate and r.get("split") == split]) // len(SLIPPAGES) + 1
                    rows.append(row)
                    if not logs.empty:
                        saved = logs.copy(); saved["protocol"] = protocol_name; saved["candidate"] = candidate; saved["split"] = split; saved["slippage"] = slip; logs_out.append(saved)

    result = pd.DataFrame(rows)
    selections: list[dict[str, object]] = []
    for protocol_name in PROTOCOLS:
        part = result[result["protocol"] == protocol_name]
        for candidate in CANDIDATES:
            current = part[part["candidate"] == candidate]
            selections.append({
                "protocol": protocol_name, "candidate": candidate,
                "selection_gate": gate_selection(current),
                "selection_trades": int(current[(current["split"] == "selection20") & current["slippage"].eq(SLIPPAGES[0])].iloc[0]["trades"]),
                "selection_ending_cash": float(current[(current["split"] == "selection20") & current["slippage"].eq(SLIPPAGES[0])].iloc[0]["ending_cash"]),
                "holdout_gate": gate_holdout(current),
            })
    sel_df = pd.DataFrame(selections)
    selected_by_protocol: dict[str, str | None] = {}
    holdout_by_protocol: dict[str, bool] = {}
    for protocol_name in PROTOCOLS:
        eligible = sel_df[(sel_df["protocol"] == protocol_name) & sel_df["selection_gate"]]
        if eligible.empty:
            selected_by_protocol[protocol_name] = None
            holdout_by_protocol[protocol_name] = False
            continue
        chosen = eligible.sort_values(["selection_ending_cash", "selection_trades"], ascending=[False, False]).iloc[0]
        selected_by_protocol[protocol_name] = str(chosen["candidate"])
        holdout_by_protocol[protocol_name] = bool(sel_df[(sel_df["protocol"] == protocol_name) & (sel_df["candidate"] == chosen["candidate"])].iloc[0]["holdout_gate"])

    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    sel_df.to_csv(output / "selection_summary.csv", index=False)
    if logs_out:
        pd.concat(logs_out, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    meta = {
        "format": "old-clean-h45-minimum-unit-compound-audit-v1",
        "source_root": str(source_root), "complete_runs": len(runs), "feature_rows": len(features),
        "candidates": list(CANDIDATES), "protocols": PROTOCOLS,
        "selection_block_sizes": list(SELECTION_BLOCK_SIZES), "holdout_block_sizes": list(HOLDOUT_BLOCK_SIZES),
        "selection_summary": selections, "selected_by_protocol": selected_by_protocol,
        "holdout_gate_by_protocol": holdout_by_protocol,
        "hypothesis": "The historical $5 notional floor may be an over-conservative unit assumption; a conditional five-share-only ledger may preserve the frozen signal's ten-fill chain.",
        "counter_hypothesis": "Even when the share-unit counterfactual avoids the artificial capital cliff, fee-adjusted edge and block stability still fail.",
        "unit_boundary": "The CLOB book minimum is documented in shares, while Gamma orderMinSize is separately documented as USDC notional; no live promotion is allowed until the exact market/order-type unit is observed.",
        "promotion_rule": "A protocol is admissible only if selection and holdout pass the same-ledger ten-fill, first-ten, and block gates; conditional share-only remains paper evidence until live market constraints are verified.",
        "selected_gate_passed": bool(any(holdout_by_protocol.values())),
        "new_data_collected": False, "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(sel_df.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()

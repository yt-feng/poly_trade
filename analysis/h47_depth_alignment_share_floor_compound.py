"""Re-test H36 after correcting the order-unit contract.

H47 keeps H36's score, thresholds, entry timing and fee fixed.  The only
changed protocol field is the conditional five-share floor used by H45/H46:
the replay does not assume a universal five-dollar notional floor.  This is a
paper sensitivity because the runtime Gamma order minimum is still a
separate, unresolved notional field.  Every split is evaluated on one
chronological cash ledger.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain

INITIAL_CASH = 10.0
MINIMUM_ORDER_NOTIONAL = 0.0
MINIMUM_ORDER_SHARES = 5.0
DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
FEE_RATE = 0.07
LOW_THRESHOLD = -12.334623264237315
HIGH_THRESHOLD = 13.003906573414652
STRATEGY = "h47_depth_alignment_share_floor"


def blocks(runs: list[str], sizes: tuple[int, ...]) -> list[list[str]]:
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    out: list[list[str]] = []
    cursor = 0
    for size in sizes:
        out.append(runs[cursor : cursor + size])
        cursor += size
    return out


def candidate_frame(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    market = pd.to_numeric(out["mid_up_prob_2m"], errors="coerce")
    depth = pd.to_numeric(out["bid_depth_imbalance_updown_2m"], errors="coerce")
    h30 = move * (1.0 - 2.0 * (market - 0.5).abs())
    alignment = np.sign(h30) * depth
    score = h30 * (1.0 + np.clip(alignment, -0.5, 0.5))
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[score.ge(HIGH_THRESHOLD)] = "buy_up"
    side.loc[score.le(LOW_THRESHOLD)] = "buy_down"
    out[f"{STRATEGY}_side"] = side
    out[f"{STRATEGY}_minute"] = 2
    out[f"{STRATEGY}_fraction"] = 0.04
    out["h47_h30_score"] = h30
    out["h47_alignment"] = alignment
    out["h47_score"] = score
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


def chain(logs: pd.DataFrame) -> dict[str, object]:
    events = paper_events(logs)
    kwargs = {
        "initial_cash": str(INITIAL_CASH),
        "minimum_order": "0.0000001",
        "required_fills": 10,
        "evidence_mode": "paper_upper_bound",
    }
    full = evaluate_compound_chain(events, **kwargs)
    prefix = evaluate_compound_chain(events[:10], **kwargs)
    return {
        "paper_chain_fills": int(full["fills"]),
        "paper_chain_ending_cash": full["ending_cash"],
        "paper_chain_minimum_cash": full["minimum_cash"],
        "paper_chain_passed": bool(full["paper_replay_passed"]),
        "paper_chain_failure_reason": full["failure_reason"],
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash": prefix["ending_cash"],
        "first_ten_minimum_cash": prefix["minimum_cash"],
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
        "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(frame: pd.DataFrame, names: list[str], slippage: float) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(x) for x in names])],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=slippage),
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    return {**metrics, **chain(logs)}, logs


def selection_gate(result: pd.DataFrame) -> bool:
    total = result[(result.split == "selection20") & result.slippage.eq(SLIPPAGES[0])]
    block_rows = result[result.split == "selection_block"]
    return bool(
        len(total) == 1
        and bool(total.iloc[0].paper_chain_passed)
        and bool(total.iloc[0].first_ten_passed)
        and len(block_rows) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES)
        and bool((block_rows.ending_cash >= 5.0).all())
        and bool((block_rows.net_return >= 0.0).all())
    )


def holdout_gate(result: pd.DataFrame) -> bool:
    total = result[result.split == "holdout59"]
    block_rows = result[result.split == "holdout_block"]
    return bool(
        len(total) == len(SLIPPAGES)
        and bool(total.paper_chain_passed.all())
        and bool(total.first_ten_passed.all())
        and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES)
        and bool((block_rows.ending_cash >= 5.0).all())
        and bool((block_rows.net_return >= 0.0).all())
    )


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    frame = candidate_frame(features)
    test = runs[4:]
    selection, holdout = test[:20], test[20:]
    split_names: list[tuple[str, list[str]]] = [
        ("selection20", selection),
        ("holdout59", holdout),
        ("all_test79", test),
    ]
    split_names += [("selection_block", names) for names in blocks(selection, SELECTION_BLOCK_SIZES)]
    split_names += [("holdout_block", names) for names in blocks(holdout, HOLDOUT_BLOCK_SIZES)]
    rows: list[dict[str, object]] = []
    logs_out: list[pd.DataFrame] = []
    for split, names in split_names:
        for slippage in SLIPPAGES:
            metrics, logs = replay(frame, names, slippage)
            rows.append({"split": split, "slippage": slippage, **metrics})
            if not logs.empty:
                saved = logs.copy()
                saved["split"] = split
                saved["slippage"] = slippage
                logs_out.append(saved)
    result = pd.DataFrame(rows)
    summary = {
        "selection_gate": selection_gate(result),
        "holdout_gate": holdout_gate(result),
        "selection_trades_005": int(result[(result.split == "selection20") & result.slippage.eq(0.005)].iloc[0].trades),
        "holdout_trades_005": int(result[(result.split == "holdout59") & result.slippage.eq(0.005)].iloc[0].trades),
        "selection_ending_cash_005": float(result[(result.split == "selection20") & result.slippage.eq(0.005)].iloc[0].ending_cash),
        "holdout_ending_cash_005": float(result[(result.split == "holdout59") & result.slippage.eq(0.005)].iloc[0].ending_cash),
        "selection_block_cash_005_010": [float(result[(result.split == "selection_block") & result.slippage.eq(x)].ending_cash.min()) for x in SLIPPAGES],
        "holdout_block_cash_005_010": [float(result[(result.split == "holdout_block") & result.slippage.eq(x)].ending_cash.min()) for x in SLIPPAGES],
        "selection_block_net_005_010": [float(result[(result.split == "selection_block") & result.slippage.eq(x)].net_return.min()) for x in SLIPPAGES],
        "holdout_block_net_005_010": [float(result[(result.split == "holdout_block") & result.slippage.eq(x)].net_return.min()) for x in SLIPPAGES],
    }
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False)
    if logs_out:
        pd.concat(logs_out, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    meta = {
        "format": "old-clean-h47-depth-alignment-share-floor-compound-v1",
        "source_root": str(source_root),
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": "H36_move_uncertainty_depth_alignment",
        "factor": "btc_move_2m * (1 - 2*abs(mid_up_prob_2m - 0.5)) * (1 + clip(sign(h30)*bid_depth_imbalance_updown_2m, -0.5, 0.5))",
        "thresholds": {"low": LOW_THRESHOLD, "high": HIGH_THRESHOLD, "selection_quantiles": [0.2, 0.8]},
        "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER_NOTIONAL, "minimum_order_shares": MINIMUM_ORDER_SHARES, "fee_rate": FEE_RATE, "settlement_seconds": 300.0, "cash_release_delay_seconds": DELAY_SECONDS, "slippages": list(SLIPPAGES), "entry_minute": 2, "fraction": 0.04},
        "selection_block_sizes": list(SELECTION_BLOCK_SIZES),
        "holdout_block_sizes": list(HOLDOUT_BLOCK_SIZES),
        "summary": summary,
        "hypothesis": "H36's depth-direction alignment may survive the corrected conditional five-share protocol even though the unverified universal $5 floor caused a capital cliff.",
        "counter_hypothesis": "The later block remains negative under the corrected share-unit contract, confirming regime instability rather than only the dollar floor.",
        "same_ledger_requirement": "one evolving chronological cash ledger per split; no reset between fills",
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "candidate_promoted_to_canary": False,
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(result[result.split.isin(["selection20", "holdout59", "selection_block", "holdout_block"])].to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.source_root, args.output)


if __name__ == "__main__":
    main()

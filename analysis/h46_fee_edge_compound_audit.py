"""Separate taker-fee drag from signal/regime failure under the H45 unit audit.

H46 holds the frozen H38 path-efficiency signal and conditional five-share
floor fixed, then compares the documented 0.07 crypto taker fee with an
optimistic zero-fee proxy. The zero-fee row is not a maker-fill claim: old
clean data has no queue or authenticated maker receipts. Both rows use one
chronological cash ledger and the same ten-fill and block gates.
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

import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from causal_path_efficiency_threshold_grid import gated as path_gate
from sequential_compound_gate import evaluate_compound_chain
import monthly_runs_quant_research_framework as research

INITIAL_CASH = 10.0
MINIMUM_ORDER_NOTIONAL = 0.0
MINIMUM_ORDER_SHARES = 5.0
DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
SELECTION_BLOCK_SIZES = (5, 5, 5, 5)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
PROTOCOLS = {"share_floor_taker_007": 0.07, "share_floor_zero_fee_proxy": 0.0}


def blocks(runs: list[str], sizes: tuple[int, ...]) -> list[list[str]]:
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    out, cursor = [], 0
    for size in sizes:
        out.append(runs[cursor:cursor + size]); cursor += size
    return out


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    return [{
        "status": "paper",
        "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
        "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
        "entry_cost": str(row.filled_cost),
        "fee": str(row.fee_usd),
        "payout": str(row.payout_at_settlement),
    } for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)]


def chains(logs: pd.DataFrame) -> dict[str, object]:
    events = paper_events(logs)
    kwargs = {"initial_cash": str(INITIAL_CASH), "minimum_order": "0.0000001", "required_fills": 10, "evidence_mode": "paper_upper_bound"}
    full = evaluate_compound_chain(events, **kwargs)
    prefix = evaluate_compound_chain(events[:10], **kwargs)
    return {
        "paper_chain_fills": int(full["fills"]), "paper_chain_ending_cash": full["ending_cash"], "paper_chain_minimum_cash": full["minimum_cash"], "paper_chain_passed": bool(full["paper_replay_passed"]), "paper_chain_failure_reason": full["failure_reason"],
        "first_ten_fills": int(prefix["fills"]), "first_ten_ending_cash": prefix["ending_cash"], "first_ten_minimum_cash": prefix["minimum_cash"], "first_ten_passed": bool(prefix["paper_replay_passed"]), "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(frame: pd.DataFrame, names: list[str], slip: float, fee_rate: float) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(x) for x in names])], "v1_adaptive_mix", study.ExecutionConfig(fixed_slippage=slip), INITIAL_CASH, MINIMUM_ORDER_NOTIONAL, DELAY_SECONDS, cooldown_events=0, capture_logs=True, fee_mode="polymarket_taker", fee_rate=fee_rate, min_order_shares=MINIMUM_ORDER_SHARES,
    )
    return {**metrics, **chains(logs)}, logs


def selection_gate(part: pd.DataFrame) -> bool:
    total = part[(part.split == "selection20") & part.slippage.eq(SLIPPAGES[0])]
    b = part[part.split == "selection_block"]
    return bool(not total.empty and bool(total.iloc[0].paper_chain_passed) and len(b) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES) and bool((b.ending_cash >= 5.0).all()) and bool((b.net_return >= 0.0).all()))


def holdout_gate(part: pd.DataFrame) -> bool:
    total, b = part[part.split == "holdout59"], part[part.split == "holdout_block"]
    return bool(len(total) == 2 and bool(total.paper_chain_passed.all()) and bool(total.first_ten_passed.all()) and len(b) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) and bool((b.ending_cash >= 5.0).all()) and bool((b.net_return >= 0.0).all()))


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError("SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH")
    features = research.add_research_features(features)
    frame = path_gate(features, 0.50)
    test = runs[4:]
    selection, holdout = test[:20], test[20:]
    split_names = [("selection20", selection), ("holdout59", holdout), ("all_test79", test)]
    split_names += [("selection_block", x) for x in blocks(selection, SELECTION_BLOCK_SIZES)]
    split_names += [("holdout_block", x) for x in blocks(holdout, HOLDOUT_BLOCK_SIZES)]
    rows, logs_out = [], []
    for protocol, fee in PROTOCOLS.items():
        for split, names in split_names:
            for slip in SLIPPAGES:
                metrics, logs = replay(frame, names, slip, fee)
                rows.append({"protocol": protocol, "split": split, "slippage": slip, **metrics})
                if not logs.empty:
                    saved = logs.copy(); saved["protocol"] = protocol; saved["split"] = split; saved["slippage"] = slip; logs_out.append(saved)
    result = pd.DataFrame(rows)
    summary = []
    for protocol in PROTOCOLS:
        part = result[result.protocol == protocol]
        summary.append({"protocol": protocol, "fee_rate": PROTOCOLS[protocol], "selection_gate": selection_gate(part), "holdout_gate": holdout_gate(part), "selection_trades": int(part[(part.split == "selection20") & part.slippage.eq(0.005)].iloc[0].trades), "holdout_trades": int(part[(part.split == "holdout59") & part.slippage.eq(0.005)].iloc[0].trades), "holdout_ending_cash_005": float(part[(part.split == "holdout59") & part.slippage.eq(0.005)].iloc[0].ending_cash), "holdout_block5_ending_005_010": [float(part[(part.split == "holdout_block") & part.slippage.eq(x)].iloc[-1].ending_cash) for x in SLIPPAGES], "holdout_block5_net_005_010": [float(part[(part.split == "holdout_block") & part.slippage.eq(x)].iloc[-1].net_return) for x in SLIPPAGES]})
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "metrics.csv", index=False); pd.DataFrame(summary).to_csv(output / "summary.csv", index=False)
    if logs_out: pd.concat(logs_out, ignore_index=True).to_csv(output / "trade_logs.csv", index=False)
    meta = {"format":"old-clean-h46-fee-edge-compound-audit-v1","source_root":str(source_root),"complete_runs":len(runs),"feature_rows":len(features),"candidate":"h38_path_efficiency_ge_050","path_threshold":0.50,"protocols":PROTOCOLS,"minimum_order_notional":0.0,"minimum_order_shares":5.0,"selection_block_sizes":list(SELECTION_BLOCK_SIZES),"holdout_block_sizes":list(HOLDOUT_BLOCK_SIZES),"hypothesis":"With the unit cliff removed, zero taker fee may restore the frozen H38 path candidate's later-block stability.","counter_hypothesis":"Even a zero-fee optimistic proxy fails the later block; the remaining issue is signal/regime stability.","summary":summary,"selected_gate_passed":False,"new_data_collected":False,"live_orders_enabled":False,"actual_fills":0,"verified_canary_fills":0}
    (output / "meta.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n'); print(json.dumps(meta,ensure_ascii=False)); print(pd.DataFrame(summary).to_string(index=False)); return meta


def main() -> None:
    p=argparse.ArgumentParser();p.add_argument("--source-root",required=True,type=Path);p.add_argument("--output",required=True,type=Path);a=p.parse_args();evaluate(a.source_root,a.output)


if __name__ == "__main__": main()

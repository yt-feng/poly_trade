"""Re-evaluate H42's causal edge model under the corrected share floor.

H42 was previously run with a universal five-dollar minimum.  H49 freezes
the same expanding logistic model, edge buffers, causal training rule and
time splits, but uses only the conditional five-share paper floor.  It is a
protocol correction audit, not a new feature search.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h42_causal_calibrated_edge as h42
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from sequential_compound_gate import evaluate_compound_chain

INITIAL_CASH = 10.0
MINIMUM_ORDER_NOTIONAL = 0.0
MINIMUM_ORDER_SHARES = 5.0
DELAY_SECONDS = 600.0
SLIPPAGES = (0.005, 0.010)
EDGE_BUFFERS = (0.0, 0.01, 0.02, 0.03)
SELECTION_BLOCK_SIZES = (4, 4, 4, 4, 4)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def blocks(runs: list[str], sizes: tuple[int, ...]) -> list[list[str]]:
    if sum(sizes) != len(runs):
        raise ValueError("BLOCK_SIZES_MUST_COVER_RUNS_EXACTLY")
    out, cursor = [], 0
    for size in sizes:
        out.append(runs[cursor : cursor + size]); cursor += size
    return out


def paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    return h42.paper_events(logs)


def chain(logs: pd.DataFrame) -> dict[str, object]:
    events = paper_events(logs)
    kwargs = {"initial_cash": str(INITIAL_CASH), "minimum_order": "0.0000001", "required_fills": 10, "evidence_mode": "paper_upper_bound"}
    full = evaluate_compound_chain(events, **kwargs)
    prefix = evaluate_compound_chain(events[:10], **kwargs)
    return {
        "paper_chain_fills": int(full["fills"]), "paper_chain_ending_cash": full["ending_cash"], "paper_chain_minimum_cash": full["minimum_cash"], "paper_chain_passed": bool(full["paper_replay_passed"]), "paper_chain_failure_reason": full["failure_reason"],
        "first_ten_fills": int(prefix["fills"]), "first_ten_ending_cash": prefix["ending_cash"], "first_ten_minimum_cash": prefix["minimum_cash"], "first_ten_passed": bool(prefix["paper_replay_passed"]), "first_ten_failure_reason": prefix["failure_reason"],
    }


def replay(frame: pd.DataFrame, names: list[str], edge_buffer: float, slippage: float) -> tuple[dict[str, object], pd.DataFrame]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(x) for x in names])], h42.STRATEGY,
        study.ExecutionConfig(fixed_slippage=slippage), INITIAL_CASH, MINIMUM_ORDER_NOTIONAL, DELAY_SECONDS,
        cooldown_events=0, capture_logs=True, fee_mode="polymarket_taker", fee_rate=0.07, min_order_shares=MINIMUM_ORDER_SHARES,
    )
    return {"edge_buffer": edge_buffer, "slippage": slippage, **metrics, **chain(logs)}, logs


def selection_gate(rows: pd.DataFrame, edge: float) -> bool:
    part = rows[rows.edge_buffer.eq(edge)]
    total = part[part.split == "selection20"]
    block_rows = part[part.split == "selection_block"]
    return bool(len(total) == len(SLIPPAGES) and bool(total.paper_chain_passed.all()) and bool(total.first_ten_passed.all()) and len(block_rows) == len(SELECTION_BLOCK_SIZES) * len(SLIPPAGES) and bool((block_rows.ending_cash >= 5.0).all()) and bool((block_rows.net_return >= 0.0).all()))


def holdout_gate(rows: pd.DataFrame, edge: float) -> bool:
    part = rows[rows.edge_buffer.eq(edge)]
    total = part[part.split == "holdout59"]
    block_rows = part[part.split == "holdout_block"]
    return bool(len(total) == len(SLIPPAGES) and bool(total.paper_chain_passed.all()) and bool(total.first_ten_passed.all()) and len(block_rows) == len(HOLDOUT_BLOCK_SIZES) * len(SLIPPAGES) and bool((block_rows.ending_cash >= 5.0).all()) and bool((block_rows.net_return >= 0.0).all()))


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    repo_root = source_root.resolve().parents[1]
    observed = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    source_commit = "bbda5f2050685f63153621a54641ebdfab943487"
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_SNAPSHOT_COMMIT_MISMATCH:{observed}:{source_commit}")
    frame, _coverage, runs = study.prepare_data(source_root)
    probabilities, history_counts = h42.expanding_probabilities(frame, runs)
    frame = frame.copy(); frame["h49_history_rows"] = history_counts
    test = runs[4:]; selection, holdout = test[:20], test[20:]
    split_names: list[tuple[str, list[str]]] = [("selection20", selection), ("holdout59", holdout), ("all_test79", test)]
    split_names += [("selection_block", names) for names in blocks(selection, SELECTION_BLOCK_SIZES)]
    split_names += [("holdout_block", names) for names in blocks(holdout, HOLDOUT_BLOCK_SIZES)]
    rows: list[dict[str, object]] = []; logs_out: list[pd.DataFrame] = []
    for edge in EDGE_BUFFERS:
        candidate = h42.add_edge_signal(frame, probabilities, edge)
        for split, names in split_names:
            for slip in SLIPPAGES:
                metrics, logs = replay(candidate, names, edge, slip)
                rows.append({"split": split, **metrics})
                if not logs.empty:
                    saved = logs.copy(); saved["split"] = split; saved["edge_buffer"] = edge; saved["slippage"] = slip; logs_out.append(saved)
    result = pd.DataFrame(rows)
    selection_summary=[]
    for edge in EDGE_BUFFERS:
        part=result[result.edge_buffer.eq(edge)]
        sel=part[part.split.eq("selection20")]
        sel005=sel[sel.slippage.eq(0.005)]
        selection_summary.append({'edge_buffer':edge,'selection_gate':selection_gate(result,edge),'selection_trades_005':int(sel005.iloc[0].trades),'selection_ending_cash_005':float(sel005.iloc[0].ending_cash),'selection_min_block_cash_010':float(part[(part.split=="selection_block")&(part.slippage==0.010)].ending_cash.min())})
    admissible=[x for x in selection_summary if x['selection_gate']]
    selected=max(admissible,key=lambda x:(x['selection_min_block_cash_010'],x['selection_ending_cash_005'] ))['edge_buffer'] if admissible else None
    holdout_summary=[]
    for edge in EDGE_BUFFERS:
        part=result[result.edge_buffer.eq(edge)]; h=part[part.split.eq("holdout59") & part.slippage.eq(0.005)]
        holdout_summary.append({'edge_buffer':edge,'holdout_gate':holdout_gate(result,edge),'holdout_trades_005':int(h.iloc[0].trades),'holdout_ending_cash_005':float(h.iloc[0].ending_cash),'holdout_min_block_cash_005_010':[float(part[(part.split=="holdout_block")&(part.slippage==x)].ending_cash.min()) for x in SLIPPAGES]})
    output.mkdir(parents=True,exist_ok=True); result.to_csv(output/'metrics.csv',index=False)
    if logs_out: pd.concat(logs_out,ignore_index=True).to_csv(output/'trade_logs.csv',index=False)
    meta={'format':'old-clean-h49-causal-calibrated-edge-share-floor-v1','source_root':str(source_root),'source_commit':source_commit,'complete_runs':len(runs),'feature_rows':len(frame),'candidate':'H42_expanding_logistic_score_market_probability_edge','edge_buffers':list(EDGE_BUFFERS),'protocol':{'initial_cash':INITIAL_CASH,'minimum_order_notional':MINIMUM_ORDER_NOTIONAL,'minimum_order_shares':MINIMUM_ORDER_SHARES,'fee_rate':0.07,'settlement_seconds':300.0,'cash_release_delay_seconds':DELAY_SECONDS,'slippages':list(SLIPPAGES),'entry_minute':2,'fraction':0.04},'selection_block_sizes':list(SELECTION_BLOCK_SIZES),'holdout_block_sizes':list(HOLDOUT_BLOCK_SIZES),'selection_summary':selection_summary,'selected_edge_buffer':selected,'holdout_summary':holdout_summary,'hypothesis':'H42 causal probability calibration may become selection-stable after removing the unverified universal $5 notional floor.','counter_hypothesis':'No edge buffer passes selection or the selected buffer still fails the independent holdout blocks under the corrected share floor.','same_ledger_requirement':'one evolving cash ledger per chronological split; no reset between fills','new_data_collected':False,'live_orders_enabled':False,'actual_fills':0,'verified_canary_fills':0,'candidate_promoted_to_canary':False}
    (output/'meta.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n'); print(json.dumps(meta,ensure_ascii=False)); print(result[result.split.isin(['selection20','holdout59','selection_block','holdout_block'])].to_string(index=False)); return meta


def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument('--source-root',required=True,type=Path); p.add_argument('--output',required=True,type=Path); a=p.parse_args(); evaluate(a.source_root,a.output)


if __name__=='__main__': main()

"""H404: old-clean portfolio/path-efficiency synergy under one cash ledger.

This is one predeclared interaction test, not a threshold sweep.  It reuses
the frozen ``portfolio_conservative_v2`` signal and the already registered
``path_efficiency_first2m >= 0.50`` state filter.  The preparation path omits
the unrelated rolling-logit model so the experiment is cheap and reproducible
on a hosted runner.  Selection and holdout use the same minimum-notional,
fee, slippage, settlement and cash-release contract as the strict compound
replay.  All results are paper upper bounds; they never become Canary fills.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import monthly_runs_quant_research_framework as qrf
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from quantml_candidate_execution_walk_forward import attach_execution_snapshots
from sequential_compound_gate import evaluate_compound_chain


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_COMPLETE_RUNS = 83
SELECTION_RUNS = 20
BLOCK_SIZES = (12, 12, 12, 12, 11)
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
MINIMUM_ORDER_SHARES = 5.0
FIXED_SLIPPAGE = 0.005
SETTLEMENT_SECONDS = 300.0
CASH_RELEASE_DELAY_SECONDS = 600.0
TAKER_FEE_RATE = 0.07
STRATEGY = "portfolio_conservative_v2"
CANDIDATE = f"{STRATEGY}__path_efficiency_ge_050"


def _prepare_lightweight(source_root: Path, *, strict_quality: bool) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Build only the features needed by the fixed portfolio signal."""
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    features = base.build_features(
        quotes,
        strict_quality=bool(strict_quality),
        max_snapshot_age_seconds=90.0,
    )
    # portfolio_conservative_v2 does not use the rolling-logit probability;
    # omitting it avoids fitting an unrelated model for this fixed test.
    features = qrf.v1.add_session_labels(features)
    features = qrf.v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = attach_execution_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    if features.empty:
        raise RuntimeError("NO_USABLE_COMPLETE_RUN_FEATURES")
    return features, coverage, runs


def add_fixed_candidate(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    choices = out.apply(lambda row: qrf.choose_trade(row, STRATEGY), axis=1)
    out[f"{STRATEGY}_side"] = choices.map(lambda item: item[0])
    out[f"{STRATEGY}_minute"] = choices.map(lambda item: item[1]).astype(int)
    out[f"{STRATEGY}_fraction"] = choices.map(lambda item: item[2]).fillna(0.0).astype(float)
    out[f"{STRATEGY}_component"] = choices.map(lambda item: item[3])
    keep = pd.to_numeric(out["path_efficiency_first2m"], errors="coerce").ge(0.50)
    out.loc[~keep, f"{STRATEGY}_side"] = "skip"
    out.loc[~keep, f"{STRATEGY}_minute"] = -1
    out.loc[~keep, f"{STRATEGY}_fraction"] = 0.0
    return out


def _paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    ordered = logs.sort_values("first_quote_ts", kind="stable")
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row["first_quote_ts"]).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row["release_ts"]).timestamp()),
            "entry_cost": str(row["filled_cost"]),
            "fee": str(row["fee_usd"]),
            "payout": str(row["payout_at_settlement"]),
        }
        for _, row in ordered.iterrows()
    ]


def _replay(frame: pd.DataFrame, names: list[str], split: str) -> dict[str, Any]:
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].astype(str).isin([str(name) for name in names])],
        STRATEGY,
        study.ExecutionConfig(fixed_slippage=FIXED_SLIPPAGE),
        INITIAL_CASH,
        MINIMUM_ORDER,
        CASH_RELEASE_DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=TAKER_FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    chain = evaluate_compound_chain(
        _paper_events(logs),
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    subset = frame[frame["run_name"].astype(str).isin([str(name) for name in names])]
    signal_rows = int((subset[f"{STRATEGY}_side"].astype(str) != "skip").sum())
    path_rows = int(pd.to_numeric(subset["path_efficiency_first2m"], errors="coerce").ge(0.50).sum())
    return {
        "split": split,
        "signal_rows": signal_rows,
        "path_filter_rows": path_rows,
        "paper_trades": int(metrics["trades"]),
        "paper_ending_cash": float(metrics["ending_cash"]),
        "paper_min_cash_before_settlement": float(metrics["min_cash_before_settlement"]),
        "paper_chain_fills": int(chain["fills"]),
        "paper_chain_ending_cash": float(chain["ending_cash"]),
        "paper_chain_minimum_cash_before_entry": float(chain["minimum_cash_before_entry"]),
        "paper_chain_passed": bool(chain["paper_replay_passed"]),
        "chain_failure_reason": chain["failure_reason"],
    }


def evaluate(source_root: Path, output: Path, *, source_commit: str, strict_quality: bool = True) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_SNAPSHOT_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, coverage, runs = _prepare_lightweight(source_root, strict_quality=strict_quality)
    frame = add_fixed_candidate(features)
    test_runs = runs[4:]
    selection_runs = test_runs[:SELECTION_RUNS]
    holdout_runs = test_runs[SELECTION_RUNS:]
    blocks: list[list[str]] = []
    cursor = 0
    for size in BLOCK_SIZES:
        blocks.append(holdout_runs[cursor:cursor + size])
        cursor += size
    if cursor != len(holdout_runs):
        raise RuntimeError("HOLDOUT_BLOCK_SIZES_MUST_COVER_ALL_RUNS")

    rows = [_replay(frame, selection_runs, "selection20"), _replay(frame, holdout_runs, "holdout59")]
    rows.extend(_replay(frame, names, f"block{index}") for index, names in enumerate(blocks, 1))
    selection, holdout = rows[0], rows[1]
    block_rows = rows[2:]
    selection_eligible = bool(
        selection["paper_chain_passed"]
        and selection["paper_chain_fills"] >= 10
        and selection["paper_chain_minimum_cash_before_entry"] >= MINIMUM_ORDER
    )
    holdout_passed = bool(
        holdout["paper_chain_passed"]
        and holdout["paper_chain_fills"] == 10
        and holdout["paper_chain_minimum_cash_before_entry"] >= MINIMUM_ORDER
    )
    holdout_block_gate = bool(
        block_rows
        and all(
            row["paper_chain_passed"]
            and row["paper_chain_fills"] == 10
            and row["paper_chain_minimum_cash_before_entry"] >= MINIMUM_ORDER
            for row in block_rows
        )
    )
    if not selection_eligible:
        decision = "reject_h404_selection_chain"
    elif not (holdout_passed and holdout_block_gate):
        decision = "reject_h404_holdout_chain"
    else:
        decision = "offline_candidate_promising_receipts_required"
    result: dict[str, Any] = {
        "format": "old-clean-h404-portfolio-path-synergy-v1",
        "hypothesis_id": "H404_portfolio_conservative_v2_path_efficiency",
        "source_root": str(source_root),
        "source_repo_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": bool(strict_quality),
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": CANDIDATE,
        "predeclared_filter": "path_efficiency_first2m >= 0.50",
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "rows": rows,
        "selection_eligible": selection_eligible,
        "holdout_passed": holdout_passed,
        "holdout_block_gate_passed": holdout_block_gate,
        "selection_rule": "one fixed candidate; no threshold tuning, no refit; selection must pass one same-ledger ten-fill paper chain before holdout is interpreted",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MINIMUM_ORDER,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE,
            "fixed_slippage": FIXED_SLIPPAGE,
            "settlement_seconds": SETTLEMENT_SECONDS,
            "cash_release_delay_seconds": CASH_RELEASE_DELAY_SECONDS,
            "same_ledger": True,
            "no_concatenation": True,
            "paper_only": True,
        },
        "decision": decision,
        "root_cause": "The predeclared path filter was applied without tuning. The portfolio signal can advance only if its fixed selection prefix forms one fee-adjusted, minimum-order, same-ledger ten-fill chain; otherwise a holdout result is not admissible.",
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "canary_gate_passed": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pd.DataFrame(rows).to_csv(output / "metrics.csv", index=False)
    coverage.to_csv(output / "coverage.csv", index=False)
    print(json.dumps({key: result[key] for key in ("candidate", "selection_eligible", "holdout_passed", "holdout_block_gate_passed", "decision")}, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--no-strict-quality", action="store_true")
    args = parser.parse_args()
    evaluate(args.source_root, args.output, source_commit=args.source_commit, strict_quality=not args.no_strict_quality)


if __name__ == "__main__":
    main()

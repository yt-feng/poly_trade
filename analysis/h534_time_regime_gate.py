"""Cloud-only H534 time-of-day gate ablation.

The QuantML overnight-seasonality card supplies only a transferable design
principle, not a BTC5m edge.  This module therefore compares one frozen UTC
00:00--02:00 entry-time gate with the unchanged all-hours H534 profile on the
pinned old-clean source.  It does not search a time-window grid, change model
weights, collect data, or enable orders.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
import sys
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h508_cash_fraction_depth_tradeoff as h508
import v1_execution_walk_forward as study


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
HYPOTHESIS_ID = "H534_UTC_OVERNIGHT_REGIME_GATE"
RESULT_FORMAT = "old-clean-h534-utc-regime-gate-v1"
FIXED_FRACTION = 0.55
FIXED_SLIPPAGE = 0.005
FIXED_LATENCY_SECONDS = 4.0
FIXED_DEPTH_HAIRCUT = 0.15
SELECTION_WINDOWS = 8
REQUIRED_FILLS = 10
INITIAL_CASH = 10.0
MINIMUM_ORDER_USD = 5.0


def overnight_utc_gate(frame: pd.DataFrame) -> pd.Series:
    """Return the preregistered UTC 00:00 <= entry < 02:00 mask."""
    if "entry_ts" not in frame.columns:
        raise ValueError("entry_ts_missing")
    timestamps = pd.to_datetime(frame["entry_ts"], utc=True, errors="coerce")
    return timestamps.notna() & timestamps.dt.hour.isin([0, 1])


def _replay(candidates: pd.DataFrame, run_names: list[str], quotes: dict[str, pd.DataFrame]) -> dict[str, Any]:
    return h508._replay_fraction_depth(
        candidates,
        run_names,
        quotes,
        gross_cash_fraction=FIXED_FRACTION,
        latency_seconds=FIXED_LATENCY_SECONDS,
        depth_haircut=FIXED_DEPTH_HAIRCUT,
        exit_cache={},
    )


def _build_candidates(source_root: Path) -> tuple[pd.DataFrame, list[str], Any]:
    features, coverage, runs = study.prepare_data(
        source_root, strict_quality=True, max_snapshot_age_seconds=90.0
    )
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
    frames: list[pd.DataFrame] = []
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = h508._fit(train)
        if model is None:
            continue
        current = h508.apply_confidence_gate(
            h508._candidate_frame(model, medians, by_run[run_name], FIXED_SLIPPAGE)
        )
        if not current.empty:
            current = current.copy()
            current["model_run_index"] = index
            frames.append(current)
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), runs, coverage


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    selection = rows[:SELECTION_WINDOWS]
    holdout = rows[SELECTION_WINDOWS:]

    def passes(row: dict[str, Any]) -> bool:
        return bool(
            row.get("first_ten_passed")
            and int(row.get("first_ten_fills", 0)) == REQUIRED_FILLS
            and float(row.get("first_ten_min_cash_before_entry", -1.0)) >= MINIMUM_ORDER_USD
            and float(row.get("first_ten_ending_cash", -1.0)) >= INITIAL_CASH
        )

    return {
        "selection_window_count": len(selection),
        "holdout_window_count": len(holdout),
        "selection_all_ten": bool(selection) and all(passes(row) for row in selection),
        "holdout_all_ten": bool(holdout) and all(passes(row) for row in holdout),
        "selection_passed_rows": sum(passes(row) for row in selection),
        "holdout_passed_rows": sum(passes(row) for row in holdout),
        "selection_min_cash": min((float(row["first_ten_ending_cash"]) for row in selection), default=INITIAL_CASH),
        "holdout_min_cash": min((float(row["first_ten_ending_cash"]) for row in holdout), default=INITIAL_CASH),
        "selection_min_fills": min((int(row["first_ten_fills"]) for row in selection), default=0),
        "holdout_min_fills": min((int(row["first_ten_fills"]) for row in holdout), default=0),
    }


def evaluate(source_root: Path, output: Path, *, source_commit: str = SOURCE_COMMIT) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    candidates, runs, coverage = _build_candidates(source_root)
    test_runs = runs[4:]
    windows = h508.rolling_windows(test_runs, window_runs=h508.WINDOW_RUNS, stride=h508.WINDOW_STRIDE)
    quotes = h508._quote_index(source_root, set(candidates["slug"].astype(str))) if not candidates.empty else {}

    policies = {
        "all_hours_baseline": candidates,
        "utc_00_02_overnight_gate": candidates.loc[overnight_utc_gate(candidates)].copy(),
    }
    window_rows: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for name, frame in policies.items():
        rows: list[dict[str, Any]] = []
        for window in windows:
            rows.append({
                "window_index": int(window["window_index"]),
                "start_run": window["start_run"],
                "end_run": window["end_run"],
                **_replay(frame, window["runs"], quotes),
            })
        window_rows[name] = rows
        summaries[name] = _summary(rows)

    baseline = summaries["all_hours_baseline"]
    gated = summaries["utc_00_02_overnight_gate"]
    if gated["holdout_all_ten"] and gated["holdout_min_cash"] > baseline["holdout_min_cash"]:
        decision = "retain_utc_00_02_gate_as_paper_candidate_owner_receipts_required"
    else:
        decision = "reject_utc_00_02_gate_retain_h534_all_hours"

    result: dict[str, Any] = {
        "format": RESULT_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": int(len(candidates)),
        "transfer_basis": "QuantML BTC overnight-seasonality card; fixed UTC gate, no window search",
        "policies": summaries,
        "window_rows": window_rows,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_usd": MINIMUM_ORDER_USD,
            "minimum_order_shares": 5.0,
            "taker_fee_rate": 0.07,
            "slippage": FIXED_SLIPPAGE,
            "latency_seconds": FIXED_LATENCY_SECONDS,
            "depth_haircut": FIXED_DEPTH_HAIRCUT,
            "cash_release_delay_seconds": 900.0,
            "required_consecutive_fills": REQUIRED_FILLS,
            "same_evolving_ledger": True,
            "no_cross_window_concatenation": True,
            "paper_only": True,
        },
        "selection_rule": "No time-window tuning; compare one fixed UTC 00:00-02:00 gate against unchanged all-hours baseline on selection and independent holdout.",
        "decision": decision,
        "root_cause_scope": "A time-of-day gate can alter opportunity density, but cannot establish owner-scoped fill, settlement, released-cash or balance continuity.",
        "new_data_collected": False,
        "model_weights_changed": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "canary_gate_passed": False,
        "owner_scoped_evidence_required": True,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    coverage.to_csv(output / "coverage.csv", index=False)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, args.output, source_commit=args.source_commit)
    print(json.dumps({"hypothesis_id": result["hypothesis_id"], "decision": result["decision"], "actual_fills": 0, "verified_canary_fills": 0}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

"""H57: causal entry-feature gates for the H56 losing windows.

The H56 rolling audit showed that ten fills are available, but positive growth
fails in contiguous windows. H57 freezes a small entry-time gate catalogue,
selects at most one gate per candidate on selection20, then evaluates that
choice once on the holdout rolling windows. The holdout never selects a gate.
All gates use only fields available at entry; outcome/PnL is emitted only as a
post-hoc diagnostic for the already-frozen replay.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
import h55_tick_depth_prefix_audit as h55
from h56_rolling_window_stability import rolling_windows, WINDOW_SIZE, WINDOW_STRIDE, CONFIGS

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
GATE_CATALOGUE = (
    {"gate_id": "baseline", "description": "no additional entry gate"},
    {"gate_id": "market_confidence_010", "min_abs_market_deviation": 0.10, "description": "abs(mid_up_prob_2m - 0.5) >= 0.10"},
    {"gate_id": "market_confidence_015", "min_abs_market_deviation": 0.15, "description": "abs(mid_up_prob_2m - 0.5) >= 0.15"},
    {"gate_id": "raw_edge_000", "min_raw_edge": 0.00, "description": "current-side probability minus quote and taker fee >= 0.00"},
    {"gate_id": "raw_edge_020", "min_raw_edge": 0.02, "description": "current-side probability minus quote and taker fee >= 0.02"},
    {"gate_id": "raw_edge_040", "min_raw_edge": 0.04, "description": "current-side probability minus quote and taker fee >= 0.04"},
)


def entry_table(frame: pd.DataFrame, candidate: str) -> pd.DataFrame:
    out = frame[["run_name", "market_id", "first_quote_ts", "btc_move_2m", "mid_up_prob_2m", "path_efficiency_first2m", "session_et", f"{candidate}_side", f"{candidate}_minute"]].copy()
    out["side"] = out[f"{candidate}_side"].astype(str)
    out["entry_minute"] = pd.to_numeric(out[f"{candidate}_minute"], errors="coerce").fillna(-1).astype(int)
    out["market_probability"] = pd.to_numeric(out["mid_up_prob_2m"], errors="coerce")
    out["abs_market_deviation"] = (out["market_probability"] - 0.5).abs()
    out["score"] = pd.to_numeric(out["btc_move_2m"], errors="coerce") * (1.0 - 2.0 * out["abs_market_deviation"])
    out["signal"] = out["side"].isin(["buy_up", "buy_down"])
    out["quote_price"] = np.nan
    out["quote_spread"] = np.nan
    out["quote_size"] = np.nan
    for side in ("buy_up", "buy_down"):
        mask = out["side"].eq(side)
        for minute in (1, 2, 4):
            rows = mask & out["entry_minute"].eq(minute)
            prefix = f"exec_{side}_{minute}m_d4"
            if rows.any():
                out.loc[rows, "quote_price"] = pd.to_numeric(frame.loc[rows, f"{prefix}_quote_price"], errors="coerce")
                out.loc[rows, "quote_spread"] = pd.to_numeric(frame.loc[rows, f"{prefix}_quote_spread"], errors="coerce")
                out.loc[rows, "quote_size"] = pd.to_numeric(frame.loc[rows, f"{prefix}_quote_size"], errors="coerce")
    side_probability = np.where(out["side"].eq("buy_up"), out["market_probability"], 1.0 - out["market_probability"])
    out["side_probability"] = pd.to_numeric(side_probability, errors="coerce")
    out["taker_fee_per_share"] = h55.FEE_RATE * out["quote_price"] * (1.0 - out["quote_price"])
    out["raw_edge"] = out["side_probability"] - out["quote_price"] - out["taker_fee_per_share"]
    return out


def gate_mask(entries: pd.DataFrame, spec: dict[str, Any]) -> pd.Series:
    mask = entries["signal"].copy()
    if "min_abs_market_deviation" in spec:
        mask &= entries["abs_market_deviation"].ge(float(spec["min_abs_market_deviation"]))
    if "min_raw_edge" in spec:
        mask &= entries["raw_edge"].ge(float(spec["min_raw_edge"]))
    return mask.fillna(False)


def apply_gate(frame: pd.DataFrame, candidate: str, spec: dict[str, Any]) -> pd.DataFrame:
    out = frame.copy()
    entries = entry_table(out, candidate)
    keep = gate_mask(entries, spec)
    out.loc[~keep, f"{candidate}_side"] = "skip"
    out.loc[~keep, f"{candidate}_fraction"] = 0.0
    return out


def selection_replay(frame: pd.DataFrame, candidate: str, spec: dict[str, Any], names: list[str]) -> list[dict[str, object]]:
    gated = apply_gate(frame, candidate, spec)
    rows = []
    for config in CONFIGS:
        metrics, _logs = h55.replay(gated, candidate, names, config["tick_size"], config["depth_floor"], config["slippage"])
        rows.append({"candidate": candidate, "gate_id": spec["gate_id"], "split": "selection20", **config, **metrics})
    return rows


def diagnostic_events(frame: pd.DataFrame, candidate: str, windows: list[tuple[int, list[str]]]) -> list[dict[str, object]]:
    entries = entry_table(frame, candidate).drop_duplicates(["run_name", "market_id"]).set_index(["run_name", "market_id"], drop=False)
    out: list[dict[str, object]] = []
    for window_index, (_start, names) in enumerate(windows, start=1):
        for config in CONFIGS:
            metrics, logs = h55.replay(frame, candidate, names, config["tick_size"], config["depth_floor"], config["slippage"])
            if logs.empty:
                continue
            logs = logs.sort_values("first_quote_ts", kind="stable").head(10)
            for fill_index, row in enumerate(logs.itertuples(index=False), start=1):
                key = (str(row.run_name), str(row.market_id))
                feature = entries.loc[key] if key in entries.index else pd.Series(dtype=object)
                out.append({
                    "candidate": candidate,
                    "window_index": window_index,
                    "fill_index": fill_index,
                    **config,
                    "run_name": str(row.run_name),
                    "market_id": str(row.market_id),
                    "side": str(row.side),
                    "entry_ts": str(row.first_quote_ts),
                    "filled_cost": float(row.filled_cost),
                    "fee_usd": float(row.fee_usd),
                    "payout_at_settlement": float(row.payout_at_settlement),
                    "pnl_usd": float(row.pnl_usd),
                    "entry_score": float(feature.get("score", np.nan)),
                    "market_probability": float(feature.get("market_probability", np.nan)),
                    "abs_market_deviation": float(feature.get("abs_market_deviation", np.nan)),
                    "raw_edge": float(feature.get("raw_edge", np.nan)),
                    "quote_price_raw": float(feature.get("quote_price", np.nan)),
                    "quote_spread": float(feature.get("quote_spread", np.nan)),
                    "quote_size": float(feature.get("quote_size", np.nan)),
                    "path_efficiency_first2m": float(feature.get("path_efficiency_first2m", np.nan)),
                    "session_et": feature.get("session_et"),
                    "diagnostic_only": True,
                })
    return out


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, object]:
    observed = subprocess.check_output(["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h55.add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, object]] = []
    for candidate in h55.CANDIDATES:
        for spec in GATE_CATALOGUE:
            selection_rows.extend(selection_replay(frame, candidate, spec, selection_runs))
    selection = pd.DataFrame(selection_rows)
    selected: dict[str, dict[str, Any] | None] = {}
    for candidate in h55.CANDIDATES:
        part = selection[selection["candidate"].eq(candidate)]
        eligible: list[dict[str, Any]] = []
        for spec in GATE_CATALOGUE:
            rows = part[part["gate_id"].eq(spec["gate_id"])]
            if len(rows) != len(CONFIGS):
                continue
            if bool(rows["first_ten_passed"].all()) and bool((rows["first_ten_fills"] == 10).all()):
                eligible.append({"gate_id": spec["gate_id"], "min_selection_cash": float(rows["first_ten_ending_cash_usd"].min()), "selection_cash_sum": float(rows["first_ten_ending_cash_usd"].sum())})
        if eligible:
            selected[candidate] = sorted(eligible, key=lambda x: (-x["min_selection_cash"], -x["selection_cash_sum"], x["gate_id"]))[0]
        else:
            selected[candidate] = None

    holdout_rows: list[dict[str, object]] = []
    for candidate in h55.CANDIDATES:
        spec = next((x for x in GATE_CATALOGUE if x["gate_id"] == (selected[candidate] or {}).get("gate_id")), None)
        if spec is None:
            continue
        gated = apply_gate(frame, candidate, spec)
        for window_index, (_start, names) in enumerate(windows, start=1):
            for config in CONFIGS:
                metrics, _logs = h55.replay(gated, candidate, names, config["tick_size"], config["depth_floor"], config["slippage"])
                holdout_rows.append({"candidate": candidate, "gate_id": spec["gate_id"], "split": f"holdout_window_{window_index}", "window_index": window_index, **config, **metrics})
    holdout = pd.DataFrame(holdout_rows)
    gates: dict[str, dict[str, object]] = {}
    for candidate in h55.CANDIDATES:
        chosen = selected[candidate]
        rows = holdout[holdout["candidate"].eq(candidate)] if not holdout.empty else pd.DataFrame()
        gate_pass = bool(chosen and len(rows) == len(windows) * len(CONFIGS) and rows["first_ten_passed"].all() and (rows["first_ten_fills"] == 10).all())
        gates[candidate] = {"selected_gate": chosen, "rolling_holdout_gate": gate_pass, "holdout_rows": rows.to_dict(orient="records")}

    diagnostics = diagnostic_events(frame, h55.CANDIDATES[0], windows) + diagnostic_events(frame, h55.CANDIDATES[1], windows)
    summary = {
        "format": "old-clean-h57-causal-loss-window-diagnostic-v1",
        "source_root": str(source_root), "source_commit": source_commit, "observed_source_commit": observed,
        "complete_runs": len(runs), "feature_rows": len(features), "candidates": list(h55.CANDIDATES),
        "gate_catalogue": list(GATE_CATALOGUE),
        "selection_rule": "select only among gates passing both nominal and stress exact-ten selection20 prefix; rank by minimum selection ending cash then sum; never use holdout for selection",
        "protocol": {"initial_cash_usd": h55.INITIAL_CASH, "minimum_order_shares": h55.MINIMUM_ORDER_SHARES, "fee_rate": h55.FEE_RATE, "cash_release_delay_seconds": h55.CASH_RELEASE_DELAY_SECONDS, "configs": list(CONFIGS), "same_ledger": True, "required_fills": 10, "no_refit": True, "no_concatenation": True},
        "documentation_sources": h55.DOCS,
        "window_size_runs": WINDOW_SIZE, "window_stride_runs": WINDOW_STRIDE, "window_count": len(windows),
        "selection_rows": selection.to_dict(orient="records"), "gates": gates,
        "baseline_event_diagnostics": diagnostics,
        "all_candidates_rolling_gate": bool(all(v["rolling_holdout_gate"] for v in gates.values())),
        "paper_only": True, "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    selection.to_csv(output / "selection_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(diagnostics).to_csv(output / "baseline_event_diagnostics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selected": {k: (v or {}).get("gate_id") for k, v in selected.items()}, "rolling_gates": {k: v["rolling_holdout_gate"] for k, v in gates.items()}}, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()

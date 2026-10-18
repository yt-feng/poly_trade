"""H67: predeclared consensus gates for the frozen H30 signal.

H65/H66 showed that confidence, quote and path filters do not repair every
rolling loss cell.  H67 tests whether independent, already-registered entry
signals can confirm H30 without refitting or using future labels.  Every
variant is evaluated on selection20 first; only one selection winner is then
run on the untouched nine rolling holdout windows.  Paper replay only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h55_tick_depth_prefix_audit as h55
from h56_rolling_window_stability import rolling_windows, WINDOW_SIZE, WINDOW_STRIDE, CONFIGS
from registry_small_cash_holdout import prepare_registry_data, add_signals

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
BASE = "h30_move_uncertainty"
AUXILIARIES = ("micro_book_consensus_up", "v1_active_fill_mix")
VARIANTS = (
    "h30_baseline",
    "h30_micro_agreement",
    "h30_v1_agreement",
    "h30_either_agreement",
)


def add_consensus_variants(features: pd.DataFrame) -> pd.DataFrame:
    out = h55.add_frozen_candidates(features)
    for strategy in AUXILIARIES:
        out = add_signals(out, strategy)
    base_side = out[f"{BASE}_side"].astype(str)
    micro_side = out["micro_book_consensus_up_side"].astype(str)
    v1_side = out["v1_active_fill_mix_side"].astype(str)
    agreement_micro = base_side.isin(["buy_up", "buy_down"]) & base_side.eq(micro_side)
    agreement_v1 = base_side.isin(["buy_up", "buy_down"]) & base_side.eq(v1_side)
    masks = {
        "h30_baseline": base_side.isin(["buy_up", "buy_down"]),
        "h30_micro_agreement": agreement_micro,
        "h30_v1_agreement": agreement_v1,
        "h30_either_agreement": agreement_micro | agreement_v1,
    }
    for variant, keep in masks.items():
        out[f"{variant}_side"] = base_side.where(keep, "skip")
        out[f"{variant}_minute"] = out[f"{BASE}_minute"].astype(int)
        out[f"{variant}_fraction"] = out[f"{BASE}_fraction"].astype(float).where(keep, 0.0)
        out[f"{variant}_component"] = f"h67_{variant}"
    return out


def replay(frame: pd.DataFrame, variant: str, names: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        metrics, _logs = h55.replay(
            frame, variant, names, config["tick_size"], config["depth_floor"], config["slippage"]
        )
        rows.append({"candidate": variant, **config, **metrics})
    return rows


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = prepare_registry_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_consensus_variants(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    windows = rolling_windows(holdout_runs)

    selection_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    for variant in VARIANTS:
        for row in replay(frame, variant, selection_runs):
            row.update({"split": "selection20"})
            selection_rows.append(row)
        signal = frame[f"{variant}_side"].isin(["buy_up", "buy_down"])
        signal_rows.append({
            "candidate": variant,
            "selection_signal_rows": int(signal[frame["run_name"].isin(selection_runs)].sum()),
            "holdout_signal_rows": int(signal[frame["run_name"].isin(holdout_runs)].sum()),
            "all_signal_rows": int(signal.sum()),
        })
    selection = pd.DataFrame(selection_rows)

    eligible: list[dict[str, Any]] = []
    for variant in VARIANTS:
        part = selection[selection["candidate"].eq(variant)]
        if len(part) == len(CONFIGS) and bool(part["first_ten_passed"].all()) and bool((part["first_ten_fills"] == 10).all()):
            eligible.append({
                "candidate": variant,
                "min_selection_cash": float(part["first_ten_ending_cash_usd"].min()),
                "selection_cash_sum": float(part["first_ten_ending_cash_usd"].sum()),
            })
    selected = sorted(eligible, key=lambda x: (-x["min_selection_cash"], -x["selection_cash_sum"], x["candidate"]))[0] if eligible else None

    holdout_rows: list[dict[str, Any]] = []
    if selected:
        variant = selected["candidate"]
        for window_index, (_start, names) in enumerate(windows, start=1):
            for row in replay(frame, variant, names):
                row.update({"split": f"holdout_window_{window_index}", "window_index": window_index})
                holdout_rows.append(row)
    holdout = pd.DataFrame(holdout_rows)
    holdout_gate = bool(
        selected
        and len(holdout) == len(windows) * len(CONFIGS)
        and bool(holdout["first_ten_passed"].all())
        and bool((holdout["first_ten_fills"] == 10).all())
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h67-consensus-gate-v1",
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "base_candidate": BASE,
        "auxiliary_candidates": list(AUXILIARIES),
        "variants": list(VARIANTS),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "window_size_runs": WINDOW_SIZE,
        "window_stride_runs": WINDOW_STRIDE,
        "window_count": len(windows),
        "selection_rule": "select one variant only when both nominal and stress selection20 cells pass exactly ten chronological fills with positive first-ten cash growth; evaluate that variant once on nine untouched rolling windows",
        "eligible_selection_variants": eligible,
        "selected_variant": selected,
        "holdout_gate": holdout_gate,
        "protocol": {
            "initial_cash_usd": h55.INITIAL_CASH,
            "minimum_order_shares": h55.MINIMUM_ORDER_SHARES,
            "fee_rate": h55.FEE_RATE,
            "cash_release_delay_seconds": h55.CASH_RELEASE_DELAY_SECONDS,
            "configs": list(CONFIGS),
            "same_ledger": True,
            "required_fills": 10,
            "no_concatenation": True,
            "no_refit": True,
        },
        "paper_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "decision": "promote_only_if_selected_variant_passes_all_18_untouched_h30_cells",
    }
    output.mkdir(parents=True, exist_ok=True)
    selection.to_csv(output / "selection_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    pd.DataFrame(signal_rows).to_csv(output / "signal_counts.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(json.dumps({"selected_variant": selected, "holdout_gate": holdout_gate}, sort_keys=True))
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

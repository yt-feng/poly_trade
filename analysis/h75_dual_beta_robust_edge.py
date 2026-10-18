"""H75: dual-beta robust fair-probability edge on unchanged old-clean data.

The model is a predeclared, causal entry-time transform motivated by a public
BTC 5m study: use the conservative probability from beta=0.83 and beta=1.36
under a local Gaussian reference, then require a 2-cent fee-adjusted edge.
No beta is fitted on holdout data and no orders are sent.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h55_tick_depth_prefix_audit as h55
import h57_causal_loss_window_diagnostic as h57
from h56_rolling_window_stability import CONFIGS, rolling_windows

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
CANDIDATE = "h75_dual_beta_robust_edge"
BETAS = (0.83, 1.36)
EDGE_THRESHOLD = 0.02
TAU_SECONDS = 180.0
FRACTION = 0.04


def normal_cdf(x: pd.Series) -> pd.Series:
    values = pd.to_numeric(x, errors="coerce")
    return values.map(lambda v: np.nan if pd.isna(v) else 0.5 * (1.0 + math.erf(float(v) / math.sqrt(2.0)))
)


def add_candidate(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = frame.copy()
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    sigma = pd.to_numeric(out["realized_vol_first2m"], errors="coerce")
    denom = sigma * math.sqrt(TAU_SECONDS)
    z = move.where(denom > 0).div(denom.where(denom > 0))
    probs = [normal_cdf(z * beta) for beta in BETAS]
    robust_up = pd.concat(probs, axis=1).min(axis=1, skipna=False)
    price_up = pd.to_numeric(out["buy_up_price_2m"], errors="coerce")
    price_down = pd.to_numeric(out["buy_down_price_2m"], errors="coerce")
    fee_up = h55.FEE_RATE * price_up * (1.0 - price_up)
    fee_down = h55.FEE_RATE * price_down * (1.0 - price_down)
    edge_up = robust_up - price_up - fee_up
    edge_down = (1.0 - robust_up) - price_down - fee_down
    up = edge_up.ge(EDGE_THRESHOLD)
    down = edge_down.ge(EDGE_THRESHOLD)
    # A single side is chosen; a tie or invalid estimate is fail-closed.
    choose_up = up & (~down | edge_up.gt(edge_down))
    choose_down = down & (~up | edge_down.gt(edge_up))
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[choose_up] = "buy_up"
    side.loc[choose_down] = "buy_down"
    out[f"{CANDIDATE}_side"] = side
    out[f"{CANDIDATE}_minute"] = 2
    out[f"{CANDIDATE}_fraction"] = np.where(side.isin(["buy_up", "buy_down"]), FRACTION, 0.0)
    out[f"{CANDIDATE}_component"] = "h75_dual_beta_0p83_1p36"
    diagnostics = pd.DataFrame({
        "z": z,
        "robust_up_probability": robust_up,
        "edge_up": edge_up,
        "edge_down": edge_down,
        "side": side,
        "signal": side.isin(["buy_up", "buy_down"]),
    })
    return out, diagnostics


def replay(frame: pd.DataFrame, names: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        metrics, _logs = h55.replay(
            frame, CANDIDATE, names, config["tick_size"], config["depth_floor"], config["slippage"]
        )
        rows.append({**config, **metrics})
    return rows


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = h57.study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame, diagnostics = add_candidate(features)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    windows = rolling_windows(holdout_runs)
    selection = pd.DataFrame([{"split": "selection20", **row} for row in replay(frame, selection_runs)])
    holdout_rows: list[dict[str, Any]] = []
    for window_index, (_start, names) in enumerate(windows, start=1):
        for row in replay(frame, names):
            holdout_rows.append({"split": f"holdout_window_{window_index}", "window_index": window_index, **row})
    holdout = pd.DataFrame(holdout_rows)
    selection_pass = bool(selection["first_ten_passed"].all() and (selection["first_ten_fills"] == 10).all())
    summary = {
        "format": "old-clean-h75-dual-beta-robust-edge-v1",
        "source_root": str(source_root), "source_commit": source_commit,
        "observed_source_commit": observed, "complete_runs": len(runs), "feature_rows": len(features),
        "candidate": CANDIDATE,
        "model": {
            "betas": list(BETAS), "tau_seconds": TAU_SECONDS,
            "z": "btc_move_2m / (realized_vol_first2m * sqrt(tau_seconds))",
            "robust_up": "min(Phi(0.83*z), Phi(1.36*z))",
            "edge_threshold_after_fee": EDGE_THRESHOLD,
            "fraction": FRACTION,
        },
        "signal_rows": int(diagnostics["signal"].sum()), "entry_rows": int(len(diagnostics)),
        "selection_runs": len(selection_runs), "holdout_runs": len(holdout_runs), "window_count": len(windows),
        "selection_pass": selection_pass,
        "selection_min_cash_usd": float(selection["first_ten_ending_cash_usd"].min()),
        "selection_cash_sum_usd": float(selection["first_ten_ending_cash_usd"].sum()),
        "holdout_pass_cells": int(holdout["first_ten_passed"].sum()), "holdout_total_cells": int(len(holdout)),
        "holdout_min_cash_usd": float(holdout["first_ten_ending_cash_usd"].min()),
        "holdout_gate": bool(len(holdout) == len(windows) * len(CONFIGS) and holdout["first_ten_passed"].all() and (holdout["first_ten_fills"] == 10).all()),
        "protocol": {
            "initial_cash_usd": h55.INITIAL_CASH, "minimum_order_shares": h55.MINIMUM_ORDER_SHARES,
            "fee_rate": h55.FEE_RATE, "cash_release_delay_seconds": h55.CASH_RELEASE_DELAY_SECONDS,
            "configs": list(CONFIGS), "same_ledger": True, "required_fills": 10,
            "no_concatenation": True, "no_refit": True,
        },
        "paper_only": True, "actual_fills": 0, "verified_canary_fills": 0,
        "new_data_collected": False, "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    selection.to_csv(output / "selection_metrics.csv", index=False)
    holdout.to_csv(output / "holdout_metrics.csv", index=False)
    diagnostics.to_csv(output / "entry_diagnostics.csv", index=False)
    (output / "meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("signal_rows", "selection_pass", "holdout_pass_cells", "holdout_total_cells", "holdout_min_cash_usd", "holdout_gate")}, sort_keys=True))
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

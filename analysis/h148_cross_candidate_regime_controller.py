"""H148: causal cross-candidate controller on the unchanged old-clean data.

H147 found that the predeclared H30 and H38 entry gates fail in different
rolling regimes.  H148 tests one narrow, causal controller: use only the
previous chronological window to choose between those two frozen gates, and
abstain when neither previous window produced a valid ten-fill paper chain.
The current window is never used for selection.  Each window has its own
diagnostic ledger; windows are never concatenated into a Canary chain.

This module only performs an offline paper replay when run in GitHub Actions.
It never submits an order or treats a quote-derived row as a confirmed fill.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
from typing import Any, Iterable

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import h55_tick_depth_prefix_audit as h55
import h64_high_confidence_gate as h64
from h56_rolling_window_stability import CONFIGS, rolling_windows, WINDOW_SIZE, WINDOW_STRIDE

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
REQUIRED_FILLS = 10
CONTROLLER_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "candidate": "h30_move_uncertainty",
        "gate": {"gate_id": "market_confidence_020", "min_abs_market_deviation": 0.20},
    },
    {
        "candidate": "h38_move_uncertainty_midpath",
        "gate": {
            "gate_id": "confidence025_edge020",
            "min_abs_market_deviation": 0.25,
            "min_raw_edge": 0.02,
        },
    },
)


def _number(value: Any, field: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_numeric") from exc
    if not pd.notna(out):
        raise ValueError(f"{field}_must_be_finite")
    return out


def row_passes(row: dict[str, Any]) -> bool:
    """Return whether one frozen replay configuration passes the ten-fill gate."""
    return bool(row.get("first_ten_passed")) and int(row.get("first_ten_fills", 0)) == REQUIRED_FILLS


def choose_from_previous(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose a candidate using only a completed previous window.

    A candidate is eligible only when every declared tick/depth/slippage
    configuration in the previous window passes.  Ranking uses the worst
    ending cash, then the sum of ending cash, then the candidate name.  The
    current window is never passed to this function.
    """
    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        candidate = str(row.get("candidate", ""))
        if candidate:
            by_candidate.setdefault(candidate, []).append(row)
    eligible: list[dict[str, Any]] = []
    expected_configs = len(CONFIGS)
    for spec in CONTROLLER_CANDIDATES:
        candidate = spec["candidate"]
        candidate_rows = by_candidate.get(candidate, [])
        if len(candidate_rows) != expected_configs or not all(row_passes(row) for row in candidate_rows):
            continue
        ending = [_number(row.get("first_ten_ending_cash_usd"), "first_ten_ending_cash_usd") for row in candidate_rows]
        eligible.append(
            {
                "candidate": candidate,
                "gate_id": spec["gate"]["gate_id"],
                "previous_min_cash": min(ending),
                "previous_cash_sum": sum(ending),
                "previous_config_count": len(candidate_rows),
            }
        )
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda row: (-row["previous_min_cash"], -row["previous_cash_sum"], row["candidate"]),
    )[0]


def current_window_passes(rows: Iterable[dict[str, Any]]) -> bool:
    """Check all declared configurations for the controller's chosen candidate."""
    values = list(rows)
    return bool(values) and len(values) == len(CONFIGS) and all(row_passes(row) for row in values)


def _replay_window(frame: pd.DataFrame, names: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in CONTROLLER_CANDIDATES:
        rows.extend(h64.replay(frame, spec["candidate"], spec["gate"], names))
    return rows


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = h64.h57.study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = h55.add_frozen_candidates(features)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    windows = rolling_windows(holdout_runs)

    # The second selection block is the prior window for holdout window 1;
    # thereafter only the immediately preceding holdout window is visible.
    prior_windows: list[tuple[str, list[str]]] = [("selection_block_2", selection_runs[10:20])]
    prior_windows.extend(
        (f"holdout_window_{index}", names)
        for index, (_start, names) in enumerate(windows[:-1], start=1)
    )
    current_windows = [(f"holdout_window_{index}", names) for index, (_start, names) in enumerate(windows, start=1)]

    prior_rows: list[dict[str, Any]] = []
    controller_rows: list[dict[str, Any]] = []
    all_current_rows: list[dict[str, Any]] = []
    for (prior_label, prior_names), (current_label, current_names) in zip(prior_windows, current_windows):
        prior_result = _replay_window(frame, prior_names)
        choice = choose_from_previous(prior_result)
        for row in prior_result:
            prior_rows.append({"window": prior_label, **row})
        current_result = _replay_window(frame, current_names)
        chosen_rows = [row for row in current_result if choice and row["candidate"] == choice["candidate"]]
        passed = current_window_passes(chosen_rows)
        all_current_rows.extend({"window": current_label, **row} for row in chosen_rows)
        controller_rows.append(
            {
                "window": current_label,
                "prior_window": prior_label,
                "chosen_candidate": choice["candidate"] if choice else None,
                "chosen_gate_id": choice["gate_id"] if choice else None,
                "previous_min_cash": choice["previous_min_cash"] if choice else None,
                "previous_cash_sum": choice["previous_cash_sum"] if choice else None,
                "abstained": choice is None,
                "current_config_count": len(chosen_rows),
                "current_gate_passed": passed,
            }
        )

    promotion_passed = bool(controller_rows) and all(
        not row["abstained"] and row["current_gate_passed"] for row in controller_rows
    )
    summary: dict[str, Any] = {
        "format": "old-clean-h148-causal-cross-candidate-regime-controller-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "window_count": len(windows),
        "window_size_runs": WINDOW_SIZE,
        "window_stride_runs": WINDOW_STRIDE,
        "controller_candidates": list(CONTROLLER_CANDIDATES),
        "selection_rule": "choose from the immediately previous window only; require every declared config to pass ten chronological fills; abstain otherwise",
        "protocol": {
            "initial_cash_usd": h55.INITIAL_CASH,
            "minimum_order_shares": h55.MINIMUM_ORDER_SHARES,
            "fee_rate": h55.FEE_RATE,
            "cash_release_delay_seconds": h55.CASH_RELEASE_DELAY_SECONDS,
            "configs": list(CONFIGS),
            "same_ledger_per_window": True,
            "required_fills": REQUIRED_FILLS,
            "no_concatenation": True,
            "no_refit": True,
            "paper_only": True,
        },
        "controller_rows": controller_rows,
        "promotion_gate_passed": promotion_passed,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "evidence_boundary": "old-clean quote-derived rolling paper replay; no authenticated execution",
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(prior_rows).to_csv(output / "prior_window_metrics.csv", index=False)
    pd.DataFrame(all_current_rows).to_csv(output / "controller_current_metrics.csv", index=False)
    (output / "controller_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"promotion_passed": promotion_passed, "controller_rows": controller_rows}, ensure_ascii=False, sort_keys=True))
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

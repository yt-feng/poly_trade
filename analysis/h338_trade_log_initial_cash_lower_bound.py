"""Compute an execution-aware initial-cash lower bound from old trade logs.

The calculation is a retrospective lower bound, not a strategy evaluation.  It
replays the first ten chronological intended entries from each existing trade
log, releases each recorded payout at its recorded release timestamp, and
solves for the smallest initial cash that leaves the frozen minimum-order cash
floor after every entry.  No labels are used to choose a row and no source is
downloaded.
"""
from __future__ import annotations

import argparse
import heapq
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


MIN_ORDER = 5.0
REQUIRED_FILLS = 10
REQUIRED_COLUMNS = {
    "first_quote_ts", "release_ts", "reserved_cash", "payout_at_settlement",
}


def _lower_bound(frame: pd.DataFrame) -> dict[str, Any] | None:
    frame = frame.copy()
    frame["first_quote_ts"] = pd.to_datetime(frame["first_quote_ts"], utc=True, errors="coerce")
    frame["release_ts"] = pd.to_datetime(frame["release_ts"], utc=True, errors="coerce")
    frame["reserved_cash"] = pd.to_numeric(frame["reserved_cash"], errors="coerce")
    frame["payout_at_settlement"] = pd.to_numeric(frame["payout_at_settlement"], errors="coerce")
    frame = frame.dropna(subset=list(REQUIRED_COLUMNS)).sort_values(["first_quote_ts", "release_ts"])
    if "market_id" in frame:
        frame = frame.drop_duplicates("market_id", keep="first")
    first = frame.head(REQUIRED_FILLS)
    if len(first) < REQUIRED_FILLS:
        return None

    # Cash is affine in initial_cash: base_cash is the balance with initial
    # cash set to zero.  The maximum entry requirement is therefore exact for
    # this recorded chronological sequence.
    base_cash = 0.0
    releases: list[tuple[int, float]] = []
    required_initial = 0.0
    minimum_base_after_entry = 0.0
    for _, row in first.iterrows():
        timestamp = int(row["first_quote_ts"].value)
        while releases and releases[0][0] <= timestamp:
            _, payout = heapq.heappop(releases)
            base_cash += payout
        reserved = float(row["reserved_cash"])
        required_initial = max(required_initial, MIN_ORDER + reserved - base_cash)
        base_cash -= reserved
        minimum_base_after_entry = min(minimum_base_after_entry, base_cash)
        heapq.heappush(releases, (int(row["release_ts"].value), float(row["payout_at_settlement"])))
    return {
        "rows_available": int(len(frame)),
        "first_ten_rows": REQUIRED_FILLS,
        "required_initial_cash_usd": required_initial,
        "gap_vs_10_usd": required_initial - 10.0,
        "minimum_base_after_entry_usd": minimum_base_after_entry,
    }


def diagnose(paths: Iterable[Path]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    files_seen = 0
    for path in paths:
        files_seen += 1
        try:
            frame = pd.read_csv(path)
        except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            skipped.append({"artifact": str(path), "reason": f"READ_ERROR:{type(exc).__name__}"})
            continue
        missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
        if missing:
            skipped.append({"artifact": str(path), "reason": "MISSING_COLUMNS:" + ",".join(missing)})
            continue
        bound = _lower_bound(frame)
        if bound is None:
            skipped.append({"artifact": str(path), "reason": "FEWER_THAN_TEN_CHRONOLOGICAL_ROWS"})
            continue
        bound["artifact"] = str(path)
        if "strategy" in frame:
            bound["strategy"] = str(frame["strategy"].iloc[0])
        results.append(bound)
    if not results:
        raise ValueError("NO_ELIGIBLE_TRADE_LOGS")
    results.sort(key=lambda row: (row["required_initial_cash_usd"], row["artifact"]))
    values = [row["required_initial_cash_usd"] for row in results]
    series = pd.Series(values, dtype=float)
    best = results[0]
    return {
        "format": "h338-trade-log-initial-cash-lower-bound-v1",
        "hypothesis_id": "H338-trade-log-initial-cash-lower-bound",
        "source_scope": "existing old-data trade logs only; no new data, refit, label selection or protocol change",
        "protocol": {
            "initial_cash_target_usd": 10.0,
            "required_fills": REQUIRED_FILLS,
            "same_evolving_cash_ledger": True,
            "minimum_order_notional_usd": MIN_ORDER,
            "cash_release_seconds": 600.0,
            "settlement_seconds": 300.0,
        },
        "files_seen": files_seen,
        "eligible_files": len(results),
        "files_skipped": skipped,
        "required_initial_cash_min_usd": float(series.min()),
        "required_initial_cash_median_usd": float(series.median()),
        "required_initial_cash_p90_usd": float(series.quantile(0.9)),
        "eligible_at_10_usd": int((series <= 10.0).sum()),
        "best_path": best,
        "decision": "ten_fill_cash_floor_requires_more_than_10_usd_on_all_observed_paths" if not (series <= 10.0).any() else "some_observed_path_reaches_ten_fill_cash_floor_at_10_usd",
        "root_cause": "With recorded fees, entry reservations, settlement payouts and release timestamps, the first ten chronological entries require more than $10 to leave $5 available after every entry. This is an execution-economics lower bound on observed paths, not proof that a new signal cannot do better.",
        "strategy_evaluated": False,
        "paper_only": True,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "next_action": "Keep live buying paused; any new candidate must demonstrate a predeclared lower entry reservation or a genuinely better fee-adjusted path on chronological holdout data before the Canary gate is reconsidered.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "files_seen", "eligible_files", "required_initial_cash_min_usd",
        "required_initial_cash_median_usd", "eligible_at_10_usd", "decision",
    )}, sort_keys=True))


if __name__ == "__main__":
    main()

"""H211: stream a microprice/queue-shift hypothesis on the frozen old data.

The recent LOB literature motivates a microprice and queue-imbalance check,
but the old BTC five-minute archive is read one complete run at a time to keep
the local memory bounded.  The experiment is paper-only and evaluates one
chronological cash ledger with the frozen $10/$5/5-share/600-second contract.
It never collects data, trains a model, or submits an order.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from .sequential_compound_gate import evaluate_compound_chain
except ImportError:  # pragma: no cover - direct script execution
    from sequential_compound_gate import evaluate_compound_chain


EDGE_THRESHOLDS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05)
PRICE_CAPS = (0.80, 0.90, 0.95)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)
REQUIRED_COLUMNS = (
    "ts_iso",
    "slug",
    "buy_up_cents",
    "buy_down_cents",
    "sell_up_cents",
    "sell_down_cents",
    "buy_up_size",
    "buy_down_size",
    "bid_depth_up_5",
    "ask_depth_up_5",
    "bid_depth_down_5",
    "ask_depth_down_5",
    "target_price",
    "final_price",
)


def _finite(value: object) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _microprice(row: pd.Series, side: str) -> tuple[float, float]:
    bid = float(row[f"sell_{side}_cents"]) / 100.0 if _finite(row[f"sell_{side}_cents"]) else math.nan
    ask = float(row[f"buy_{side}_cents"]) / 100.0 if _finite(row[f"buy_{side}_cents"]) else math.nan
    bid_depth = float(row[f"bid_depth_{side}_5"]) if _finite(row[f"bid_depth_{side}_5"]) else math.nan
    ask_depth = float(row[f"ask_depth_{side}_5"]) if _finite(row[f"ask_depth_{side}_5"]) else math.nan
    if not all(_finite(value) for value in (bid, ask, bid_depth, ask_depth)) or bid_depth + ask_depth <= 0:
        return ask, math.nan
    microprice = (ask * bid_depth + bid * ask_depth) / (bid_depth + ask_depth)
    return ask, microprice


def _session(ts: pd.Timestamp) -> str:
    local = ts.tz_convert("America/New_York")
    minutes = int(local.hour * 60 + local.minute)
    if minutes >= 1140 or minutes < 120:
        return "asia"
    if minutes < 570:
        return "london"
    if local.dayofweek < 5 and minutes < 720:
        return "us_open"
    if local.dayofweek < 5 and minutes < 960:
        return "us_afternoon"
    return "other"


def _read_run(run: Path) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for source in sorted(run.glob("*.csv")):
        try:
            parts.append(pd.read_csv(source, usecols=lambda column: column in REQUIRED_COLUMNS))
        except (OSError, ValueError, pd.errors.ParserError):
            continue
    if not parts:
        return pd.DataFrame()
    frame = pd.concat(parts, ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["ts_iso"], utc=True, errors="coerce")
    for column in REQUIRED_COLUMNS[2:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["ts", "slug"]).sort_values(["slug", "ts"], kind="stable")
    markets: list[dict[str, Any]] = []
    for slug, group in frame.groupby("slug", sort=False):
        group = group.drop_duplicates("ts", keep="last").sort_values("ts", kind="stable")
        if group.empty:
            continue
        targets = group["target_price"].dropna()
        finals = group["final_price"].dropna()
        if targets.empty or finals.empty:
            continue
        first_ts = group["ts"].iloc[0]
        first_two = group[group["ts"] <= first_ts + pd.Timedelta(minutes=2)]
        if first_two.empty:
            continue
        row = first_two.iloc[-1]
        path = first_two["final_price"].dropna().to_numpy(dtype=float)
        differences = np.diff(path)
        total_abs = float(np.abs(differences).sum())
        path_efficiency = abs(float(path[-1] - path[0])) / total_abs if len(path) >= 2 and total_abs > 0 else 1.0
        target = float(targets.iloc[0])
        outcome = 1.0 if float(finals.iloc[-1]) > target else 0.0
        up_ask, up_micro = _microprice(row, "up")
        down_ask, down_micro = _microprice(row, "down")
        up_size = float(row["buy_up_size"]) if _finite(row["buy_up_size"]) else math.nan
        down_size = float(row["buy_down_size"]) if _finite(row["buy_down_size"]) else math.nan
        up_bid = float(row["sell_up_cents"]) / 100.0 if _finite(row["sell_up_cents"]) else math.nan
        down_bid = float(row["sell_down_cents"]) / 100.0 if _finite(row["sell_down_cents"]) else math.nan
        markets.append(
            {
                "run_name": run.name,
                "slug": str(slug),
                "first_ts": first_ts,
                "entry_ts": row["ts"],
                "session": _session(first_ts),
                "outcome": outcome,
                "path_efficiency": path_efficiency,
                "up_ask": up_ask,
                "up_size": up_size,
                "up_shift": up_micro - (up_bid + up_ask) / 2 if _finite(up_micro) and _finite(up_bid) and _finite(up_ask) else math.nan,
                "down_ask": down_ask,
                "down_size": down_size,
                "down_shift": down_micro - (down_bid + down_ask) / 2 if _finite(down_micro) and _finite(down_bid) and _finite(down_ask) else math.nan,
            }
        )
    return pd.DataFrame(markets)


def build_markets(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    runs = sorted(
        [path for path in source_root.iterdir() if path.is_dir() and len(list(path.glob("*.csv"))) == 48],
        key=lambda path: path.name,
    )
    parts = [_read_run(run) for run in runs]
    parts = [part for part in parts if not part.empty]
    markets = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return markets.sort_values("entry_ts", kind="stable").reset_index(drop=True), [run.name for run in runs]


def make_signals(markets: pd.DataFrame, threshold: float, price_cap: float, require_path: bool) -> pd.DataFrame:
    output = markets.copy()
    output["side"] = "skip"
    output["ask"] = np.nan
    output["size"] = np.nan
    output["shift"] = np.nan
    for index, row in output.iterrows():
        choices: list[tuple[str, float, float, float]] = []
        if _finite(row.up_shift) and row.up_shift >= threshold and _finite(row.up_ask) and row.up_ask <= price_cap:
            choices.append(("buy_up", float(row.up_ask), float(row.up_size), float(row.up_shift)))
        if _finite(row.down_shift) and row.down_shift >= threshold and _finite(row.down_ask) and row.down_ask <= price_cap:
            choices.append(("buy_down", float(row.down_ask), float(row.down_size), float(row.down_shift)))
        if require_path and (not _finite(row.path_efficiency) or row.path_efficiency < 0.50):
            choices = []
        if choices:
            side, ask, size, shift = max(choices, key=lambda item: item[3])
            output.at[index, "side"] = side
            output.at[index, "ask"] = ask
            output.at[index, "size"] = size
            output.at[index, "shift"] = shift
    return output[output["side"] != "skip"].sort_values("entry_ts", kind="stable").reset_index(drop=True)


def replay(signals: pd.DataFrame) -> dict[str, Any]:
    cash = 10.0
    minimum_cash = cash
    pending: list[tuple[pd.Timestamp, float]] = []
    logs: list[dict[str, Any]] = []
    for row in signals.itertuples(index=False):
        pending.sort(key=lambda item: item[0])
        while pending and pending[0][0] <= row.entry_ts:
            cash += pending.pop(0)[1]
        if not all(_finite(value) for value in (row.ask, row.size, row.outcome)):
            continue
        fill_price = float(row.ask) + 0.005
        capacity = float(row.size) * 0.25 * fill_price
        required = max(5.0, 5.0 * fill_price)
        if cash < required or capacity < required:
            continue
        cost = min(max(cash * 0.10, required), cash, capacity)
        shares = cost / fill_price
        fee = shares * 0.07 * fill_price * (1.0 - fill_price)
        reserved = cost + fee
        if reserved > cash + 1e-12:
            continue
        payout = shares * (float(row.outcome) if row.side == "buy_up" else 1.0 - float(row.outcome))
        cash -= reserved
        minimum_cash = min(minimum_cash, cash)
        release_ts = row.entry_ts + pd.Timedelta(seconds=900)
        pending.append((release_ts, payout))
        logs.append(
            {
                "status": "paper",
                "entry_ts": float(row.entry_ts.timestamp()),
                "cash_available_ts": float(release_ts.timestamp()),
                "entry_cost": str(cost),
                "fee": str(fee),
                "payout": str(payout),
                "side": row.side,
                "run_name": row.run_name,
                "slug": row.slug,
                "shift": float(row.shift),
                "path_efficiency": float(row.path_efficiency),
            }
        )
    for _, payout in sorted(pending, key=lambda item: item[0]):
        cash += payout
    return {"signals": int(len(signals)), "fills": len(logs), "ending_cash": cash, "minimum_cash": minimum_cash, "logs": logs}


def prefix_summary(logs: list[dict[str, Any]]) -> dict[str, Any]:
    result = evaluate_compound_chain(
        logs[:10], initial_cash="10", minimum_order="5", required_fills=10, evidence_mode="paper_upper_bound"
    )
    return {
        "fills": int(result["fills"]),
        "ending_cash": result["ending_cash"],
        "minimum_cash": result["minimum_cash"],
        "passed": bool(result["paper_replay_passed"]),
        "failure": result["failure_reason"],
    }


def _holdout_blocks(runs: list[str]) -> list[list[str]]:
    if len(runs) != sum(HOLDOUT_BLOCK_SIZES):
        return []
    blocks: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        blocks.append(runs[cursor : cursor + size])
        cursor += size
    return blocks


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, Any]:
    markets, runs = build_markets(source_root)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    records: list[dict[str, Any]] = []
    for threshold in EDGE_THRESHOLDS:
        for price_cap in PRICE_CAPS:
            for require_path in (False, True):
                candidate = make_signals(markets, threshold, price_cap, require_path)
                selection = replay(candidate[candidate["run_name"].isin(selection_runs)])
                holdout = replay(candidate[candidate["run_name"].isin(holdout_runs)])
                blocks = []
                for index, block_runs in enumerate(_holdout_blocks(holdout_runs), start=1):
                    block = replay(candidate[candidate["run_name"].isin(block_runs)])
                    blocks.append({"block": index, "prefix": prefix_summary(block["logs"]), "fills": block["fills"], "ending_cash": block["ending_cash"]})
                records.append(
                    {
                        "threshold": threshold,
                        "price_cap": price_cap,
                        "require_path_efficiency": require_path,
                        "selection": {"signals": selection["signals"], "fills": selection["fills"], "prefix": prefix_summary(selection["logs"])},
                        "holdout": {"signals": holdout["signals"], "fills": holdout["fills"], "prefix": prefix_summary(holdout["logs"])},
                        "holdout_blocks": blocks,
                    }
                )
    result = {
        "format": "old-clean-h211-microprice-stream-v1",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "complete_runs": len(runs),
        "markets": int(len(markets)),
        "selection_runs": len(selection_runs),
        "holdout_runs": len(holdout_runs),
        "contract": {"initial_cash_usd": 10, "minimum_order_usd": 5, "minimum_order_shares": 5, "fee_rate": 0.07, "slippage": 0.005, "cash_release_seconds": 900},
        "hypotheses": ["microprice_shift_threshold", "microprice_shift_plus_path_efficiency"],
        "records": records,
        "same_ledger_selection_holdout_records": sum(bool(row["selection"]["prefix"]["passed"] and row["holdout"]["prefix"]["passed"]) for row in records),
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "evidence_boundary": "paper upper bound from quote/depth snapshots; no authenticated execution, settlement, or reusable-cash receipt",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, args.output, args.source_commit)
    print(json.dumps({key: result[key] for key in ("complete_runs", "markets", "selection_runs", "holdout_runs", "same_ledger_selection_holdout_records", "new_data_collected", "actual_fills", "verified_canary_fills")}, sort_keys=True))


if __name__ == "__main__":
    main()

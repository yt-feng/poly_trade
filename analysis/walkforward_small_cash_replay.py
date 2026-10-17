"""Replay current-main walk-forward trade logs under the $10 cash contract.

The upstream walk-forward registry uses a $100 account and a flat $0.01/share
fee. This diagnostic reconstructs the binary outcome from those logs, then
replays the same chronological test events with the frozen small-account
contract. It uses the observed old replay shares as a conservative depth lower
bound; it never assumes unobserved capacity and never submits orders.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

OLD_FEE = 0.01
SETTLEMENT_SECONDS = 300.0
BLOCK_SIZES = (16, 16, 16, 16, 15)


def fee(shares: float, price: float, rate: float, exponent: float) -> float:
    return round(float(shares) * rate * (float(price) ** exponent) * (1.0 - float(price)), 5)


def prepare_logs(root: Path, test_runs: list[str]) -> pd.DataFrame:
    logs = pd.read_csv(root / "walk_forward_trade_logs.csv")
    selected = pd.read_csv(root / "walk_forward_selected_by_fold.csv")
    run_to_selected = dict(zip(selected["test_run"].astype(str), selected["selected_strategy"].astype(str)))
    logs["first_quote_ts"] = pd.to_datetime(logs["first_quote_ts"], utc=True)
    logs["run_name"] = logs["run_name"].astype(str)
    logs["strategy"] = logs["strategy"].astype(str)
    logs["side"] = logs["side"].astype(str)
    logs["price"] = pd.to_numeric(logs["entry_price"], errors="coerce")
    logs["old_shares"] = pd.to_numeric(logs["target_cost"], errors="coerce") / logs["price"]
    logs["old_pnl"] = pd.to_numeric(logs["pnl_usd"], errors="coerce")
    logs["old_before"] = pd.to_numeric(logs["bankroll_after"], errors="coerce") - logs["old_pnl"]
    logs["fraction"] = pd.to_numeric(logs["fraction"], errors="coerce")
    old_per_share = logs["old_pnl"] / logs["old_shares"]
    logs["outcome"] = np.where(
        logs["side"].eq("buy_up"),
        old_per_share + logs["price"] + OLD_FEE,
        1.0 - (old_per_share + logs["price"] + OLD_FEE),
    )
    # This is intentionally a lower bound: the old event proves only that this
    # number of shares was available, even when the old target was not capped.
    logs["max_shares_inferred"] = logs["old_shares"]
    logs["validation_selected"] = logs.apply(
        lambda row: run_to_selected.get(row["run_name"]) == row["strategy"], axis=1
    )
    mask = (
        logs["split"].eq("test")
        & logs["run_name"].isin(test_runs)
        & logs["first_quote_ts"].notna()
        & logs["price"].between(0.01, 0.98)
        & logs["outcome"].between(-1e-6, 1 + 1e-6)
    )
    out = logs.loc[mask].copy()
    out["outcome"] = out["outcome"].clip(0.0, 1.0)
    return out


def replay(frame: pd.DataFrame, label: str, initial_cash: float, min_cost: float,
           fee_rate: float, fee_exponent: float, slippage: float,
           cash_release_delay: float) -> tuple[dict[str, object], pd.DataFrame]:
    frame = frame.sort_values("first_quote_ts").reset_index(drop=True)
    cash = float(initial_cash)
    locked = 0.0
    positions: deque[dict[str, float]] = deque()
    rows: list[dict[str, object]] = []
    blocked = 0
    min_cash = cash
    max_locked = 0.0
    peak = cash
    max_drawdown = 0.0
    for _, row in frame.iterrows():
        now = row["first_quote_ts"].timestamp()
        while positions and positions[0]["release"] <= now + 1e-9:
            position = positions.popleft()
            cash += position["payout"]
            locked -= position["reserved"]
        price = float(row["price"])
        fill_price = price + slippage
        capacity = max(0.0, float(row["max_shares_inferred"]) * fill_price)
        fraction = float(row["fraction"]) if math.isfinite(float(row["fraction"])) else 0.04
        target = max(0.0, cash * fraction)
        cost = min(target, cash, capacity)
        required = max(min_cost, 5.0 * fill_price)
        if not math.isfinite(cost) or cash + 1e-12 < required or capacity + 1e-12 < required:
            blocked += 1
            continue
        cost = min(max(cost, required), cash, capacity)
        shares = cost / fill_price
        entry_fee = fee(shares, fill_price, fee_rate, fee_exponent)
        reserved = cost + entry_fee
        if reserved > cash + 1e-12:
            blocked += 1
            continue
        outcome = float(row["outcome"])
        payout = shares * (outcome if row["side"] == "buy_up" else 1.0 - outcome)
        pnl = payout - reserved
        cash -= reserved
        locked += reserved
        positions.append({
            "release": now + SETTLEMENT_SECONDS + cash_release_delay,
            "payout": payout,
            "reserved": reserved,
        })
        min_cash = min(min_cash, cash)
        max_locked = max(max_locked, locked)
        peak = max(peak, cash)
        max_drawdown = max(max_drawdown, (peak - cash) / peak if peak else 0.0)
        rows.append({
            "label": label, "run_name": row["run_name"], "first_quote_ts": row["first_quote_ts"],
            "strategy": row["strategy"], "side": row["side"], "entry_price": price,
            "fill_price": fill_price, "cost": cost, "fee": entry_fee, "pnl": pnl,
            "cash_after_entry": cash, "outcome": outcome,
        })
    while positions:
        position = positions.popleft()
        cash += position["payout"]
        locked -= position["reserved"]
    return {
        "label": label, "trades": len(rows), "blocked_events": blocked,
        "ending_cash": cash, "net_pnl": cash - initial_cash,
        "min_cash_after_entry": min_cash, "max_locked_cash": max_locked,
        "max_drawdown": max_drawdown,
    }, pd.DataFrame(rows)


def run(args: argparse.Namespace) -> dict[str, object]:
    root = Path(args.walkforward_dir)
    selected = pd.read_csv(root / "walk_forward_selected_by_fold.csv")
    test_runs = selected["test_run"].astype(str).tolist()
    logs = prepare_logs(root, test_runs)
    frames = {
        "fixed_classic_early_drop_down": logs[logs["strategy"].eq("classic_early_drop_down")],
        "fixed_classic_breakout_up": logs[logs["strategy"].eq("classic_breakout_up")],
        "validation_selected": logs[logs["validation_selected"]],
    }
    summary: list[dict[str, object]] = []
    blocks: list[dict[str, object]] = []
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for label, frame in frames.items():
        metrics, trades = replay(frame, label, args.initial_cash, args.min_cost, args.fee_rate,
                                 args.fee_exponent, args.slippage, args.cash_release_delay)
        summary.append(metrics)
        if not trades.empty:
            trades.to_csv(out / f"{label}_trades.csv", index=False)
        cursor = 0
        for block_index, size in enumerate(BLOCK_SIZES, 1):
            names = test_runs[cursor:cursor + size]
            cursor += size
            block_metrics, _ = replay(frame[frame["run_name"].isin(names)], f"{label}_block{block_index}",
                                      args.initial_cash, args.min_cost, args.fee_rate,
                                      args.fee_exponent, args.slippage, args.cash_release_delay)
            block_metrics.update({"label": label, "block": block_index, "run_count": size,
                                  "first_run": names[0], "last_run": names[-1]})
            blocks.append(block_metrics)
    result = {
        "format": "old-data-walkforward-small-cash-replay-v1",
        "source_walkforward_dir": str(root),
        "test_runs": len(test_runs),
        "protocol": {
            "initial_cash": args.initial_cash, "min_cost": args.min_cost,
            "fee_rate": args.fee_rate, "fee_exponent": args.fee_exponent,
            "slippage": args.slippage, "cash_release_delay_seconds": args.cash_release_delay,
            "settlement_seconds": SETTLEMENT_SECONDS, "old_log_fee_for_outcome_reconstruction": OLD_FEE,
            "capacity_basis": "observed old replay shares are a conservative lower bound",
            "new_data_collected": False, "live_orders_enabled": False,
        },
        "summary": summary, "blocks": blocks,
        "promotion_rule": "all five independent block replays must end at >= $5 with nonnegative PnL; this diagnostic never counts fills",
    }
    out.joinpath("summary.csv").write_text(pd.DataFrame(summary).to_csv(index=False))
    out.joinpath("blocks.csv").write_text(pd.DataFrame(blocks).to_csv(index=False))
    out.joinpath("meta.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--walkforward-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--initial-cash", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--fee-exponent", type=float, default=1.0)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--cash-release-delay", type=float, default=600.0)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False))


if __name__ == "__main__":
    main()

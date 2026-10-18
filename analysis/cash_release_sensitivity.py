"""Old-data cash-lock sensitivity for the frozen early-drop gates.

The normal replay immediately adds realized PnL to bankroll.  This diagnostic
keeps entry cost and fee locked until a five-minute market settles plus an
explicit cash-release delay.  It therefore measures whether a small account
can reuse an order subject to both a notional floor and the exchange's
minimum share count without assuming instant settlement.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

import v1_execution_walk_forward as study
from causal_robust_gate_sweep import attach_robust_gate
from classic_min_order_chain import prepare


GATES = ("mean_positive", "lower_quartile_positive")
SETTLEMENT_SECONDS = 300.0


def simulate_cash_locked(
    df: pd.DataFrame,
    strategy: str,
    config: study.ExecutionConfig,
    bankroll0: float,
    min_cost: float,
    cash_release_delay_seconds: float,
    cooldown_events: int = study.COOLDOWN_EVENTS,
    capture_logs: bool = True,
    fee_mode: str = "polymarket_taker",
    fee_rate: float = 0.07,
    fee_exponent: float = 1.0,
    min_order_shares: float = 5.0,
    minimum_free_cash_after_entry: float = 0.0,
) -> tuple[pd.DataFrame, dict]:
    if not math.isfinite(float(min_order_shares)) or float(min_order_shares) < 0:
        raise ValueError("min_order_shares must be finite and non-negative")
    if not math.isfinite(float(minimum_free_cash_after_entry)) or float(minimum_free_cash_after_entry) < 0:
        raise ValueError("minimum_free_cash_after_entry must be finite and non-negative")
    d = df.sort_values("first_quote_ts").reset_index(drop=True)
    side = d[f"{strategy}_side"].fillna("skip").astype(str).to_numpy()
    minute = d[f"{strategy}_minute"].to_numpy(dtype=int)
    fraction = d[f"{strategy}_fraction"].to_numpy(dtype=float)
    signal = side != "skip"
    quote_price = np.full(len(d), np.nan)
    signal_price = np.full(len(d), np.nan)
    quote_size = np.full(len(d), np.nan)
    quote_spread = np.full(len(d), np.nan)
    quote_overround = np.full(len(d), np.nan)
    adverse = np.full(len(d), np.nan)
    for side_name in ("buy_up", "buy_down"):
        side_mask = signal & (side == side_name)
        for entry_minute in (1, 2, 4):
            rows = side_mask & (minute == entry_minute)
            if not rows.any():
                continue
            prefix = f"exec_{side_name}_{entry_minute}m_d{config.delay_seconds}"
            quote_price[rows] = pd.to_numeric(d.loc[rows, f"{prefix}_quote_price"], errors="coerce").to_numpy()
            signal_price[rows] = pd.to_numeric(d.loc[rows, f"{prefix}_signal_price"], errors="coerce").to_numpy()
            quote_size[rows] = pd.to_numeric(d.loc[rows, f"{prefix}_quote_size"], errors="coerce").to_numpy()
            quote_spread[rows] = pd.to_numeric(d.loc[rows, f"{prefix}_quote_spread"], errors="coerce").to_numpy()
            quote_overround[rows] = pd.to_numeric(d.loc[rows, f"{prefix}_quote_overround"], errors="coerce").to_numpy()
            adverse[rows] = pd.to_numeric(d.loc[rows, f"{prefix}_adverse_slippage"], errors="coerce").to_numpy()
    fill_price = quote_price + config.fixed_slippage
    outcome = pd.to_numeric(d["outcome_up"], errors="coerce").to_numpy(dtype=float)
    eligible = (
        signal & np.isfinite(signal_price) & np.isfinite(quote_price) & np.isfinite(fill_price)
        & np.isfinite(quote_size) & np.isfinite(quote_spread) & np.isfinite(quote_overround)
        & np.isfinite(adverse) & np.isfinite(outcome) & (quote_price >= 0.05)
        & (quote_price <= 0.95) & (fill_price < 0.98) & (quote_size >= config.min_depth)
        & (quote_spread <= config.max_spread) & (quote_overround <= config.max_overround)
        & (adverse <= config.max_adverse_slippage)
    )
    cash = float(bankroll0)
    locked = 0.0
    peak_cash = cash
    min_cash = cash
    max_locked = 0.0
    max_cash_drawdown = 0.0
    recent_trades: list[int] = []
    loss_streak = 0
    cooldown = 0
    positions: deque[dict] = deque()
    realized: list[float] = []
    logs: list[dict] = []
    blocked_cash = 0
    blocked_pending = 0
    last_now = None

    def release_until(now: float) -> None:
        nonlocal cash, locked, peak_cash, max_cash_drawdown
        while positions and positions[0]["release_ts"] <= now + 1e-9:
            pos = positions.popleft()
            cash += pos["payout"]
            locked -= pos["reserved"]
            realized.append(pos["pnl"])
            peak_cash = max(peak_cash, cash)
            if peak_cash > 0:
                max_cash_drawdown = max(max_cash_drawdown, (peak_cash - cash) / peak_cash)

    for i in np.flatnonzero(signal):
        now = pd.Timestamp(d.at[i, "first_quote_ts"]).timestamp()
        last_now = now
        release_until(now)
        recent_trades = [index for index in recent_trades if index > i - study.QUOTA_WINDOW_EVENTS]
        if cooldown > 0:
            cooldown -= 1
            blocked_pending += 1
            continue
        if len(recent_trades) >= study.MAX_TRADES_PER_WINDOW or not eligible[i]:
            continue
        capacity = float(quote_size[i]) * study.FILL_RATIO * float(fill_price[i])
        target_cost = cash * max(fraction[i], 0.0)
        cost = min(target_cost, cash, capacity)
        required_cost = max(float(min_cost), float(min_order_shares) * float(fill_price[i]))
        if cash < required_cost or capacity < required_cost:
            blocked_cash += 1
            continue
        cost = min(max(cost, required_cost), cash, capacity)
        if not math.isfinite(cost) or cost < float(min_cost) or fill_price[i] <= 0:
            continue
        shares = cost / fill_price[i]
        fee = study.trade_fee_usd(shares, fill_price[i], fee_mode, fee_rate, fee_exponent)
        reserved = cost + fee
        if reserved > cash + 1e-12:
            blocked_cash += 1
            continue
        if cash - reserved < float(minimum_free_cash_after_entry) - 1e-12:
            blocked_cash += 1
            continue
        payout = shares * (outcome[i] if side[i] == "buy_up" else 1.0 - outcome[i])
        pnl = payout - reserved
        cash -= reserved
        locked += reserved
        release_ts = now + SETTLEMENT_SECONDS + float(cash_release_delay_seconds)
        positions.append({"release_ts": release_ts, "payout": payout, "reserved": reserved, "pnl": pnl})
        max_locked = max(max_locked, locked)
        min_cash = min(min_cash, cash)
        peak_cash = max(peak_cash, cash)
        if peak_cash > 0:
            max_cash_drawdown = max(max_cash_drawdown, (peak_cash - cash) / peak_cash)
        recent_trades.append(i)
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= study.COOLDOWN_AFTER_LOSSES:
                cooldown = int(cooldown_events)
                loss_streak = 0
        else:
            loss_streak = 0
        if capture_logs:
            logs.append({
                "strategy": strategy,
                "first_quote_ts": d.at[i, "first_quote_ts"],
                "run_name": d.at[i, "run_name"],
                "market_id": d.at[i, "market_id"],
                "side": side[i],
                "entry_minute": int(minute[i]),
                "filled_cost": cost,
                "fee_usd": fee,
                "reserved_cash": reserved,
                "payout_at_settlement": payout,
                "pnl_usd": pnl,
                "release_ts": pd.Timestamp(release_ts, unit="s", tz="UTC"),
                "cash_after_entry": cash,
            })
    observation_end = float(last_now or 0.0)
    # Report a fully settled account after the observed window, while retaining
    # the number of positions that were still locked at the last decision.
    locked_at_observation_end = len(positions)
    release_until(observation_end + SETTLEMENT_SECONDS + float(cash_release_delay_seconds) + 1.0)
    wins = sum(x for x in realized if x > 0)
    losses = sum(x for x in realized if x < 0)
    return pd.DataFrame(logs), {
        "strategy": strategy,
        "cash_release_delay_seconds": float(cash_release_delay_seconds),
        "cooldown_events": int(cooldown_events),
        "fee_mode": fee_mode,
        "fee_rate": float(fee_rate),
        "fee_exponent": float(fee_exponent),
        "min_order_notional": float(min_cost),
        "min_order_shares": float(min_order_shares),
        "minimum_free_cash_after_entry": float(minimum_free_cash_after_entry),
        "trades": len(realized),
        "ending_cash": float(cash),
        "net_return": float(cash / bankroll0 - 1.0),
        "min_cash_before_settlement": float(min_cash),
        "max_locked_cash": float(max_locked),
        "max_cash_drawdown": float(max_cash_drawdown),
        "locked_positions_at_observation_end": int(locked_at_observation_end),
        "blocked_cash_events": int(blocked_cash),
        "blocked_pending_events": int(blocked_pending),
        "profit_factor": float(wins / abs(losses)) if losses else float("nan"),
        "win_rate": float(sum(x > 0 for x in realized) / len(realized)) if realized else float("nan"),
        "settlement_seconds": SETTLEMENT_SECONDS,
        "cash_release_model": "entry cost and fee locked until five-minute settlement plus delay",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--delays", default="0,5,30,300,600")
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args()
    delays = [float(x.strip()) for x in args.delays.split(",") if x.strip()]
    base, runs = prepare(Path(args.source_root), ["classic_early_drop_down"])
    test_runs = runs[4:]
    test = base[base["run_name"].isin(test_runs)].copy()
    rows = []
    block_rows = []
    for gate in GATES:
        features, diagnostic = attach_robust_gate(base, gate, min_history=20, lookback=20)
        strategy = f"classic_early_drop_down_{gate}"
        for delay in delays:
            logs, metrics = simulate_cash_locked(
                features[features["run_name"].isin(test_runs)],
                strategy,
                study.ExecutionConfig(fixed_slippage=args.slippage),
                args.bankroll0,
                args.min_cost,
                delay,
            )
            rows.append({"gate": gate, "slippage": args.slippage, **diagnostic, **metrics})
            if not logs.empty:
                logs = logs.copy()
                order = {name: i for i, name in enumerate(test_runs)}
                logs["test_index"] = logs["run_name"].map(order)
                for block, lo, hi in [("first20", 0, 20), ("holdout", 20, len(test_runs))]:
                    part = logs[logs["test_index"].between(lo, hi - 1)]
                    block_rows.append({
                        "gate": gate,
                        "delay_seconds": delay,
                        "block": block,
                        "trades": int(len(part)),
                        "sum_pnl": float(part["pnl_usd"].sum()) if not part.empty else 0.0,
                        "max_locked_cash": float(part["reserved_cash"].cumsum().max()) if not part.empty else 0.0,
                    })
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    pd.DataFrame(block_rows).to_csv(out.with_name(out.stem + "_blocks.csv"), index=False)
    meta = {
        "complete_runs": len(runs), "test_runs": len(test_runs), "gates": list(GATES),
        "delays_seconds": delays, "settlement_seconds": SETTLEMENT_SECONDS,
        "slippage": args.slippage, "bankroll0": args.bankroll0, "min_cost": args.min_cost,
        "old_clean_dataset_only": True, "new_data_collected": False,
        "live_orders_enabled": False, "causal_history_only": True,
        "interpretation": "cash reuse diagnostic; not canary evidence",
    }
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    print(pd.DataFrame(rows).to_string(index=False))
    print(pd.DataFrame(block_rows).to_string(index=False))


if __name__ == "__main__":
    main()

"""Upper-bound test for same-market complete-set arbitrage on old quotes.

This is deliberately a quote-feasibility study.  It never submits orders and
does not count a pair as a canary: snapshots cannot prove simultaneous fills,
queue position, settlement or cash release.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


COLS = [
    "ts_iso", "slug", "buy_up_cents", "buy_down_cents",
    "buy_up_size", "buy_down_size",
]


def read_quotes(source_root: Path) -> pd.DataFrame:
    frames = []
    for path in sorted(source_root.glob("*/*btc-updown-5m_quotes.csv")):
        frame = pd.read_csv(path, usecols=lambda col: col in COLS)
        frame["source_file"] = path.name
        frame["run_name"] = path.parent.name
        frames.append(frame)
    if not frames:
        raise FileNotFoundError(f"no quote files under {source_root}")
    out = pd.concat(frames, ignore_index=True)
    out["ts"] = pd.to_datetime(out["ts_iso"], errors="coerce", utc=True)
    for col in COLS[2:]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["slug", "ts"]).sort_values(["slug", "ts", "source_file"])
    return out.drop_duplicates(["slug", "ts_iso"], keep="last").reset_index(drop=True)


def parse_close(slug: str) -> pd.Timestamp:
    try:
        return pd.to_datetime(int(str(slug).rsplit("-", 1)[1]) + 300, unit="s", utc=True)
    except (ValueError, TypeError, IndexError):
        return pd.NaT


def fee(shares: float, price: float, rate: float) -> float:
    return shares * rate * price * (1.0 - price)


def first_pair_for_market(
    frame: pd.DataFrame,
    delay_seconds: int,
    minimum_order_usdc: float,
    fee_rate: float,
    slippage: float,
) -> dict | None:
    frame = frame.sort_values("ts").reset_index(drop=True)
    ts = frame["ts"].astype("int64").to_numpy()
    close = parse_close(str(frame.at[0, "slug"]))
    close_ns = close.value if pd.notna(close) else np.iinfo(np.int64).max
    for i, signal_ts in enumerate(ts):
        # The legacy collector names the taker-facing asks buy_*; sell_* are
        # bids.  Keep this mapping explicit because reversing it manufactures
        # an apparent complete-set arbitrage in almost every market.
        p_up = frame.at[i, "buy_up_cents"] / 100.0
        p_down = frame.at[i, "buy_down_cents"] / 100.0
        if not (0 < p_up < 1 and 0 < p_down < 1 and p_up + p_down < 1):
            continue
        j = int(np.searchsorted(ts, signal_ts + delay_seconds * 1_000_000_000, side="left"))
        if j >= len(frame) or ts[j] >= close_ns:
            continue
        fill_up = frame.at[j, "buy_up_cents"] / 100.0 + slippage
        fill_down = frame.at[j, "buy_down_cents"] / 100.0 + slippage
        size_up = frame.at[j, "buy_up_size"]
        size_down = frame.at[j, "buy_down_size"]
        if not (0 < fill_up < 1 and 0 < fill_down < 1 and np.isfinite(size_up) and np.isfinite(size_down)):
            continue
        shares = max(minimum_order_usdc / fill_up, minimum_order_usdc / fill_down)
        if min(size_up, size_down) + 1e-12 < shares:
            continue
        cost = shares * (fill_up + fill_down)
        fees = fee(shares, fill_up, fee_rate) + fee(shares, fill_down, fee_rate)
        pnl = shares - cost - fees
        if pnl <= 0:
            continue
        return {
            "slug": str(frame.at[0, "slug"]),
            "run_name": str(frame.at[0, "run_name"]),
            "signal_ts": pd.to_datetime(signal_ts, unit="ns", utc=True).isoformat(),
            "fill_ts": pd.to_datetime(ts[j], unit="ns", utc=True).isoformat(),
            "up_price": fill_up,
            "down_price": fill_down,
            "up_size": float(size_up),
            "down_size": float(size_down),
            "shares": shares,
            "cost": cost,
            "fees": fees,
            "pnl": pnl,
            "return_on_cost": pnl / (cost + fees),
        }
    return None


def run(quotes: pd.DataFrame, bankroll0: float, **kwargs) -> tuple[pd.DataFrame, dict]:
    candidates = []
    for slug, frame in quotes.groupby("slug", sort=False):
        item = first_pair_for_market(frame, **kwargs)
        if item:
            candidates.append(item)
    table = pd.DataFrame(candidates).sort_values("fill_ts").reset_index(drop=True) if candidates else pd.DataFrame()
    bankroll = bankroll0
    trades = 0
    for row in table.to_dict("records"):
        if row["cost"] + row["fees"] > bankroll + 1e-12:
            continue
        bankroll += row["pnl"]
        trades += 1
    return table, {
        "markets": int(quotes["slug"].nunique()),
        "quote_rows": int(len(quotes)),
        "candidate_markets": int(len(table)),
        "starting_bankroll": bankroll0,
        "ending_bankroll_upper_bound": bankroll,
        "continuous_trades_upper_bound": trades,
        "sum_pnl_upper_bound": float(table["pnl"].sum()) if not table.empty else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bankroll0", type=float, default=10.0)
    parser.add_argument("--minimum-order-usdc", type=float, default=5.0)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--delay-seconds", type=int, default=4)
    parser.add_argument("--slippage", type=float, default=0.0025)
    args = parser.parse_args()
    quotes = read_quotes(args.source_root)
    candidates, summary = run(
        quotes,
        args.bankroll0,
        delay_seconds=args.delay_seconds,
        minimum_order_usdc=args.minimum_order_usdc,
        fee_rate=args.fee_rate,
        slippage=args.slippage,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(args.output / "candidates.csv", index=False)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()

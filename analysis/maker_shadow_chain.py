"""Shadow-test resting maker bids on old quote snapshots; no real orders."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1
import monthly_runs_quant_research_framework as qrf
import v1_execution_walk_forward as study

CANDIDATES = ("classic_breakout_up", "micro_book_consensus_up")
FILL_RATIO = study.FILL_RATIO


def prepare(source_root: Path):
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"] == maximum, "run_name"].astype(str).tolist()
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    features["pred_prob_up_logit"] = qrf.rolling_logit_safe(features)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = features[pd.notna(features["outcome_up"])].sort_values("first_quote_ts").reset_index(drop=True)
    for strategy in CANDIDATES:
        choices = features.apply(lambda row, name=strategy: qrf.choose_trade(row, name), axis=1)
        features[f"{strategy}_side"] = choices.map(lambda x: x[0])
        features[f"{strategy}_minute"] = choices.map(lambda x: x[1]).astype(int)
        features[f"{strategy}_fraction"] = choices.map(lambda x: x[2]).fillna(0.0).astype(float)
    features = attach_maker_snapshots(features, quotes, delays=(4,), minutes=(1, 2, 4))
    return features, runs


def attach_maker_snapshots(
    features: pd.DataFrame,
    quotes: pd.DataFrame,
    delays,
    minutes,
    *,
    crossing_cents: float = 1.0,
) -> pd.DataFrame:
    """Attach passive-bid snapshots and a parameterized trade-through proxy.

    ``crossing_cents`` is a sensitivity parameter for an offline upper bound,
    not evidence that a resting order was filled.  The default preserves the
    original one-cent proxy used by the H345 audit.
    """
    if float(crossing_cents) < 0:
        raise ValueError("crossing_cents must be non-negative")
    delay_values = tuple(delays)
    if any(float(delay) < 0 for delay in delay_values):
        raise ValueError("delays must be non-negative")
    records = []
    for market_id, group in quotes.groupby("market_id", dropna=False):
        g = group.sort_values("ts_utc").reset_index(drop=True)
        if g.empty:
            continue
        ts = pd.to_datetime(g["ts_utc"], utc=True, errors="coerce")
        # Keep the original timestamp ordering and inclusion semantics, but
        # turn the repeated ``ts >= fill_cut`` DataFrame filters below into
        # bounded search slices.  H373 evaluates hundreds of cells over the
        # same markets; rescanning every suffix for every side and delay made
        # the frozen replay unnecessarily quadratic in snapshot count.
        ts_index = pd.DatetimeIndex(ts)
        token_ask_cents = {
            token: pd.to_numeric(g[f"buy_{token}_cents"], errors="coerce").to_numpy(dtype=float)
            for token in ("up", "down")
        }
        first_ts = ts.iloc[0]
        close_ts = g["close_ts_utc"].dropna().iloc[0] if g["close_ts_utc"].notna().any() else pd.NaT
        rec = {"market_id": market_id}
        for minute in minutes:
            signal_cut = first_ts + pd.Timedelta(minutes=minute)
            signal_pos = int(ts.searchsorted(signal_cut, side="right") - 1)
            signal_row = None if signal_pos < 0 else g.iloc[signal_pos]
            signal_ts = pd.NaT if signal_row is None else signal_row["ts_utc"]
            for delay in delay_values:
                delay_label = str(int(delay)) if float(delay).is_integer() else str(delay).replace(".", "p")
                for side in ("buy_up", "buy_down"):
                    token = "up" if side == "buy_up" else "down"
                    prefix = f"maker_{side}_{minute}m_d{delay_label}"
                    if signal_row is None:
                        rec.update({f"{prefix}_{k}": np.nan for k in ("price", "size", "spread", "overround", "touch")})
                        continue
                    maker_price = pd.to_numeric(signal_row.get(f"sell_{token}_cents"), errors="coerce") / 100.0
                    maker_size = pd.to_numeric(signal_row.get(f"sell_{token}_size"), errors="coerce")
                    spread = pd.to_numeric(signal_row.get(f"spread_{token}_cents"), errors="coerce") / 100.0
                    overround = pd.to_numeric(signal_row.get("mid_overround_cents"), errors="coerce") / 100.0
                    touch = 0
                    if pd.notna(signal_ts) and pd.notna(maker_price):
                        fill_cut = pd.Timestamp(signal_ts) + pd.Timedelta(seconds=float(delay))
                        start_pos = int(ts_index.searchsorted(fill_cut, side="left"))
                        end_pos = len(g)
                        if pd.notna(close_ts):
                            end_pos = int(ts_index.searchsorted(close_ts, side="right"))
                        after_asks = token_ask_cents[token][start_pos:end_pos]
                        # A resting bid can only be treated as a trade-through
                        # proxy when a later observed ask crosses it by the
                        # declared amount. A later bid moving down is not
                        # evidence that our bid was filled.
                        maker_cents = pd.to_numeric(signal_row.get(f"sell_{token}_cents"), errors="coerce")
                        valid_asks = np.isfinite(after_asks)
                        if valid_asks.any() and (after_asks[valid_asks] <= maker_cents - float(crossing_cents) + 1e-12).any():
                            touch = 1
                    rec.update({
                        f"{prefix}_price": maker_price,
                        f"{prefix}_size": maker_size,
                        f"{prefix}_spread": spread,
                        f"{prefix}_overround": overround,
                        f"{prefix}_touch": touch,
                    })
        records.append(rec)
    return features.merge(pd.DataFrame(records), on="market_id", how="left")


def simulate_maker(df: pd.DataFrame, strategy: str, bankroll0: float, min_cost: float, min_depth: float = 100.0):
    d = df.sort_values("first_quote_ts").reset_index(drop=True)
    side = d[f"{strategy}_side"].fillna("skip").astype(str).to_numpy()
    minute = d[f"{strategy}_minute"].to_numpy(dtype=int)
    fraction = d[f"{strategy}_fraction"].to_numpy(dtype=float)
    signal = side != "skip"
    price = np.full(len(d), np.nan); size = np.full(len(d), np.nan); spread = np.full(len(d), np.nan); overround = np.full(len(d), np.nan); touch = np.zeros(len(d), dtype=bool)
    for side_name in ("buy_up", "buy_down"):
        for entry_minute in (1, 2, 4):
            mask = signal & (side == side_name) & (minute == entry_minute)
            prefix = f"maker_{side_name}_{entry_minute}m_d4"
            price[mask] = pd.to_numeric(d.loc[mask, f"{prefix}_price"], errors="coerce").to_numpy()
            size[mask] = pd.to_numeric(d.loc[mask, f"{prefix}_size"], errors="coerce").to_numpy()
            spread[mask] = pd.to_numeric(d.loc[mask, f"{prefix}_spread"], errors="coerce").to_numpy()
            overround[mask] = pd.to_numeric(d.loc[mask, f"{prefix}_overround"], errors="coerce").to_numpy()
            touch[mask] = pd.to_numeric(d.loc[mask, f"{prefix}_touch"], errors="coerce").fillna(0).to_numpy(dtype=bool)
    eligible = (signal & touch & np.isfinite(price) & np.isfinite(size) & np.isfinite(spread) & np.isfinite(overround) & (price >= 0.05) & (price <= 0.95) & (size >= min_depth) & (spread <= 0.04) & (overround <= 0.03))
    bankroll = float(bankroll0); peak = bankroll; max_dd = 0.0; trades = 0; wins = 0; logs=[]; recent=[]; cooldown=0
    outcome = pd.to_numeric(d["outcome_up"], errors="coerce").to_numpy(dtype=float)
    for i in np.flatnonzero(signal):
        recent = [x for x in recent if x > i - study.QUOTA_WINDOW_EVENTS]
        if cooldown > 0:
            cooldown -= 1; continue
        if len(recent) >= study.MAX_TRADES_PER_WINDOW or not eligible[i] or bankroll < min_cost:
            continue
        capacity = size[i] * FILL_RATIO * price[i]
        cost = min(max(bankroll * max(fraction[i], 0.0), min_cost), bankroll, capacity)
        if not np.isfinite(cost) or cost < min_cost or cost <= 0:
            continue
        shares = cost / price[i]
        pnl = shares * ((outcome[i] if side[i] == "buy_up" else 1.0 - outcome[i]) - price[i])
        before = bankroll; bankroll += pnl; trades += 1; recent.append(i)
        peak = max(peak, bankroll); max_dd = max(max_dd, (peak - bankroll) / peak if peak else 0.0)
        if pnl > 0: wins += 1
        else:
            cooldown = study.COOLDOWN_EVENTS if cooldown == 0 else cooldown
        logs.append({"first_quote_ts": d.at[i, "first_quote_ts"], "run_name": d.at[i, "run_name"], "market_id": d.at[i, "market_id"], "side": side[i], "maker_price": price[i], "filled_cost": cost, "pnl_usd": pnl, "bankroll_after": bankroll})
    return pd.DataFrame(logs), {"ending_bankroll": bankroll, "total_return": bankroll / bankroll0 - 1.0, "trades": trades, "max_drawdown": max_dd, "win_rate": wins / trades if trades else np.nan, "signals": int(signal.sum()), "eligible_touches": int(eligible.sum())}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--source-root", required=True); parser.add_argument("--output", required=True); parser.add_argument("--bankroll0", type=float, default=10.0); parser.add_argument("--min-cost", type=float, default=5.0)
    args = parser.parse_args(); features, runs = prepare(Path(args.source_root)); folds = study.make_run_folds(runs); rows=[]
    for strategy in CANDIDATES:
        bankroll = args.bankroll0
        for fold in folds:
            frame = features[features["run_name"] == fold["test_run"]]
            _, metrics = simulate_maker(frame, strategy, bankroll, args.min_cost)
            rows.append({"strategy": strategy, "fold_id": fold["fold_id"], "test_run": fold["test_run"], "starting_bankroll": bankroll, "ending_bankroll": metrics["ending_bankroll"], "total_return": metrics["total_return"], "trades": metrics["trades"], "eligible_touches": metrics["eligible_touches"], "max_drawdown": metrics["max_drawdown"], "win_rate": metrics["win_rate"]})
            bankroll = metrics["ending_bankroll"]
    result=pd.DataFrame(rows); out=Path(args.output); out.parent.mkdir(parents=True, exist_ok=True); result.to_csv(out,index=False)
    summary=result.groupby("strategy",as_index=False).agg(ending_bankroll=("ending_bankroll","last"),total_trades=("trades","sum"),eligible_touches=("eligible_touches","sum"),positive_folds=("total_return",lambda s:int((s>0).sum())),zero_trade_folds=("trades",lambda s:int((s==0).sum())),max_fold_drawdown=("max_drawdown","max"))
    sp=out.with_name(out.stem+"_summary.csv"); summary.to_csv(sp,index=False); print(json.dumps({"complete_runs":len(runs),"fold_count":len(folds),"bankroll0":args.bankroll0,"min_cost":args.min_cost,"fill_interpretation":"quote touch proxy, not actual fill"},indent=2)); print(summary.to_string(index=False))


if __name__ == "__main__": main()

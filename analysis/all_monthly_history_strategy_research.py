from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

FILE_GLOB = "*btc-updown-5m_quotes.csv"
ENTRY_MINUTES = [1, 2, 4]
FIXED_FRACS = [0.10, 0.15, 0.20, 0.25]

STATIC_STRATEGIES = [
    "hist_m1_drop20_down",
    "hist_m2_drop10_down",
    "hist_m2_sharpdrop_reversal_up",
    "hist_m2_milddrop_down",
    "hist_m2_extremeup_fade_down",
    "hist_m4_rise30to50_up",
    "hist_switch_v1",
    "hist_switch_v2",
    "hist_switch_conservative",
    "hist_fairprob_switch",
    "hist_fairprob_conservative",
    "hist_fairprob_robust",
]


def first_non_null(series: pd.Series):
    s = series.dropna()
    return np.nan if s.empty else s.iloc[0]


def last_non_null(series: pd.Series):
    s = series.dropna()
    return np.nan if s.empty else s.iloc[-1]


def parse_close_ts_from_slug(slug: str) -> pd.Timestamp:
    m = re.search(r"(\d{10})$", str(slug))
    if not m:
        return pd.NaT
    return pd.to_datetime(int(m.group(1)), unit="s", utc=True)


def scan_run_dirs(source_root: Path) -> List[Path]:
    return sorted([p for p in source_root.iterdir() if p.is_dir()]) if source_root.exists() else []


def read_all_monthly_runs(source_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    run_dirs = scan_run_dirs(source_root)
    frames = []
    cover_rows = []
    for run_dir in run_dirs:
        files = sorted(run_dir.glob(FILE_GLOB))
        if not files:
            continue
        row_count = 0
        for p in files:
            df = pd.read_csv(p)
            df["source_file"] = p.name
            df["run_name"] = run_dir.name
            frames.append(df)
            row_count += len(df)
        cover_rows.append({"run_name": run_dir.name, "file_count": len(files), "raw_row_count": row_count})
    if not frames:
        raise FileNotFoundError(f"No files matching {FILE_GLOB} under any subdirectory of {source_root}")
    raw = pd.concat(frames, ignore_index=True)
    raw.columns = [str(c).strip() for c in raw.columns]
    return raw, pd.DataFrame(cover_rows)


def prepare_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    q = raw.copy()
    q["ts_utc"] = pd.to_datetime(q["ts_iso"], utc=True, errors="coerce")
    for c in [
        "buy_up_cents","buy_down_cents","sell_up_cents","sell_down_cents",
        "buy_up_size","buy_down_size","sell_up_size","sell_down_size",
        "target_price","final_price"
    ]:
        if c in q.columns:
            q[c] = pd.to_numeric(q[c], errors="coerce")
    q["close_ts_utc"] = q["slug"].map(parse_close_ts_from_slug)
    q["market_id"] = q["run_name"].astype(str) + "::" + q["slug"].astype(str)
    q = q.sort_values(["run_name","slug","ts_utc","source_file"]).drop_duplicates(subset=["run_name","ts_iso","slug"], keep="last")
    return q.reset_index(drop=True)


def build_snapshot(quotes: pd.DataFrame, minute: int) -> pd.DataFrame:
    rows: List[Dict[str, float]] = []
    for market_id, g in quotes.groupby("market_id", dropna=False):
        g = g.sort_values("ts_utc").copy()
        if g.empty:
            continue
        first_ts = g["ts_utc"].min()
        snap = g[g["ts_utc"] <= first_ts + pd.Timedelta(minutes=minute)].copy()
        if snap.empty:
            continue
        row = snap.iloc[-1]
        target = first_non_null(g["target_price"])
        final = last_non_null(g["final_price"])
        if pd.isna(target) or pd.isna(final):
            continue
        move = np.nan if pd.isna(row.get("final_price")) else float(row["final_price"] - target)
        buy_up = pd.to_numeric(row.get("buy_up_cents"), errors="coerce")
        buy_down = pd.to_numeric(row.get("buy_down_cents"), errors="coerce")
        sell_up = pd.to_numeric(row.get("sell_up_cents"), errors="coerce")
        sell_down = pd.to_numeric(row.get("sell_down_cents"), errors="coerce")
        up_price = np.nan if pd.isna(buy_up) else float(buy_up) / 100.0
        down_price = np.nan if pd.isna(buy_down) else float(buy_down) / 100.0
        spread_up = np.nan if pd.isna(buy_up) or pd.isna(sell_up) else float((sell_up - buy_up) / 100.0)
        spread_down = np.nan if pd.isna(buy_down) or pd.isna(sell_down) else float((sell_down - buy_down) / 100.0)
        buy_up_size = pd.to_numeric(row.get("buy_up_size"), errors="coerce")
        buy_down_size = pd.to_numeric(row.get("buy_down_size"), errors="coerce")
        size_imb = np.nan
        if pd.notna(buy_up_size) and pd.notna(buy_down_size) and (buy_up_size + buy_down_size) > 0:
            size_imb = float((buy_up_size - buy_down_size) / (buy_up_size + buy_down_size))
        rows.append({
            "market_id": market_id,
            "run_name": row["run_name"],
            "slug": row["slug"],
            "first_quote_ts": first_ts,
            "outcome_up": float(final > target),
            f"move_m{minute}": move,
            f"buy_up_price_m{minute}": up_price,
            f"buy_down_price_m{minute}": down_price,
            f"spread_up_m{minute}": spread_up,
            f"spread_down_m{minute}": spread_down,
            f"buy_up_size_m{minute}": buy_up_size,
            f"buy_down_size_m{minute}": buy_down_size,
            f"size_imb_m{minute}": size_imb,
        })
    return pd.DataFrame(rows)


def build_wide(quotes: pd.DataFrame) -> pd.DataFrame:
    out = None
    for m in ENTRY_MINUTES:
        snap = build_snapshot(quotes, m)
        keys = ["market_id", "run_name", "slug", "first_quote_ts", "outcome_up"]
        out = snap if out is None else out.merge(snap, on=keys, how="outer")
    return out.sort_values("first_quote_ts").reset_index(drop=True)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["move_bucket_m1"] = pd.cut(out["move_m1"], bins=[-1e9,-50,-20,-10,10,1e9], labels=["<=-50","-50~-20","-20~-10","-10~10",">=10"], include_lowest=True)
    out["move_bucket_m2"] = pd.cut(out["move_m2"], bins=[-1e9,-50,-30,-10,10,50,1e9], labels=["<=-50","-50~-30","-30~-10","-10~10","10~50",">=50"], include_lowest=True)
    out["move_bucket_m4"] = pd.cut(out["move_m4"], bins=[-1e9,-10,10,30,50,1e9], labels=["<=-10","-10~10","10~30","30~50",">=50"], include_lowest=True)
    out["up_price_bucket_m2"] = pd.cut(out["buy_up_price_m2"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["down_price_bucket_m2"] = pd.cut(out["buy_down_price_m2"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["up_price_bucket_m4"] = pd.cut(out["buy_up_price_m4"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["imb_sign_m2"] = np.where(out["size_imb_m2"] <= -0.1, "neg", np.where(out["size_imb_m2"] >= 0.1, "pos", "neu"))
    out["liq_sign_down_m2"] = np.where(out["buy_down_size_m2"] >= 350, "high", np.where(out["buy_down_size_m2"] >= 150, "mid", "low"))
    out["liq_sign_up_m2"] = np.where(out["buy_up_size_m2"] >= 350, "high", np.where(out["buy_up_size_m2"] >= 150, "mid", "low"))
    out["spread_sign_m2"] = np.where(out["spread_up_m2"].fillna(1) <= 0.04, "tight", "wide")
    out["regime"] = out.apply(regime_for_row, axis=1)
    return out


def regime_for_row(row: pd.Series) -> str:
    m1, m2, m4 = row.get("move_m1", np.nan), row.get("move_m2", np.nan), row.get("move_m4", np.nan)
    if pd.notna(m1) and m1 <= -20:
        return "early_drop"
    if pd.notna(m2) and -50 < m2 <= -30:
        return "sharp_drop_reversal"
    if pd.notna(m2) and -30 < m2 <= -10:
        return "mild_drop"
    if pd.notna(m2) and m2 >= 50:
        return "extreme_up"
    if pd.notna(m4) and 30 < m4 <= 50:
        return "late_breakout"
    return "neutral"


def choose_static(row: pd.Series, strategy: str) -> Tuple[str, int]:
    m1, m2, m4 = row.get("move_m1", np.nan), row.get("move_m2", np.nan), row.get("move_m4", np.nan)
    p1d, p2u, p2d, p4u = row.get("buy_down_price_m1", np.nan), row.get("buy_up_price_m2", np.nan), row.get("buy_down_price_m2", np.nan), row.get("buy_up_price_m4", np.nan)
    if strategy == "hist_m1_drop20_down":
        return ("buy_down", 1) if pd.notna(m1) and m1 <= -20 else ("skip", -1)
    if strategy == "hist_m2_drop10_down":
        return ("buy_down", 2) if pd.notna(m2) and m2 <= -10 else ("skip", -1)
    if strategy == "hist_m2_sharpdrop_reversal_up":
        return ("buy_up", 2) if pd.notna(m2) and -50 < m2 <= -30 else ("skip", -1)
    if strategy == "hist_m2_milddrop_down":
        return ("buy_down", 2) if pd.notna(m2) and -30 < m2 <= -10 else ("skip", -1)
    if strategy == "hist_m2_extremeup_fade_down":
        return ("buy_down", 2) if pd.notna(m2) and m2 >= 50 else ("skip", -1)
    if strategy == "hist_m4_rise30to50_up":
        return ("buy_up", 4) if pd.notna(m4) and 30 < m4 <= 50 else ("skip", -1)
    if strategy == "hist_switch_v1":
        if pd.notna(m1) and m1 <= -20 and pd.notna(p1d) and p1d <= 0.90:
            return ("buy_down", 1)
        if pd.notna(m2) and -50 < m2 <= -30 and pd.notna(p2u) and p2u <= 0.40:
            return ("buy_up", 2)
        if pd.notna(m2) and -30 < m2 <= -10 and pd.notna(p2d) and p2d <= 0.75:
            return ("buy_down", 2)
        if pd.notna(m2) and m2 >= 50 and pd.notna(p2d) and p2d <= 0.20:
            return ("buy_down", 2)
        if pd.notna(m4) and 30 < m4 <= 50 and pd.notna(p4u) and p4u <= 0.90:
            return ("buy_up", 4)
        return ("skip", -1)
    if strategy == "hist_switch_v2":
        if pd.notna(m2) and -50 < m2 <= -30 and pd.notna(p2u) and p2u <= 0.35:
            return ("buy_up", 2)
        if pd.notna(m1) and m1 <= -20 and pd.notna(p1d) and p1d <= 0.85:
            return ("buy_down", 1)
        if pd.notna(m4) and 30 < m4 <= 50 and pd.notna(p4u) and p4u <= 0.90:
            return ("buy_up", 4)
        if pd.notna(m2) and m2 >= 50 and pd.notna(p2d) and p2d <= 0.20:
            return ("buy_down", 2)
        return ("skip", -1)
    if strategy == "hist_switch_conservative":
        if pd.notna(m2) and -50 < m2 <= -30 and pd.notna(p2u) and p2u <= 0.30 and row.get("spread_up_m2", 1.0) <= 0.05:
            return ("buy_up", 2)
        if pd.notna(m1) and m1 <= -20 and pd.notna(p1d) and p1d <= 0.80:
            return ("buy_down", 1)
        if pd.notna(m4) and 30 < m4 <= 50 and pd.notna(p4u) and p4u <= 0.80:
            return ("buy_up", 4)
        return ("skip", -1)
    raise ValueError(strategy)


def exp_weighted_prob(values: np.ndarray, halflife: float = 20.0) -> float:
    if len(values) == 0:
        return np.nan
    idx = np.arange(len(values))
    weights = 0.5 ** ((len(values) - 1 - idx) / halflife)
    return float(np.dot(values, weights) / weights.sum())


def state_prob(hist: pd.DataFrame, row: pd.Series, side: str) -> Tuple[float, int]:
    if side == "buy_up":
        outcome_col = "outcome_up"
        subsets = [
            hist[(hist["regime"] == row["regime"]) & (hist["move_bucket_m2"] == row["move_bucket_m2"]) & (hist["up_price_bucket_m2"] == row["up_price_bucket_m2"])],
            hist[(hist["regime"] == row["regime"]) & (hist["move_bucket_m4"] == row["move_bucket_m4"]) & (hist["up_price_bucket_m4"] == row["up_price_bucket_m4"])],
            hist[(hist["regime"] == row["regime"]) & (hist["imb_sign_m2"] == row["imb_sign_m2"])],
            hist[(hist["regime"] == row["regime"])],
            hist,
        ]
    else:
        outcome_col = "outcome_down"
        h = hist.copy()
        h["outcome_down"] = 1.0 - h["outcome_up"]
        hist = h
        subsets = [
            hist[(hist["regime"] == row["regime"]) & (hist["move_bucket_m2"] == row["move_bucket_m2"]) & (hist["down_price_bucket_m2"] == row["down_price_bucket_m2"])],
            hist[(hist["regime"] == row["regime"]) & (hist["imb_sign_m2"] == row["imb_sign_m2"]) & (hist["liq_sign_down_m2"] == row["liq_sign_down_m2"])],
            hist[(hist["regime"] == row["regime"])],
            hist,
        ]
    for sub in subsets:
        if len(sub) >= 12:
            return exp_weighted_prob(sub[outcome_col].astype(float).to_numpy()), int(len(sub))
    return np.nan, 0


def candidate_micro_trades(row: pd.Series) -> List[Tuple[str, str, int, float, float]]:
    cands = []
    m1, m2, m4 = row.get("move_m1", np.nan), row.get("move_m2", np.nan), row.get("move_m4", np.nan)
    if pd.notna(m1) and m1 <= -20 and pd.notna(row.get("buy_down_price_m1")):
        cands.append(("early_drop_continuation", "buy_down", 1, float(row["buy_down_price_m1"]), float(row.get("spread_down_m1", 0.0) if pd.notna(row.get("spread_down_m1", np.nan)) else 0.0)))
    if pd.notna(m2) and -50 < m2 <= -30 and pd.notna(row.get("buy_up_price_m2")):
        cands.append(("sharp_drop_reversal", "buy_up", 2, float(row["buy_up_price_m2"]), float(row.get("spread_up_m2", 0.0) if pd.notna(row.get("spread_up_m2", np.nan)) else 0.0)))
    if pd.notna(m2) and -30 < m2 <= -10 and pd.notna(row.get("buy_down_price_m2")):
        cands.append(("mild_drop_continuation", "buy_down", 2, float(row["buy_down_price_m2"]), float(row.get("spread_down_m2", 0.0) if pd.notna(row.get("spread_down_m2", np.nan)) else 0.0)))
    if pd.notna(m2) and m2 >= 50 and pd.notna(row.get("buy_down_price_m2")):
        cands.append(("extreme_up_fade", "buy_down", 2, float(row["buy_down_price_m2"]), float(row.get("spread_down_m2", 0.0) if pd.notna(row.get("spread_down_m2", np.nan)) else 0.0)))
    if pd.notna(m4) and 30 < m4 <= 50 and pd.notna(row.get("buy_up_price_m4")):
        cands.append(("late_breakout_up", "buy_up", 4, float(row["buy_up_price_m4"]), float(row.get("spread_up_m4", 0.0) if pd.notna(row.get("spread_up_m4", np.nan)) else 0.0)))
    return cands


def choose_fairprob(hist: pd.DataFrame, row: pd.Series, mode: str, fee: float) -> Dict[str, object]:
    best = {"strategy": "skip", "side": "skip", "entry_minute": -1, "market_price": np.nan, "fair_prob": np.nan, "edge": np.nan, "support": 0}
    best_score = -1e9
    for micro, side, minute, price, spread in candidate_micro_trades(row):
        fair_prob, support = state_prob(hist, row, side)
        if pd.isna(fair_prob):
            continue
        edge = float(fair_prob - price)
        margin = fee + 0.02 + min(max(spread, 0.0), 0.05)
        if mode == "hist_fairprob_conservative":
            margin += 0.02
        if mode == "hist_fairprob_robust":
            margin += 0.015
        if support < 15:
            margin += 0.01
        if micro == "late_breakout_up" and price > 0.85:
            margin += 0.02
        if edge <= margin:
            continue
        if mode == "hist_fairprob_robust":
            score = edge + 0.002 * min(support, 50) - 0.05 * max(price - 0.8, 0.0)
        else:
            score = edge + 0.001 * min(support, 50)
        if score > best_score:
            best_score = score
            best = {"strategy": micro, "side": side, "entry_minute": minute, "market_price": price, "fair_prob": fair_prob, "edge": edge, "support": support}
    return best


def choose_trade(hist: pd.DataFrame, row: pd.Series, strategy: str, fee: float) -> Dict[str, object]:
    if strategy.startswith("hist_fairprob_"):
        return choose_fairprob(hist, row, strategy, fee)
    side, minute = choose_static(row, strategy)
    return {"strategy": strategy, "side": side, "entry_minute": minute, "market_price": np.nan, "fair_prob": np.nan, "edge": np.nan, "support": np.nan}


def payout(row: pd.Series, side: str, minute: int, fee: float) -> Tuple[float, float, float]:
    if side == "buy_up":
        price = row[f"buy_up_price_m{minute}"]
        size = row[f"buy_up_size_m{minute}"]
        pnl_per_share = row["outcome_up"] - price - fee
    else:
        price = row[f"buy_down_price_m{minute}"]
        size = row[f"buy_down_size_m{minute}"]
        pnl_per_share = (1.0 - row["outcome_up"]) - price - fee
    return float(price), float(size), float(pnl_per_share)


def simulate_strategy(df: pd.DataFrame, strategy: str, frac: float, fee: float) -> pd.DataFrame:
    bankroll, peak, max_dd = 100.0, 100.0, 0.0
    rows = []
    df = df.sort_values("first_quote_ts").reset_index(drop=True)
    for i in range(len(df)):
        row = df.iloc[i]
        hist = df.iloc[:i].copy()
        if len(hist) < 50:
            continue
        choice = choose_trade(hist, row, strategy, fee)
        side, minute = str(choice["side"]), int(choice["entry_minute"])
        if side == "skip" or minute < 0:
            continue
        price, size_avail, pnl_per_share = payout(row, side, minute, fee)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        target_cost = min(bankroll * frac, bankroll, float(size_avail) * float(price))
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        rows.append({
            "source_layer": "all_monthly_history",
            "strategy": strategy,
            "sizing": f"fixed_{int(frac*100)}pct",
            "run_name": row["run_name"],
            "market_id": row["market_id"],
            "slug": row["slug"],
            "first_quote_ts": row["first_quote_ts"],
            "regime": row["regime"],
            "entry_minute": minute,
            "side": side,
            "target_cost": target_cost,
            "shares": shares,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
            "fair_prob": choice.get("fair_prob", np.nan),
            "market_price": choice.get("market_price", np.nan),
            "edge": choice.get("edge", np.nan),
            "support": choice.get("support", np.nan),
            "sim_max_drawdown": max_dd,
        })
    return pd.DataFrame(rows)


def max_loss_streak(pnls: List[float]) -> int:
    best = cur = 0
    for x in pnls:
        if x < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def summarize_logs(logs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (strategy, sizing), g in logs.groupby(["strategy", "sizing"], dropna=False):
        pnl = pd.to_numeric(g["pnl_usd"], errors="coerce").dropna()
        cost = pd.to_numeric(g["target_cost"], errors="coerce")
        rtn = (pnl / cost).replace([np.inf, -np.inf], np.nan).dropna()
        wins = pnl[pnl > 0].sum()
        losses = pnl[pnl < 0].sum()
        profit_factor = np.nan if losses == 0 else float(wins / abs(losses))
        ending_bankroll = float(g["bankroll_after"].iloc[-1]) if len(g) else 100.0
        max_dd = float(g["sim_max_drawdown"].max()) if len(g) else 0.0
        rows.append({
            "source_layer": "all_monthly_history",
            "strategy": strategy,
            "sizing": sizing,
            "trades": int(len(g)),
            "ending_bankroll": ending_bankroll,
            "total_return": ending_bankroll / 100.0 - 1.0,
            "avg_trade_return_on_cost": float(rtn.mean()) if len(rtn) else np.nan,
            "median_trade_return_on_cost": float(rtn.median()) if len(rtn) else np.nan,
            "win_rate": float((pnl > 0).mean()) if len(pnl) else np.nan,
            "profit_factor": profit_factor,
            "max_drawdown": max_dd,
            "max_consecutive_losses": int(max_loss_streak(list(pnl))),
            "avg_entry_minute": float(g["entry_minute"].mean()) if len(g) else np.nan,
            "avg_edge": float(pd.to_numeric(g["edge"], errors="coerce").mean()) if len(g) else np.nan,
            "p10_trade_return_on_cost": float(rtn.quantile(0.10)) if len(rtn) else np.nan,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["score_end"] = out["ending_bankroll"].rank(pct=True)
    out["score_win"] = out["win_rate"].rank(pct=True)
    out["score_pf"] = out["profit_factor"].fillna(0).rank(pct=True)
    out["score_dd"] = (-out["max_drawdown"].fillna(999)).rank(pct=True)
    out["score_streak"] = (-out["max_consecutive_losses"].fillna(999)).rank(pct=True)
    out["score_tail"] = out["p10_trade_return_on_cost"].fillna(-999).rank(pct=True)
    out["robustness_score"] = 0.20*out["score_end"] + 0.20*out["score_win"] + 0.20*out["score_pf"] + 0.20*out["score_dd"] + 0.10*out["score_streak"] + 0.10*out["score_tail"]
    return out.sort_values(["ending_bankroll"], ascending=False).reset_index(drop=True)


def per_run_breakdown(logs: pd.DataFrame, finalists: pd.DataFrame) -> pd.DataFrame:
    if logs.empty or finalists.empty:
        return pd.DataFrame()
    keys = finalists[["strategy", "sizing"]].drop_duplicates()
    out = logs.merge(keys, on=["strategy", "sizing"], how="inner")
    grouped = out.groupby(["strategy", "sizing", "run_name"], as_index=False).agg(
        trades=("market_id", "size"),
        avg_pnl_usd=("pnl_usd", "mean"),
        total_pnl_usd=("pnl_usd", "sum"),
        avg_edge=("edge", "mean"),
    )
    grouped["win_rate"] = out.groupby(["strategy", "sizing", "run_name"])["pnl_usd"].apply(lambda s: float((s > 0).mean())).values
    return grouped.sort_values(["strategy", "sizing", "run_name"]).reset_index(drop=True)


def coverage_table(wide: pd.DataFrame, run_cover: pd.DataFrame) -> pd.DataFrame:
    markets = wide.groupby("run_name", as_index=False).agg(markets=("market_id", "size"))
    return run_cover.merge(markets, on="run_name", how="left").sort_values("run_name").reset_index(drop=True)


def make_plots(summary: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not summary.empty:
        top = summary.sort_values("ending_bankroll", ascending=False).head(20)
        labels = top["strategy"] + "|" + top["sizing"]
        plt.figure(figsize=(14, 5))
        plt.bar(labels, top["ending_bankroll"])
        plt.xticks(rotation=70, ha="right")
        plt.title("全历史策略 Top 期末本金")
        plt.tight_layout()
        p = fig_dir / "all_history_top_endings.png"
        plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))

        robust = summary.sort_values("robustness_score", ascending=False).head(20)
        labels = robust["strategy"] + "|" + robust["sizing"]
        plt.figure(figsize=(14, 5))
        plt.bar(labels, robust["robustness_score"])
        plt.xticks(rotation=70, ha="right")
        plt.title("全历史策略 Top 稳定性得分")
        plt.tight_layout()
        p = fig_dir / "all_history_top_robustness.png"
        plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))
    return paths


def markdown_table(df: pd.DataFrame, rows: int = 30) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def build_report(coverage: pd.DataFrame, summary: pd.DataFrame, finalists_run: pd.DataFrame) -> str:
    lines = []
    lines.append("# monthly_runs 全历史 5分钟 BTC 事件：系统性策略研究")
    lines.append("")
    lines.append("这份报告会把 `data/monthly_runs/*` 下所有找到的 5 分钟 BTC 数据统一汇总，重新找历史上更稳、更强的交易策略。")
    lines.append("")
    lines.append("## 数据覆盖")
    lines.append("")
    lines.append(markdown_table(coverage, rows=100))
    lines.append("")
    lines.append("## 候选策略说明")
    lines.append("")
    lines.append("- `hist_m1_drop20_down`：第1分钟跌超20，买Down")
    lines.append("- `hist_m2_drop10_down`：第2分钟跌超10，买Down")
    lines.append("- `hist_m2_sharpdrop_reversal_up`：第2分钟跌在(-50,-30]，买Up")
    lines.append("- `hist_m2_milddrop_down`：第2分钟跌在(-30,-10]，买Down")
    lines.append("- `hist_m2_extremeup_fade_down`：第2分钟涨超50，买Down")
    lines.append("- `hist_m4_rise30to50_up`：第4分钟涨在(30,50]，买Up")
    lines.append("- `hist_switch_v1 / v2 / conservative`：分钟感知组合策略")
    lines.append("- `hist_fairprob_switch / conservative / robust`：先估 fair probability，再与市场价格比较，只有 edge 足够大才交易")
    lines.append("")
    lines.append("## 收益最高 Top 20")
    lines.append("")
    lines.append(markdown_table(summary.sort_values("ending_bankroll", ascending=False), rows=20))
    lines.append("")
    lines.append("## 稳定性最好 Top 20")
    lines.append("")
    lines.append(markdown_table(summary.sort_values("robustness_score", ascending=False), rows=20))
    lines.append("")
    if not summary.empty:
        best_end = summary.sort_values("ending_bankroll", ascending=False).iloc[0]
        best_rob = summary.sort_values("robustness_score", ascending=False).iloc[0]
        lines.append("## 两种“最佳”")
        lines.append("")
        lines.append(f"- 收益最高：**{best_end['strategy']} | {best_end['sizing']}**，期末本金 **{best_end['ending_bankroll']:.2f} USD**，最大回撤 **{best_end['max_drawdown']:.2%}**。")
        lines.append(f"- 稳定性最好：**{best_rob['strategy']} | {best_rob['sizing']}**，稳健得分 **{best_rob['robustness_score']:.4f}**，期末本金 **{best_rob['ending_bankroll']:.2f} USD**，最大回撤 **{best_rob['max_drawdown']:.2%}**。")
        lines.append("")
    lines.append("## finalist 按 run 的表现")
    lines.append("")
    lines.append(markdown_table(finalists_run, rows=100))
    lines.append("")
    lines.append("## 查缺补漏")
    lines.append("")
    lines.append("这版已经把现有 repo 数据里能做的都往 fair probability / regime / 微观结构近似 方向推进了。")
    lines.append("但这几个关键变量，当前数据里仍然没有，所以下一步最值得补：")
    lines.append("")
    lines.append("- Chainlink 对齐标签与历史 report")
    lines.append("- Polymarket 自己更细的 orderbook / trade stream")
    lines.append("- Binance perp OI / taker flow / liquidation")
    lines.append("- Deribit IV / basis / OI")
    lines.append("- 宏观事件日历")
    lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![全历史策略 Top 期末本金](all_history_figures/all_history_top_endings.png)")
    lines.append("")
    lines.append("![全历史策略 Top 稳定性得分](all_history_figures/all_history_top_robustness.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    raw, run_cover = read_all_monthly_runs(Path(args.source_root))
    quotes = prepare_quotes(raw)
    wide = add_features(build_wide(quotes))
    coverage = coverage_table(wide, run_cover)

    logs = []
    for strategy in STATIC_STRATEGIES:
        for frac in FIXED_FRACS:
            lg = simulate_strategy(wide, strategy, frac, args.fee)
            if not lg.empty:
                logs.append(lg)
    logs_df = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()
    summary = summarize_logs(logs_df)

    top_end = summary.sort_values("ending_bankroll", ascending=False).head(10) if not summary.empty else pd.DataFrame()
    top_rob = summary.sort_values("robustness_score", ascending=False).head(10) if not summary.empty else pd.DataFrame()
    finalists = pd.concat([top_end, top_rob], ignore_index=True).drop_duplicates(subset=["strategy", "sizing"]) if not summary.empty else pd.DataFrame()
    finalists_run = per_run_breakdown(logs_df, finalists)

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "all_history_figures"
    report_dir.mkdir(parents=True, exist_ok=True)
    figs = make_plots(summary, fig_dir)

    coverage.to_csv(report_dir / "all_history_run_coverage.csv", index=False)
    logs_df.to_csv(report_dir / "all_history_trade_logs.csv", index=False)
    summary.to_csv(report_dir / "all_history_strategy_summary.csv", index=False)
    finalists_run.to_csv(report_dir / "all_history_finalists_run_breakdown.csv", index=False)
    (report_dir / "all_history_strategy_report.md").write_text(build_report(coverage, summary, finalists_run), encoding="utf-8")
    (report_dir / "all_history_strategy_summary.json").write_text(json.dumps({"rows_summary": int(len(summary)), "rows_logs": int(len(logs_df)), "rows_coverage": int(len(coverage)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_summary": int(len(summary)), "rows_logs": int(len(logs_df)), "rows_coverage": int(len(coverage)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

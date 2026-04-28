from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

FILE_GLOB = "*btc-updown-5m_quotes.csv"
FIXED_FRACS = [0.10, 0.15, 0.20, 0.25]
REQUIRED_NUMERIC = [
    "buy_up_cents", "buy_down_cents", "sell_up_cents", "sell_down_cents",
    "buy_up_size", "buy_down_size", "sell_up_size", "sell_down_size",
    "mid_up_cents", "mid_down_cents", "spread_up_cents", "spread_down_cents",
    "bid_depth_up_5", "ask_depth_up_5", "bid_depth_down_5", "ask_depth_down_5",
    "target_price", "final_price", "trade_count_1s", "trade_volume_1s",
]
STRATEGIES = [
    "static_m1_drop20_down",
    "static_m2_drop10_down_liq",
    "static_m2_sharpdrop_up_liq",
    "static_m2_milddrop_down_book",
    "static_m2_extremeup_fade_down",
    "static_m4_breakout_up_tight",
    "state_selector_pnl",
    "state_selector_robust",
    "logistic_selector_edge",
    "logistic_selector_robust",
]
LOGIT_FEATURES = [
    "btc_move_1m", "btc_move_2m", "btc_move_4m", "mid_up_prob_open", "mid_up_prob_2m",
    "mid_up_prob_change_2m", "buy_up_price_2m", "buy_down_price_2m", "buy_up_size_2m",
    "buy_down_size_2m", "sell_up_size_2m", "sell_down_size_2m", "size_imbalance_updown_2m",
    "sell_size_imbalance_updown_2m", "bid_depth_imbalance_updown_2m", "book_pressure_up_2m",
    "book_pressure_down_2m", "spread_up_median_first2m", "spread_down_median_first2m",
    "overround_median_first2m", "quote_count_first2m", "trade_count_sum_first2m",
    "trade_volume_sum_first2m", "realized_vol_first2m", "path_efficiency_first2m",
    "max_drawdown_first2m", "max_rebound_first2m",
]


def first_non_null(s: pd.Series):
    x = s.dropna()
    return np.nan if x.empty else x.iloc[0]


def last_non_null(s: pd.Series):
    x = s.dropna()
    return np.nan if x.empty else x.iloc[-1]


def parse_close_ts_from_slug(slug: str) -> pd.Timestamp:
    m = re.search(r"(\d{10})$", str(slug))
    if not m:
        return pd.NaT
    return pd.to_datetime(int(m.group(1)), unit="s", utc=True)


def scan_run_dirs(source_root: Path) -> List[Path]:
    return sorted([p for p in source_root.iterdir() if p.is_dir()]) if source_root.exists() else []


def read_all_monthly_runs(source_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    frames: List[pd.DataFrame] = []
    cover: List[Dict[str, object]] = []
    for run_dir in scan_run_dirs(source_root):
        files = sorted(run_dir.glob(FILE_GLOB))
        if not files:
            continue
        total_rows = 0
        for p in files:
            df = pd.read_csv(p)
            df["run_name"] = run_dir.name
            df["source_file"] = p.name
            frames.append(df)
            total_rows += len(df)
        cover.append({"run_name": run_dir.name, "file_count": len(files), "raw_row_count": total_rows})
    if not frames:
        raise FileNotFoundError(f"No files matching {FILE_GLOB} under {source_root}")
    raw = pd.concat(frames, ignore_index=True)
    raw.columns = [str(c).strip() for c in raw.columns]
    return raw, pd.DataFrame(cover)


def ensure_columns(df: pd.DataFrame, cols: List[str]) -> None:
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan


def prepare_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    q = raw.copy()
    ensure_columns(q, REQUIRED_NUMERIC + ["ts_iso", "slug", "run_name", "source_file"])
    q["ts_utc"] = pd.to_datetime(q["ts_iso"], utc=True, errors="coerce")
    for c in REQUIRED_NUMERIC:
        q[c] = pd.to_numeric(q[c], errors="coerce")
    q["close_ts_utc"] = q["slug"].map(parse_close_ts_from_slug)
    if q["mid_up_cents"].isna().any():
        q["mid_up_cents"] = q["mid_up_cents"].fillna((q["buy_up_cents"] + q["sell_up_cents"]) / 2.0)
    if q["mid_down_cents"].isna().any():
        q["mid_down_cents"] = q["mid_down_cents"].fillna((q["buy_down_cents"] + q["sell_down_cents"]) / 2.0)
    q["mid_up_prob"] = q["mid_up_cents"] / 100.0
    q["mid_down_prob"] = q["mid_down_cents"] / 100.0
    q["mid_sum_cents"] = q[["mid_up_cents", "mid_down_cents"]].sum(axis=1, min_count=2)
    q["mid_overround_cents"] = q["mid_sum_cents"] - 100.0
    q["btc_move_from_target"] = q["final_price"] - q["target_price"]
    q["market_id"] = q["run_name"].astype(str) + "::" + q["slug"].astype(str)
    q = q.sort_values(["run_name", "slug", "ts_utc", "source_file"]).drop_duplicates(subset=["run_name", "ts_iso", "slug"], keep="last")
    return q.reset_index(drop=True)


def snapshot_row(g: pd.DataFrame, minute: int) -> pd.Series | None:
    first_ts = g["ts_utc"].min()
    snap = g[g["ts_utc"] <= first_ts + pd.Timedelta(minutes=minute)].copy()
    return None if snap.empty else snap.iloc[-1]


def price_from_row(row: pd.Series | None, col: str) -> float:
    if row is None:
        return np.nan
    v = pd.to_numeric(row.get(col), errors="coerce")
    return np.nan if pd.isna(v) else float(v) / 100.0


def num_from_row(row: pd.Series | None, col: str) -> float:
    if row is None:
        return np.nan
    return pd.to_numeric(row.get(col), errors="coerce")


def regime_for_row(row: pd.Series) -> str:
    m1, m2, m4 = row.get("btc_move_1m", np.nan), row.get("btc_move_2m", np.nan), row.get("btc_move_4m", np.nan)
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


def build_features(quotes: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for market_id, g in quotes.groupby("market_id", dropna=False):
        g = g.sort_values("ts_utc").copy()
        if g.empty:
            continue
        first_ts = g["ts_utc"].min()
        target = first_non_null(g["target_price"])
        final = last_non_null(g["final_price"])
        outcome_up = np.nan if pd.isna(target) or pd.isna(final) else float(final > target)
        row1, row2, row4 = snapshot_row(g, 1), snapshot_row(g, 2), snapshot_row(g, 4)
        if row2 is None:
            continue
        first2 = g[g["ts_utc"] <= first_ts + pd.Timedelta(minutes=2)].copy()
        path = pd.to_numeric(first2["final_price"], errors="coerce").dropna()
        diffs = path.diff().dropna()
        total_abs = float(diffs.abs().sum()) if not diffs.empty else 0.0
        net_move = float(path.iloc[-1] - path.iloc[0]) if len(path) >= 2 else 0.0
        realized_vol = float(diffs.std()) if len(diffs) >= 2 else 0.0
        path_eff = float(abs(net_move) / total_abs) if total_abs > 0 else 1.0
        move_series = pd.to_numeric(first2["btc_move_from_target"], errors="coerce").dropna()
        max_drawdown = abs(float(move_series.min())) if not move_series.empty else np.nan
        max_rebound = float(move_series.max()) if not move_series.empty else np.nan

        buy_up_size_2m = num_from_row(row2, "buy_up_size")
        buy_down_size_2m = num_from_row(row2, "buy_down_size")
        sell_up_size_2m = num_from_row(row2, "sell_up_size")
        sell_down_size_2m = num_from_row(row2, "sell_down_size")
        bid_depth_up_2m = num_from_row(row2, "bid_depth_up_5")
        bid_depth_down_2m = num_from_row(row2, "bid_depth_down_5")
        ask_depth_up_2m = num_from_row(row2, "ask_depth_up_5")
        ask_depth_down_2m = num_from_row(row2, "ask_depth_down_5")
        size_imb = np.nan if pd.isna(buy_up_size_2m) or pd.isna(buy_down_size_2m) or (buy_up_size_2m + buy_down_size_2m) <= 0 else float((buy_up_size_2m - buy_down_size_2m) / (buy_up_size_2m + buy_down_size_2m))
        sell_size_imb = np.nan if pd.isna(sell_up_size_2m) or pd.isna(sell_down_size_2m) or (sell_up_size_2m + sell_down_size_2m) <= 0 else float((sell_up_size_2m - sell_down_size_2m) / (sell_up_size_2m + sell_down_size_2m))
        bid_depth_imb = np.nan if pd.isna(bid_depth_up_2m) or pd.isna(bid_depth_down_2m) or (bid_depth_up_2m + bid_depth_down_2m) <= 0 else float((bid_depth_up_2m - bid_depth_down_2m) / (bid_depth_up_2m + bid_depth_down_2m))
        book_pressure_up = np.nan if pd.isna(bid_depth_up_2m) or pd.isna(ask_depth_up_2m) or (bid_depth_up_2m + ask_depth_up_2m) <= 0 else float((bid_depth_up_2m - ask_depth_up_2m) / (bid_depth_up_2m + ask_depth_up_2m))
        book_pressure_down = np.nan if pd.isna(bid_depth_down_2m) or pd.isna(ask_depth_down_2m) or (bid_depth_down_2m + ask_depth_down_2m) <= 0 else float((bid_depth_down_2m - ask_depth_down_2m) / (bid_depth_down_2m + ask_depth_down_2m))

        rows.append({
            "market_id": market_id,
            "run_name": first_non_null(g["run_name"]),
            "slug": first_non_null(g["slug"]),
            "first_quote_ts": first_ts,
            "outcome_up": outcome_up,
            "quote_count_first2m": int(len(first2)),
            "btc_move_1m": np.nan if row1 is None or pd.isna(target) or pd.isna(row1.get("final_price")) else float(row1["final_price"] - target),
            "btc_move_2m": np.nan if pd.isna(target) or pd.isna(row2.get("final_price")) else float(row2["final_price"] - target),
            "btc_move_4m": np.nan if row4 is None or pd.isna(target) or pd.isna(row4.get("final_price")) else float(row4["final_price"] - target),
            "mid_up_prob_open": pd.to_numeric(first2.iloc[0].get("mid_up_prob"), errors="coerce") if not first2.empty else np.nan,
            "mid_up_prob_2m": num_from_row(row2, "mid_up_prob"),
            "mid_up_prob_change_2m": np.nan if first2.empty or pd.isna(pd.to_numeric(first2.iloc[0].get("mid_up_prob"), errors="coerce")) or pd.isna(num_from_row(row2, "mid_up_prob")) else float(num_from_row(row2, "mid_up_prob") - pd.to_numeric(first2.iloc[0].get("mid_up_prob"), errors="coerce")),
            "buy_up_price_1m": price_from_row(row1, "buy_up_cents"),
            "buy_down_price_1m": price_from_row(row1, "buy_down_cents"),
            "buy_up_price_2m": price_from_row(row2, "buy_up_cents"),
            "buy_down_price_2m": price_from_row(row2, "buy_down_cents"),
            "buy_up_price_4m": price_from_row(row4, "buy_up_cents"),
            "buy_down_price_4m": price_from_row(row4, "buy_down_cents"),
            "buy_up_size_1m": num_from_row(row1, "buy_up_size"),
            "buy_down_size_1m": num_from_row(row1, "buy_down_size"),
            "buy_up_size_2m": buy_up_size_2m,
            "buy_down_size_2m": buy_down_size_2m,
            "buy_up_size_4m": num_from_row(row4, "buy_up_size"),
            "buy_down_size_4m": num_from_row(row4, "buy_down_size"),
            "sell_up_size_2m": sell_up_size_2m,
            "sell_down_size_2m": sell_down_size_2m,
            "size_imbalance_updown_2m": size_imb,
            "sell_size_imbalance_updown_2m": sell_size_imb,
            "bid_depth_imbalance_updown_2m": bid_depth_imb,
            "book_pressure_up_2m": book_pressure_up,
            "book_pressure_down_2m": book_pressure_down,
            "spread_up_median_first2m": pd.to_numeric(first2["spread_up_cents"], errors="coerce").median() / 100.0,
            "spread_down_median_first2m": pd.to_numeric(first2["spread_down_cents"], errors="coerce").median() / 100.0,
            "overround_median_first2m": pd.to_numeric(first2["mid_overround_cents"], errors="coerce").median() / 100.0,
            "trade_count_sum_first2m": pd.to_numeric(first2["trade_count_1s"], errors="coerce").sum(min_count=1),
            "trade_volume_sum_first2m": pd.to_numeric(first2["trade_volume_1s"], errors="coerce").sum(min_count=1),
            "realized_vol_first2m": realized_vol,
            "path_efficiency_first2m": path_eff,
            "max_drawdown_first2m": max_drawdown,
            "max_rebound_first2m": max_rebound,
            "realized_pnl_buy_up_1m": np.nan if pd.isna(outcome_up) or pd.isna(price_from_row(row1, "buy_up_cents")) else float(outcome_up - price_from_row(row1, "buy_up_cents") - 0.01),
            "realized_pnl_buy_down_1m": np.nan if pd.isna(outcome_up) or pd.isna(price_from_row(row1, "buy_down_cents")) else float((1.0 - outcome_up) - price_from_row(row1, "buy_down_cents") - 0.01),
            "realized_pnl_buy_up_2m": np.nan if pd.isna(outcome_up) or pd.isna(price_from_row(row2, "buy_up_cents")) else float(outcome_up - price_from_row(row2, "buy_up_cents") - 0.01),
            "realized_pnl_buy_down_2m": np.nan if pd.isna(outcome_up) or pd.isna(price_from_row(row2, "buy_down_cents")) else float((1.0 - outcome_up) - price_from_row(row2, "buy_down_cents") - 0.01),
            "realized_pnl_buy_up_4m": np.nan if pd.isna(outcome_up) or pd.isna(price_from_row(row4, "buy_up_cents")) else float(outcome_up - price_from_row(row4, "buy_up_cents") - 0.01),
            "realized_pnl_buy_down_4m": np.nan if pd.isna(outcome_up) or pd.isna(price_from_row(row4, "buy_down_cents")) else float((1.0 - outcome_up) - price_from_row(row4, "buy_down_cents") - 0.01),
        })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).sort_values("first_quote_ts").reset_index(drop=True)
    out["regime"] = out.apply(regime_for_row, axis=1)
    out["up_price_bucket_2m"] = pd.cut(out["buy_up_price_2m"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["down_price_bucket_2m"] = pd.cut(out["buy_down_price_2m"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["liq_sign_down_2m"] = np.where(out["buy_down_size_2m"] >= 300, "high", np.where(out["buy_down_size_2m"] >= 120, "mid", "low"))
    out["liq_sign_up_2m"] = np.where(out["buy_up_size_2m"] >= 300, "high", np.where(out["buy_up_size_2m"] >= 120, "mid", "low"))
    out["imb_sign_2m"] = np.where(out["size_imbalance_updown_2m"] <= -0.1, "neg", np.where(out["size_imbalance_updown_2m"] >= 0.1, "pos", "neu"))
    out["spread_sign_2m"] = np.where(out["spread_up_median_first2m"] <= 0.05, "tight", "wide")
    return out


def rolling_logit(df: pd.DataFrame, min_history: int = 100, retrain_every: int = 25, lookback: int = 1200) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    df = df.sort_values("first_quote_ts").reset_index(drop=True)
    probs = np.full(len(df), np.nan)
    model = None
    med = None
    next_retrain = min_history
    for i in range(len(df)):
        if i < min_history:
            continue
        if model is None or i >= next_retrain:
            hist = df.iloc[max(0, i - lookback):i].copy()
            y = hist["outcome_up"].astype(int)
            if y.nunique() < 2:
                next_retrain = i + retrain_every
                continue
            X = hist[LOGIT_FEATURES]
            med = X.median(numeric_only=True)
            X = X.fillna(med)
            weights = 0.5 ** ((len(hist) - 1 - np.arange(len(hist))) / 200.0)
            try:
                model = LogisticRegression(max_iter=1000)
                model.fit(X, y, sample_weight=weights)
            except Exception:
                model = None
            next_retrain = i + retrain_every
        if model is not None and med is not None:
            x = df.iloc[[i]][LOGIT_FEATURES].fillna(med)
            try:
                probs[i] = float(model.predict_proba(x)[:, 1][0])
            except Exception:
                pass
    return pd.Series(probs, index=df.index)


def candidate_static(row: pd.Series, strategy: str) -> Tuple[str, int]:
    m1, m2, m4 = row.get("btc_move_1m", np.nan), row.get("btc_move_2m", np.nan), row.get("btc_move_4m", np.nan)
    if strategy == "static_m1_drop20_down":
        return ("buy_down", 1) if pd.notna(m1) and m1 <= -20 else ("skip", -1)
    if strategy == "static_m2_drop10_down_liq":
        return ("buy_down", 2) if pd.notna(m2) and m2 <= -10 and row.get("buy_down_size_2m", 0) >= 120 and row.get("spread_down_median_first2m", 1.0) <= 0.06 else ("skip", -1)
    if strategy == "static_m2_sharpdrop_up_liq":
        return ("buy_up", 2) if pd.notna(m2) and -50 < m2 <= -30 and row.get("buy_up_size_2m", 0) >= 120 and row.get("spread_up_median_first2m", 1.0) <= 0.06 and row.get("buy_up_price_2m", 1.0) <= 0.45 else ("skip", -1)
    if strategy == "static_m2_milddrop_down_book":
        return ("buy_down", 2) if pd.notna(m2) and -30 < m2 <= -10 and row.get("size_imbalance_updown_2m", 1.0) <= 0 and row.get("book_pressure_down_2m", -1.0) >= -0.2 else ("skip", -1)
    if strategy == "static_m2_extremeup_fade_down":
        return ("buy_down", 2) if pd.notna(m2) and m2 >= 50 and row.get("buy_down_price_2m", 1.0) <= 0.20 else ("skip", -1)
    if strategy == "static_m4_breakout_up_tight":
        return ("buy_up", 4) if pd.notna(m4) and 30 < m4 <= 50 and row.get("buy_up_size_4m", 0) >= 100 and row.get("buy_up_price_4m", 1.0) <= 0.90 else ("skip", -1)
    return ("skip", -1)


def candidate_micro_trades(row: pd.Series) -> List[Tuple[str, str, int]]:
    out = []
    if pd.notna(row.get("btc_move_1m")) and row["btc_move_1m"] <= -20:
        out.append(("early_drop_cont", "buy_down", 1))
    if pd.notna(row.get("btc_move_2m")) and -50 < row["btc_move_2m"] <= -30:
        out.append(("sharp_drop_rev", "buy_up", 2))
    if pd.notna(row.get("btc_move_2m")) and -30 < row["btc_move_2m"] <= -10:
        out.append(("mild_drop_cont", "buy_down", 2))
    if pd.notna(row.get("btc_move_2m")) and row["btc_move_2m"] >= 50:
        out.append(("extreme_up_fade", "buy_down", 2))
    if pd.notna(row.get("btc_move_4m")) and 30 < row["btc_move_4m"] <= 50:
        out.append(("late_breakout", "buy_up", 4))
    return out


def candidate_mask(df: pd.DataFrame, micro: str) -> pd.Series:
    if micro == "early_drop_cont":
        return df["btc_move_1m"] <= -20
    if micro == "sharp_drop_rev":
        return (df["btc_move_2m"] > -50) & (df["btc_move_2m"] <= -30)
    if micro == "mild_drop_cont":
        return (df["btc_move_2m"] > -30) & (df["btc_move_2m"] <= -10)
    if micro == "extreme_up_fade":
        return df["btc_move_2m"] >= 50
    if micro == "late_breakout":
        return (df["btc_move_4m"] > 30) & (df["btc_move_4m"] <= 50)
    return pd.Series(False, index=df.index)


def exp_weighted_mean(values: np.ndarray, halflife: float = 25.0) -> float:
    if len(values) == 0:
        return np.nan
    idx = np.arange(len(values))
    weights = 0.5 ** ((len(values) - 1 - idx) / halflife)
    return float(np.dot(values, weights) / weights.sum())


def candidate_history_stats(hist: pd.DataFrame, row: pd.Series, micro: str, side: str, minute: int) -> Tuple[float, float, int]:
    sub = hist[candidate_mask(hist, micro)].copy()
    sub = sub[sub["regime"] == row["regime"]]
    if minute == 2:
        if side == "buy_up":
            sub = sub[(sub["up_price_bucket_2m"] == row["up_price_bucket_2m"]) & (sub["liq_sign_up_2m"] == row["liq_sign_up_2m"]) & (sub["spread_sign_2m"] == row["spread_sign_2m"])]
            pnl = pd.to_numeric(sub["realized_pnl_buy_up_2m"], errors="coerce").dropna().to_numpy()
        else:
            sub = sub[(sub["down_price_bucket_2m"] == row["down_price_bucket_2m"]) & (sub["liq_sign_down_2m"] == row["liq_sign_down_2m"]) & (sub["imb_sign_2m"] == row["imb_sign_2m"])]
            pnl = pd.to_numeric(sub["realized_pnl_buy_down_2m"], errors="coerce").dropna().to_numpy()
    elif minute == 1:
        pnl = pd.to_numeric(sub["realized_pnl_buy_down_1m"], errors="coerce").dropna().to_numpy()
    else:
        pnl = pd.to_numeric(sub["realized_pnl_buy_up_4m"], errors="coerce").dropna().to_numpy()
    if len(pnl) < 12:
        return np.nan, np.nan, int(len(pnl))
    return exp_weighted_mean(pnl), float((pnl > 0).mean()), int(len(pnl))


def choose_trade(hist: pd.DataFrame, row: pd.Series, strategy: str, fee: float) -> Dict[str, object]:
    if strategy.startswith("static_"):
        side, minute = candidate_static(row, strategy)
        return {"side": side, "minute": minute, "score": np.nan, "support": np.nan, "micro": strategy}
    if strategy.startswith("logistic_selector"):
        q_up = row.get("pred_prob_up_logit", np.nan)
        if pd.isna(q_up):
            return {"side": "skip", "minute": -1, "score": np.nan, "support": np.nan, "micro": "none"}
        best = {"side": "skip", "minute": -1, "score": -1e9, "support": np.nan, "micro": "none"}
        for micro, side, minute in candidate_micro_trades(row):
            price = row[f"buy_up_price_{minute}m"] if side == "buy_up" else row[f"buy_down_price_{minute}m"]
            if pd.isna(price):
                continue
            q_side = q_up if side == "buy_up" else 1.0 - q_up
            spread_pen = 0.0 if minute != 2 else (row.get("spread_up_median_first2m", 0.0) if side == "buy_up" else row.get("spread_down_median_first2m", 0.0))
            edge = q_side - price - fee - min(max(spread_pen, 0.0), 0.05)
            margin = 0.01 if strategy == "logistic_selector_edge" else 0.03
            if edge <= margin:
                continue
            if strategy == "logistic_selector_robust" and price > 0.85 and side == "buy_up":
                continue
            if edge > best["score"]:
                best = {"side": side, "minute": minute, "score": float(edge), "support": np.nan, "micro": micro}
        return best
    best = {"side": "skip", "minute": -1, "score": -1e9, "support": 0, "micro": "none"}
    for micro, side, minute in candidate_micro_trades(row):
        mean_pnl, win_rate, support = candidate_history_stats(hist, row, micro, side, minute)
        if pd.isna(mean_pnl):
            continue
        score = mean_pnl
        if strategy == "state_selector_robust":
            if support < 20 or win_rate < 0.55 or mean_pnl <= 0.02:
                continue
            score = mean_pnl + 0.01 * min(20, support) / 20.0
        else:
            if mean_pnl <= 0.0:
                continue
        if score > best["score"]:
            best = {"side": side, "minute": minute, "score": float(score), "support": support, "micro": micro}
    return best


def payout(row: pd.Series, side: str, minute: int, fee: float) -> Tuple[float, float, float]:
    if side == "buy_up":
        price = row[f"buy_up_price_{minute}m"]
        size = row[f"buy_up_size_{minute}m"]
        pnl_per_share = row["outcome_up"] - price - fee
    else:
        price = row[f"buy_down_price_{minute}m"]
        size = row[f"buy_down_size_{minute}m"]
        pnl_per_share = (1.0 - row["outcome_up"]) - price - fee
    return float(price), float(size), float(pnl_per_share)


def simulate_strategy(df: pd.DataFrame, strategy: str, frac: float, fee: float) -> pd.DataFrame:
    bankroll, peak, max_dd = 100.0, 100.0, 0.0
    rows: List[Dict[str, object]] = []
    df = df.sort_values("first_quote_ts").reset_index(drop=True)
    for i in range(len(df)):
        row = df.iloc[i]
        hist = df.iloc[:i].copy()
        if len(hist) < 60:
            continue
        choice = choose_trade(hist, row, strategy, fee)
        side, minute = str(choice["side"]), int(choice["minute"])
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
            "strategy": strategy,
            "sizing": f"fixed_{int(frac*100)}pct",
            "run_name": row["run_name"],
            "market_id": row["market_id"],
            "slug": row["slug"],
            "first_quote_ts": row["first_quote_ts"],
            "regime": row["regime"],
            "entry_minute": minute,
            "side": side,
            "micro": choice.get("micro", strategy),
            "signal_score": choice.get("score", np.nan),
            "support": choice.get("support", np.nan),
            "target_cost": target_cost,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
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
        rows.append({
            "source_layer": "all_monthly_clob_systematic",
            "strategy": strategy,
            "sizing": sizing,
            "trades": int(len(g)),
            "ending_bankroll": ending_bankroll,
            "total_return": ending_bankroll / 100.0 - 1.0,
            "avg_trade_return_on_cost": float(rtn.mean()) if len(rtn) else np.nan,
            "median_trade_return_on_cost": float(rtn.median()) if len(rtn) else np.nan,
            "win_rate": float((pnl > 0).mean()) if len(pnl) else np.nan,
            "profit_factor": profit_factor,
            "max_drawdown": float(g["sim_max_drawdown"].max()) if len(g) else 0.0,
            "max_consecutive_losses": int(max_loss_streak(list(pnl))),
            "avg_entry_minute": float(g["entry_minute"].mean()) if len(g) else np.nan,
            "avg_signal_score": float(pd.to_numeric(g["signal_score"], errors="coerce").mean()) if len(g) else np.nan,
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
    return out.sort_values("ending_bankroll", ascending=False).reset_index(drop=True)


def per_run_breakdown(logs: pd.DataFrame, finalists: pd.DataFrame) -> pd.DataFrame:
    if logs.empty or finalists.empty:
        return pd.DataFrame()
    keys = finalists[["strategy", "sizing"]].drop_duplicates()
    out = logs.merge(keys, on=["strategy", "sizing"], how="inner")
    grouped = out.groupby(["strategy", "sizing", "run_name"], as_index=False).agg(
        trades=("market_id", "size"),
        avg_pnl_usd=("pnl_usd", "mean"),
        total_pnl_usd=("pnl_usd", "sum"),
        avg_signal_score=("signal_score", "mean"),
    )
    grouped["win_rate"] = out.groupby(["strategy", "sizing", "run_name"])["pnl_usd"].apply(lambda s: float((s > 0).mean())).values
    return grouped.sort_values(["strategy", "sizing", "run_name"]).reset_index(drop=True)


def used_feature_catalog() -> pd.DataFrame:
    rows = [
        ("buy_up_size_2m", "liquidity", "2分钟买Up可成交量"),
        ("buy_down_size_2m", "liquidity", "2分钟买Down可成交量"),
        ("sell_up_size_2m", "liquidity", "2分钟卖Up对手量"),
        ("sell_down_size_2m", "liquidity", "2分钟卖Down对手量"),
        ("bid_depth_imbalance_updown_2m", "depth", "bid depth不平衡"),
        ("book_pressure_up_2m", "book_pressure", "Up侧盘口压力"),
        ("book_pressure_down_2m", "book_pressure", "Down侧盘口压力"),
        ("size_imbalance_updown_2m", "imbalance", "买盘量不平衡"),
        ("sell_size_imbalance_updown_2m", "imbalance", "卖盘量不平衡"),
        ("spread_up_median_first2m", "spread", "前2分钟Up spread中位数"),
        ("spread_down_median_first2m", "spread", "前2分钟Down spread中位数"),
        ("overround_median_first2m", "pricing", "前2分钟overround中位数"),
        ("trade_count_sum_first2m", "trading_activity", "前2分钟成交笔数总和"),
        ("trade_volume_sum_first2m", "trading_activity", "前2分钟成交量总和"),
        ("quote_count_first2m", "quote_activity", "前2分钟quote数量"),
        ("realized_vol_first2m", "path", "前2分钟路径波动"),
        ("path_efficiency_first2m", "path", "前2分钟路径效率"),
        ("max_drawdown_first2m", "path", "前2分钟最大下探"),
        ("max_rebound_first2m", "path", "前2分钟最大反弹"),
    ]
    return pd.DataFrame(rows, columns=["feature", "category", "description"])


def coverage_table(features: pd.DataFrame, run_cover: pd.DataFrame) -> pd.DataFrame:
    mkts = features.groupby("run_name", as_index=False).agg(markets=("market_id", "size")) if not features.empty else pd.DataFrame(columns=["run_name", "markets"])
    return run_cover.merge(mkts, on="run_name", how="left").sort_values("run_name").reset_index(drop=True)


def make_plots(summary: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
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
        plt.title("全历史CLOB策略 Top 期末本金")
        plt.tight_layout()
        p = fig_dir / "clob_top_endings.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
        robust = summary.sort_values("robustness_score", ascending=False).head(20)
        labels = robust["strategy"] + "|" + robust["sizing"]
        plt.figure(figsize=(14, 5))
        plt.bar(labels, robust["robustness_score"])
        plt.xticks(rotation=70, ha="right")
        plt.title("全历史CLOB策略 Top 稳健得分")
        plt.tight_layout()
        p = fig_dir / "clob_top_robustness.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
    return paths


def markdown_table(df: pd.DataFrame, rows: int = 30) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def build_report(coverage: pd.DataFrame, used_features: pd.DataFrame, summary: pd.DataFrame, finalists_run: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# monthly_runs 全历史 CLOB 数据：系统性策略研究")
    lines.append("")
    lines.append("这份报告只使用 `data/monthly_runs/*` 里已经从 Polymarket CLOB 拉下来的真实事件数据，不依赖额外外部数据。")
    lines.append("")
    lines.append("## 数据覆盖")
    lines.append("")
    lines.append(markdown_table(coverage, rows=100))
    lines.append("")
    lines.append("## 这版明确用到了哪些 CLOB / 流动性指标")
    lines.append("")
    lines.append(markdown_table(used_features, rows=100))
    lines.append("")
    lines.append("## 收益最高 Top 20")
    lines.append("")
    lines.append(markdown_table(summary.sort_values("ending_bankroll", ascending=False), rows=20))
    lines.append("")
    lines.append("## 稳健性最好 Top 20")
    lines.append("")
    lines.append(markdown_table(summary.sort_values("robustness_score", ascending=False), rows=20))
    lines.append("")
    if not summary.empty:
        best_end = summary.sort_values("ending_bankroll", ascending=False).iloc[0]
        best_rob = summary.sort_values("robustness_score", ascending=False).iloc[0]
        lines.append("## 两种“最佳”")
        lines.append("")
        lines.append(f"- 收益最高：**{best_end['strategy']} | {best_end['sizing']}**，期末本金 **{best_end['ending_bankroll']:.2f} USD**，最大回撤 **{best_end['max_drawdown']:.2%}**。")
        lines.append(f"- 稳健性最好：**{best_rob['strategy']} | {best_rob['sizing']}**，稳健得分 **{best_rob['robustness_score']:.4f}**，期末本金 **{best_rob['ending_bankroll']:.2f} USD**，最大回撤 **{best_rob['max_drawdown']:.2%}**。")
        lines.append("")
    lines.append("## finalist 按 run 的表现")
    lines.append("")
    lines.append(markdown_table(finalists_run, rows=100))
    lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![全历史CLOB策略 Top 期末本金](all_monthly_clob_figures/clob_top_endings.png)")
    lines.append("")
    lines.append("![全历史CLOB策略 Top 稳健得分](all_monthly_clob_figures/clob_top_robustness.png)")
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
    features = build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable event features were built from monthly_runs")
    features["pred_prob_up_logit"] = rolling_logit(features)
    coverage = coverage_table(features, run_cover)
    used_features = used_feature_catalog()

    logs = []
    for strategy in STRATEGIES:
        for frac in FIXED_FRACS:
            lg = simulate_strategy(features, strategy, frac, args.fee)
            if not lg.empty:
                logs.append(lg)
    logs_df = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()
    summary = summarize_logs(logs_df)
    if summary.empty:
        raise RuntimeError("No strategy produced any trades on all-monthly CLOB research")
    top_end = summary.sort_values("ending_bankroll", ascending=False).head(10)
    top_rob = summary.sort_values("robustness_score", ascending=False).head(10)
    finalists = pd.concat([top_end, top_rob], ignore_index=True).drop_duplicates(subset=["strategy", "sizing"])
    finalists_run = per_run_breakdown(logs_df, finalists)

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "all_monthly_clob_figures"
    report_dir.mkdir(parents=True, exist_ok=True)
    figs = make_plots(summary, fig_dir)

    coverage.to_csv(report_dir / "all_monthly_clob_run_coverage.csv", index=False)
    used_features.to_csv(report_dir / "all_monthly_clob_used_features.csv", index=False)
    summary.to_csv(report_dir / "all_monthly_clob_strategy_summary.csv", index=False)
    finalists_run.to_csv(report_dir / "all_monthly_clob_finalists_run_breakdown.csv", index=False)
    (report_dir / "all_monthly_clob_strategy_report.md").write_text(build_report(coverage, used_features, summary, finalists_run), encoding="utf-8")
    (report_dir / "all_monthly_clob_strategy_summary.json").write_text(json.dumps({"rows_features": int(len(features)), "rows_summary": int(len(summary)), "rows_logs": int(len(logs_df)), "rows_coverage": int(len(coverage)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_features": int(len(features)), "rows_summary": int(len(summary)), "rows_logs": int(len(logs_df)), "rows_coverage": int(len(coverage)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

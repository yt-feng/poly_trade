from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss

RUN_ID = "24869603988_attempt1"
FILE_GLOB = "*btc-updown-5m_quotes.csv"
NUMERIC_COLUMNS = [
    "buy_up_cents","buy_down_cents","sell_up_cents","sell_down_cents",
    "buy_up_size","buy_down_size","sell_up_size","sell_down_size",
    "mid_up_cents","mid_down_cents","spread_up_cents","spread_down_cents",
    "bid_depth_up_5","ask_depth_up_5","bid_depth_down_5","ask_depth_down_5",
    "level_count_bid_up","level_count_ask_up","level_count_bid_down","level_count_ask_down",
    "target_price","final_price","trade_count_1s","trade_volume_1s",
]
MODEL_FEATURES = [
    "btc_move_2m","btc_return_bps_2m","mid_up_prob_open","mid_up_prob_2m",
    "mid_up_prob_change_2m","spread_up_median_first2m","spread_down_median_first2m",
    "overround_median_first2m","trade_count_sum_first2m","trade_volume_sum_first2m",
    "quote_count_first2m",
]
MOVE_BINS = [-10000, -100, -50, -30, -10, 10, 30, 50, 100, 10000]
MOVE_LABELS = ["<=-100", "-100~-50", "-50~-30", "-30~-10", "-10~10", "10~30", "30~50", "50~100", ">=100"]
PROB_BINS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
PROB_LABELS = ["0.0~0.2", "0.2~0.4", "0.4~0.6", "0.6~0.8", "0.8~1.0"]


def first_non_null(series: pd.Series):
    non_null = series.dropna()
    return np.nan if non_null.empty else non_null.iloc[0]


def last_non_null(series: pd.Series):
    non_null = series.dropna()
    return np.nan if non_null.empty else non_null.iloc[-1]


def read_source_files(source_dir: Path) -> tuple[pd.DataFrame, List[Path]]:
    files = sorted(source_dir.glob(FILE_GLOB))
    if not files:
        raise FileNotFoundError(f"No files matching {FILE_GLOB} under {source_dir}")
    frames: List[pd.DataFrame] = []
    for path in files:
        frame = pd.read_csv(path)
        frame["source_file"] = path.name
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    combined.columns = [str(c).strip() for c in combined.columns]
    return combined, files


def to_numeric_inplace(df: pd.DataFrame, columns: Iterable[str]) -> None:
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")


def parse_close_ts_from_slug(slug: str) -> pd.Timestamp:
    m = re.search(r"(\d{10})$", str(slug))
    if not m:
        return pd.NaT
    return pd.to_datetime(int(m.group(1)), unit="s", utc=True)


def prepare_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    quotes = raw.copy()
    quotes["ts_utc"] = pd.to_datetime(quotes["ts_iso"], errors="coerce", utc=True)
    to_numeric_inplace(quotes, NUMERIC_COLUMNS)
    if {"mid_up_cents", "buy_up_cents", "sell_up_cents"}.issubset(quotes.columns):
        quotes["mid_up_cents"] = quotes["mid_up_cents"].fillna((quotes["buy_up_cents"] + quotes["sell_up_cents"]) / 2.0)
    if {"mid_down_cents", "buy_down_cents", "sell_down_cents"}.issubset(quotes.columns):
        quotes["mid_down_cents"] = quotes["mid_down_cents"].fillna((quotes["buy_down_cents"] + quotes["sell_down_cents"]) / 2.0)
    quotes["close_ts_utc"] = quotes["slug"].map(parse_close_ts_from_slug)
    quotes["mid_sum_cents"] = quotes[["mid_up_cents", "mid_down_cents"]].sum(axis=1, min_count=2)
    quotes["mid_overround_cents"] = quotes["mid_sum_cents"] - 100.0
    quotes["mid_up_prob"] = quotes["mid_up_cents"] / 100.0
    quotes["mid_down_prob"] = quotes["mid_down_cents"] / 100.0
    quotes["btc_move_from_target"] = quotes["final_price"] - quotes["target_price"]
    quotes = quotes.sort_values(["slug", "ts_utc", "source_file"]).drop_duplicates(subset=["ts_iso", "slug"], keep="last")
    return quotes.reset_index(drop=True)


def build_markets(quotes: pd.DataFrame) -> pd.DataFrame:
    grouped = quotes.groupby("slug", as_index=False, dropna=False)
    markets = grouped.agg(
        market_url=("market_url", first_non_null),
        window_text=("window_text", first_non_null),
        first_quote_ts=("ts_utc", "min"),
        last_quote_ts=("ts_utc", "max"),
        close_ts_utc=("close_ts_utc", first_non_null),
        quote_count=("ts_utc", "size"),
        target_price=("target_price", first_non_null),
        final_price_last=("final_price", last_non_null),
    )
    markets["can_resolve_outcome"] = markets["target_price"].notna() & markets["final_price_last"].notna()
    markets["outcome_up"] = np.where(markets["can_resolve_outcome"], (markets["final_price_last"] > markets["target_price"]).astype(float), np.nan)
    return markets.sort_values("first_quote_ts").reset_index(drop=True)


def build_first2m_features(quotes: pd.DataFrame, fee: float) -> pd.DataFrame:
    rows: List[Dict[str, float]] = []
    for slug, g in quotes.groupby("slug", dropna=False):
        g = g.sort_values("ts_utc").copy()
        if g.empty:
            continue
        first_ts = g["ts_utc"].min()
        cutoff = first_ts + pd.Timedelta(minutes=2)
        first2 = g[g["ts_utc"] <= cutoff].copy()
        if first2.empty:
            continue
        first_row = first2.iloc[0]
        last_row = first2.iloc[-1]
        target_price = first_non_null(g["target_price"])
        final_price = last_non_null(g["final_price"])
        outcome_up = np.nan if pd.isna(target_price) or pd.isna(final_price) else float(final_price > target_price)
        btc_move_2m = np.nan if pd.isna(target_price) or pd.isna(last_row.get("final_price")) else float(last_row["final_price"] - target_price)
        mid_up_prob_open = pd.to_numeric(first_row.get("mid_up_prob"), errors="coerce")
        mid_up_prob_2m = pd.to_numeric(last_row.get("mid_up_prob"), errors="coerce")
        if pd.isna(mid_up_prob_open) and pd.notna(first_row.get("mid_up_cents")):
            mid_up_prob_open = float(first_row["mid_up_cents"]) / 100.0
        if pd.isna(mid_up_prob_2m) and pd.notna(last_row.get("mid_up_cents")):
            mid_up_prob_2m = float(last_row["mid_up_cents"]) / 100.0
        realized_pnl_buy_up = np.nan
        realized_pnl_buy_down = np.nan
        if pd.notna(outcome_up) and pd.notna(mid_up_prob_2m):
            realized_pnl_buy_up = float(outcome_up - mid_up_prob_2m - fee)
            realized_pnl_buy_down = float((1.0 - outcome_up) - (1.0 - mid_up_prob_2m) - fee)
        rows.append({
            "slug": slug,
            "window_text": first_non_null(g["window_text"]),
            "first_quote_ts": first_ts,
            "close_ts_utc": first_non_null(g["close_ts_utc"]),
            "quote_count_total": int(len(g)),
            "quote_count_first2m": int(len(first2)),
            "target_price": target_price,
            "price_after_2m": last_non_null(first2["final_price"]),
            "final_price_last": final_price,
            "btc_move_2m": btc_move_2m,
            "btc_return_bps_2m": np.nan if pd.isna(target_price) or target_price == 0 or pd.isna(btc_move_2m) else float(btc_move_2m / target_price * 10000.0),
            "mid_up_prob_open": mid_up_prob_open,
            "mid_up_prob_2m": mid_up_prob_2m,
            "mid_up_prob_change_2m": np.nan if pd.isna(mid_up_prob_open) or pd.isna(mid_up_prob_2m) else float(mid_up_prob_2m - mid_up_prob_open),
            "spread_up_median_first2m": pd.to_numeric(first2["spread_up_cents"], errors="coerce").median(),
            "spread_down_median_first2m": pd.to_numeric(first2["spread_down_cents"], errors="coerce").median(),
            "overround_median_first2m": pd.to_numeric(first2["mid_overround_cents"], errors="coerce").median(),
            "trade_count_sum_first2m": pd.to_numeric(first2["trade_count_1s"], errors="coerce").sum(min_count=1),
            "trade_volume_sum_first2m": pd.to_numeric(first2["trade_volume_1s"], errors="coerce").sum(min_count=1),
            "outcome_up": outcome_up,
            "realized_pnl_buy_up_from_2m": realized_pnl_buy_up,
            "realized_pnl_buy_down_from_2m": realized_pnl_buy_down,
        })
    features = pd.DataFrame(rows)
    return features.sort_values("first_quote_ts").reset_index(drop=True) if not features.empty else features


def make_trade_pnl(entry_prob: pd.Series, outcome_up: pd.Series, side: str, fee: float) -> pd.Series:
    if side == "buy_up":
        return outcome_up - entry_prob - fee
    return (1.0 - outcome_up) - (1.0 - entry_prob) - fee


def move_bucket_table(features: pd.DataFrame) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["btc_move_2m"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()
    usable["move_bucket"] = pd.cut(usable["btc_move_2m"], bins=MOVE_BINS, labels=MOVE_LABELS, include_lowest=True)
    out = usable.groupby("move_bucket", as_index=False, observed=False).agg(
        count=("outcome_up", "size"),
        avg_btc_move_2m=("btc_move_2m", "mean"),
        avg_entry_prob=("mid_up_prob_2m", "mean"),
        realized_up_rate=("outcome_up", "mean"),
        avg_pnl_buy_up=("realized_pnl_buy_up_from_2m", "mean"),
        avg_pnl_buy_down=("realized_pnl_buy_down_from_2m", "mean"),
    )
    out["edge"] = out["realized_up_rate"] - out["avg_entry_prob"]
    out["best_side"] = np.where(out["avg_pnl_buy_up"] >= out["avg_pnl_buy_down"], "buy_up", "buy_down")
    out["best_avg_pnl"] = out[["avg_pnl_buy_up", "avg_pnl_buy_down"]].max(axis=1)
    return out


def calibration_table(features: pd.DataFrame) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["mid_up_prob_2m"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()
    usable["prob_bin"] = pd.cut(usable["mid_up_prob_2m"], bins=np.linspace(0, 1, 11), include_lowest=True, duplicates="drop")
    out = usable.groupby("prob_bin", as_index=False, observed=False).agg(
        count=("outcome_up", "size"),
        avg_entry_prob=("mid_up_prob_2m", "mean"),
        realized_up_rate=("outcome_up", "mean"),
        avg_pnl_buy_up=("realized_pnl_buy_up_from_2m", "mean"),
        avg_pnl_buy_down=("realized_pnl_buy_down_from_2m", "mean"),
    )
    out["edge"] = out["realized_up_rate"] - out["avg_entry_prob"]
    out["best_side"] = np.where(out["avg_pnl_buy_up"] >= out["avg_pnl_buy_down"], "buy_up", "buy_down")
    out["best_avg_pnl"] = out[["avg_pnl_buy_up", "avg_pnl_buy_down"]].max(axis=1)
    return out


def build_edge_surface(features: pd.DataFrame) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["btc_move_2m"].notna() & features["mid_up_prob_2m"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()
    usable["move_bucket"] = pd.cut(usable["btc_move_2m"], bins=MOVE_BINS, labels=MOVE_LABELS, include_lowest=True)
    usable["prob_bucket"] = pd.cut(usable["mid_up_prob_2m"], bins=PROB_BINS, labels=PROB_LABELS, include_lowest=True)

    def agg(g: pd.DataFrame) -> pd.Series:
        return pd.Series({
            "count": int(len(g)),
            "avg_btc_move_2m": float(g["btc_move_2m"].mean()),
            "avg_entry_prob": float(g["mid_up_prob_2m"].mean()),
            "realized_up_rate": float(g["outcome_up"].mean()),
            "avg_pnl_buy_up": float(g["realized_pnl_buy_up_from_2m"].mean()),
            "avg_pnl_buy_down": float(g["realized_pnl_buy_down_from_2m"].mean()),
        })

    out = usable.groupby(["move_bucket", "prob_bucket"], observed=False).apply(agg).reset_index()
    out["edge"] = out["realized_up_rate"] - out["avg_entry_prob"]
    out["best_side"] = np.where(out["avg_pnl_buy_up"] >= out["avg_pnl_buy_down"], "buy_up", "buy_down")
    out["best_avg_pnl"] = out[["avg_pnl_buy_up", "avg_pnl_buy_down"]].max(axis=1)
    return out.sort_values(["move_bucket", "prob_bucket"]).reset_index(drop=True)


def build_threshold_trade_logs(features: pd.DataFrame, fee: float) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["mid_up_prob_2m"].notna() & features["btc_move_2m"].notna()].copy()
    rows: List[pd.DataFrame] = []
    for th in [10, 20, 30, 40, 50, 75, 100]:
        defs = [
            ("momentum_buy_up_after_rise", usable["btc_move_2m"] >= th, "buy_up"),
            ("meanrev_buy_down_after_rise", usable["btc_move_2m"] >= th, "buy_down"),
            ("momentum_buy_down_after_drop", usable["btc_move_2m"] <= -th, "buy_down"),
            ("meanrev_buy_up_after_drop", usable["btc_move_2m"] <= -th, "buy_up"),
        ]
        for name, mask, side in defs:
            subset = usable[mask].copy()
            if subset.empty:
                continue
            subset["strategy"] = name
            subset["threshold_usd"] = th
            subset["side"] = side
            subset["pnl"] = make_trade_pnl(subset["mid_up_prob_2m"], subset["outcome_up"], side, fee)
            rows.append(subset[["slug","first_quote_ts","strategy","threshold_usd","side","btc_move_2m","mid_up_prob_2m","outcome_up","pnl"]])
    return pd.concat(rows, ignore_index=True).sort_values(["strategy","threshold_usd","first_quote_ts"]).reset_index(drop=True) if rows else pd.DataFrame()


def summarize_logs(df: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.groupby(group_cols, as_index=False).agg(
        trades=("slug", "size"),
        avg_btc_move_2m=("btc_move_2m", "mean"),
        avg_entry_prob=("mid_up_prob_2m", "mean"),
        win_rate=("pnl", lambda s: float((s > 0).mean())),
        avg_pnl=("pnl", "mean"),
        cum_pnl=("pnl", "sum"),
    )
    return out.sort_values(["cum_pnl","avg_pnl"], ascending=False).reset_index(drop=True)


def search_interval_strategies(features: pd.DataFrame, fee: float) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["btc_move_2m"].notna() & features["mid_up_prob_2m"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()
    move_ranges = [(-10000,-100),(-100,-50),(-50,-30),(-30,-10),(-10,10),(10,30),(30,50),(50,100),(100,10000)]
    prob_ranges = [(0.0,1.0),(0.0,0.2),(0.2,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.0)]
    rows = []
    for move_low, move_high in move_ranges:
        for prob_low, prob_high in prob_ranges:
            subset = usable[(usable["btc_move_2m"] > move_low) & (usable["btc_move_2m"] <= move_high) & (usable["mid_up_prob_2m"] >= prob_low) & (usable["mid_up_prob_2m"] <= prob_high)].copy()
            if len(subset) < 5:
                continue
            for side in ["buy_up", "buy_down"]:
                pnl = make_trade_pnl(subset["mid_up_prob_2m"], subset["outcome_up"], side, fee)
                rows.append({
                    "strategy_name": f"{side}|move({move_low},{move_high}]|prob[{prob_low},{prob_high}]",
                    "side": side,
                    "move_low": move_low,
                    "move_high": move_high,
                    "prob_low": prob_low,
                    "prob_high": prob_high,
                    "trades": int(len(subset)),
                    "avg_btc_move_2m": float(subset["btc_move_2m"].mean()),
                    "avg_entry_prob": float(subset["mid_up_prob_2m"].mean()),
                    "win_rate": float((pnl > 0).mean()),
                    "avg_pnl": float(pnl.mean()),
                    "cum_pnl": float(pnl.sum()),
                })
    return pd.DataFrame(rows).sort_values(["cum_pnl","avg_pnl","trades"], ascending=[False,False,False]).reset_index(drop=True) if rows else pd.DataFrame()


def train_test_split_time(features: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    usable = features[features["outcome_up"].notna()].copy().sort_values("first_quote_ts")
    if len(usable) < 20:
        return usable.iloc[:0].copy(), usable.copy()
    split_idx = max(int(len(usable) * 0.7), 1)
    split_idx = min(split_idx, len(usable) - 1)
    return usable.iloc[:split_idx].copy(), usable.iloc[split_idx:].copy()


def fill_with_train_medians(train: pd.DataFrame, test: pd.DataFrame, cols: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    med = train[cols].median(numeric_only=True)
    return train[cols].fillna(med), test[cols].fillna(med)


def safe_log_loss(y_true: np.ndarray, probs: np.ndarray) -> float:
    try:
        return float(log_loss(y_true, np.clip(probs, 1e-6, 1 - 1e-6), labels=[0,1]))
    except Exception:
        return float("nan")


def evaluate_prob_model(name: str, probs: np.ndarray, test_df: pd.DataFrame, fee: float) -> Tuple[Dict[str, float], pd.DataFrame]:
    y = test_df["outcome_up"].astype(float).to_numpy()
    entry = test_df["mid_up_prob_2m"].astype(float).to_numpy()
    decisions = np.where(probs > entry + fee, "buy_up", np.where(probs < entry - fee, "buy_down", "skip"))
    pnl = np.full(len(test_df), np.nan)
    bu = decisions == "buy_up"
    bd = decisions == "buy_down"
    pnl[bu] = y[bu] - entry[bu] - fee
    pnl[bd] = (1.0 - y[bd]) - (1.0 - entry[bd]) - fee
    traded = ~np.isnan(pnl)
    result = {
        "model": name,
        "test_rows": int(len(test_df)),
        "accuracy": float(accuracy_score(y, probs >= 0.5)),
        "brier": float(brier_score_loss(y, probs)),
        "log_loss": safe_log_loss(y, probs),
        "trades": int(traded.sum()),
        "trade_ratio": float(traded.mean()),
        "avg_pnl": float(np.nanmean(pnl)) if traded.any() else np.nan,
        "cum_pnl": float(np.nansum(pnl)) if traded.any() else np.nan,
        "win_rate": float(np.nanmean(pnl[traded] > 0)) if traded.any() else np.nan,
    }
    log_df = test_df[["slug","first_quote_ts","btc_move_2m","mid_up_prob_2m","outcome_up"]].copy()
    log_df["model"] = name
    log_df["pred_prob_up"] = probs
    log_df["decision"] = decisions
    log_df["pnl"] = pnl
    return result, log_df


def model_comparison(features: pd.DataFrame, fee: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    train_df, test_df = train_test_split_time(features)
    if train_df.empty or test_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    X_train, X_test = fill_with_train_medians(train_df, test_df, MODEL_FEATURES)
    y_train = train_df["outcome_up"].astype(int)
    rows: List[Dict[str, float]] = []
    logs: List[pd.DataFrame] = []
    base_prob = float(y_train.mean())
    r, lg = evaluate_prob_model("baseline_train_up_rate", np.full(len(test_df), base_prob), test_df, fee)
    rows.append(r); logs.append(lg)
    try:
        lr = LogisticRegression(max_iter=1000)
        lr.fit(X_train, y_train)
        r, lg = evaluate_prob_model("logistic_regression", lr.predict_proba(X_test)[:,1], test_df, fee)
        rows.append(r); logs.append(lg)
    except Exception:
        pass
    try:
        rf = RandomForestClassifier(n_estimators=300, random_state=42, min_samples_leaf=3)
        rf.fit(X_train, y_train)
        r, lg = evaluate_prob_model("random_forest", rf.predict_proba(X_test)[:,1], test_df, fee)
        rows.append(r); logs.append(lg)
    except Exception:
        pass
    return pd.DataFrame(rows).sort_values("cum_pnl", ascending=False).reset_index(drop=True), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def missingness_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"column": df.columns, "missing_ratio": [df[c].isna().mean() for c in df.columns], "non_null": [int(df[c].notna().sum()) for c in df.columns]}).sort_values("missing_ratio", ascending=False).reset_index(drop=True)


def markdown_table(df: pd.DataFrame, rows: int = 20) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def maybe_make_plots(features: pd.DataFrame, move_table: pd.DataFrame, calibration: pd.DataFrame, edge_surface: pd.DataFrame, threshold_summary: pd.DataFrame, threshold_logs: pd.DataFrame, interval_strategies: pd.DataFrame, model_table: pd.DataFrame, model_logs: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not features.empty and features["btc_move_2m"].notna().any():
        plt.figure(figsize=(8,4)); plt.hist(features["btc_move_2m"].dropna(), bins=30); plt.title("前2分钟BTC涨跌幅分布（美元）"); plt.tight_layout(); p=fig_dir/"btc_move_2m_hist.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not features.empty and features["mid_up_prob_2m"].notna().any():
        plt.figure(figsize=(8,4)); plt.hist(features["mid_up_prob_2m"].dropna(), bins=20); plt.title("2分钟盘口Up概率分布"); plt.tight_layout(); p=fig_dir/"entry_prob_hist.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    usable = features[features["btc_move_2m"].notna() & features["mid_up_prob_2m"].notna()].copy()
    if not usable.empty:
        plt.figure(figsize=(8,5)); plt.scatter(usable["btc_move_2m"], usable["mid_up_prob_2m"], s=10); plt.title("前2分钟BTC涨跌幅 vs 2分钟盘口Up概率"); plt.xlabel("BTC前2分钟涨跌幅（美元）"); plt.ylabel("2分钟盘口Up概率"); plt.tight_layout(); p=fig_dir/"btc_move_vs_entry_prob.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not move_table.empty:
        plt.figure(figsize=(10,4)); plt.bar(move_table["move_bucket"].astype(str), move_table["realized_up_rate"]); plt.xticks(rotation=45, ha="right"); plt.title("按涨跌分桶的最终上涨率"); plt.tight_layout(); p=fig_dir/"up_rate_by_move_bucket.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
        plt.figure(figsize=(10,4)); plt.bar(move_table["move_bucket"].astype(str), move_table["best_avg_pnl"]); plt.xticks(rotation=45, ha="right"); plt.title("按涨跌分桶的最佳方向平均PnL"); plt.tight_layout(); p=fig_dir/"best_avg_pnl_by_move_bucket.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not calibration.empty:
        plt.figure(figsize=(8,4)); plt.plot(calibration["avg_entry_prob"], calibration["realized_up_rate"], marker="o"); plt.plot([0,1],[0,1]); plt.title("2分钟盘口概率校准图"); plt.xlabel("平均盘口Up概率"); plt.ylabel("实际最终Up比例"); plt.tight_layout(); p=fig_dir/"entry_prob_calibration.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not edge_surface.empty:
        pivot = edge_surface.pivot(index="move_bucket", columns="prob_bucket", values="best_avg_pnl").reindex(index=MOVE_LABELS, columns=PROB_LABELS)
        plt.figure(figsize=(8,5)); im=plt.imshow(pivot.to_numpy(dtype=float), aspect="auto"); plt.xticks(range(len(pivot.columns)), pivot.columns, rotation=45, ha="right"); plt.yticks(range(len(pivot.index)), pivot.index); plt.title("二维区域最佳方向平均PnL热力图"); plt.colorbar(im); plt.tight_layout(); p=fig_dir/"edge_surface_best_avg_pnl.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not threshold_summary.empty:
        top=threshold_summary.head(12); labels=top["strategy"]+"@"+top["threshold_usd"].astype(str); plt.figure(figsize=(10,5)); plt.bar(labels, top["cum_pnl"]); plt.xticks(rotation=45, ha="right"); plt.title("Top阈值策略累计PnL"); plt.tight_layout(); p=fig_dir/"top_threshold_rules_cum_pnl.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not interval_strategies.empty:
        top=interval_strategies.head(12); plt.figure(figsize=(10,5)); plt.bar(range(len(top)), top["cum_pnl"]); plt.xticks(range(len(top)), top["strategy_name"], rotation=60, ha="right"); plt.title("Top组合策略累计PnL"); plt.tight_layout(); p=fig_dir/"top_interval_strategies_cum_pnl.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not model_table.empty:
        plt.figure(figsize=(8,4)); plt.bar(model_table["model"], model_table["cum_pnl"]); plt.xticks(rotation=20, ha="right"); plt.title("模型累计PnL对比"); plt.tight_layout(); p=fig_dir/"model_cum_pnl.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not threshold_logs.empty:
        best=threshold_logs.groupby(["strategy","threshold_usd"], as_index=False)["pnl"].sum().sort_values("pnl", ascending=False).head(1)
        if not best.empty:
            strategy=best.iloc[0]["strategy"]; th=best.iloc[0]["threshold_usd"]; s=threshold_logs[(threshold_logs["strategy"]==strategy)&(threshold_logs["threshold_usd"]==th)].sort_values("first_quote_ts").copy(); s["cum_pnl"]=s["pnl"].cumsum(); plt.figure(figsize=(8,4)); plt.plot(s["first_quote_ts"], s["cum_pnl"]); plt.title(f"最佳阈值规则累计PnL：{strategy}@{th}"); plt.tight_layout(); p=fig_dir/"best_threshold_rule_cum_pnl.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    if not model_logs.empty:
        traded=model_logs[model_logs["decision"]!="skip"].copy()
        if not traded.empty:
            best_model=traded.groupby("model", as_index=False)["pnl"].sum().sort_values("pnl", ascending=False).iloc[0]["model"]; s=traded[traded["model"]==best_model].sort_values("first_quote_ts").copy(); s["cum_pnl"]=s["pnl"].cumsum(); plt.figure(figsize=(8,4)); plt.plot(s["first_quote_ts"], s["cum_pnl"]); plt.title(f"最佳模型累计PnL：{best_model}"); plt.tight_layout(); p=fig_dir/"best_model_cum_pnl.png"; plt.savefig(p,dpi=150); plt.close(); paths.append(str(p))
    return paths


def build_report(files: List[Path], quotes: pd.DataFrame, features: pd.DataFrame, move_table: pd.DataFrame, calibration: pd.DataFrame, edge_surface: pd.DataFrame, threshold_summary: pd.DataFrame, interval_strategies: pd.DataFrame, model_table: pd.DataFrame, fee: float) -> str:
    lines: List[str] = []
    lines.append(f"# 前2分钟信息 deep dive 研究报告：{RUN_ID}")
    lines.append("")
    lines.append("## 这版新增")
    lines.append("")
    lines.append("- 二维错价热力图：前2分钟BTC涨跌幅 × 2分钟盘口Up概率")
    lines.append("- 系统搜索组合策略：自动枚举区间并比较买Up / 买Down")
    lines.append("- 更多图表，直接看哪些区域真的更有 edge")
    lines.append("")
    lines.append("## 数据概览")
    lines.append("")
    lines.append(f"- 原始分块文件数：**{len(files)}**")
    lines.append(f"- 清洗后 quotes 行数：**{len(quotes)}**")
    lines.append(f"- 市场级特征行数：**{len(features)}**")
    lines.append(f"- 固定单笔成本 fee：**{fee:.4f}**")
    if not features.empty and features["outcome_up"].notna().any():
        lines.append(f"- 已解析 outcome 的样本 Up 比例：**{features.loc[features['outcome_up'].notna(), 'outcome_up'].mean():.4f}**")
    lines.append("")
    lines.append("## 关键直觉")
    lines.append("")
    lines.append("1. 前2分钟大跌后的 Down 动量，仍然是当前最值得优先观察的信号。")
    lines.append("2. 上涨后的信号并不单调，不是所有上涨区间都值得追。")
    lines.append("3. 比起单独看涨跌幅，更应该看“涨跌幅 + 盘口概率”这个二维区域。")
    lines.append("")
    lines.append("## 前2分钟特征样例")
    lines.append("")
    sample_cols = ["slug","window_text","btc_move_2m","mid_up_prob_2m","outcome_up","realized_pnl_buy_up_from_2m","realized_pnl_buy_down_from_2m"]
    lines.append(markdown_table(features[sample_cols] if not features.empty else pd.DataFrame(), rows=20))
    lines.append("")
    lines.append("## BTC前2分钟涨跌分桶结果")
    lines.append("")
    lines.append(markdown_table(move_table, rows=20))
    lines.append("")
    lines.append("## 2分钟盘口概率校准")
    lines.append("")
    lines.append(markdown_table(calibration, rows=20))
    lines.append("")
    lines.append("## 二维错价热力表")
    lines.append("")
    lines.append(markdown_table(edge_surface, rows=40))
    lines.append("")
    lines.append("这里的 `best_side` 表示在该二维区域里，历史上买 Up 还是买 Down 的平均PnL更高。")
    lines.append("")
    lines.append("## 常见阈值策略对比")
    lines.append("")
    lines.append(markdown_table(threshold_summary, rows=20))
    lines.append("")
    lines.append("## 系统搜索得到的Top组合策略")
    lines.append("")
    lines.append(markdown_table(interval_strategies, rows=30))
    lines.append("")
    lines.append("## 简单模型对比（时间顺序切分）")
    lines.append("")
    lines.append(markdown_table(model_table, rows=20) if not model_table.empty else "(empty)")
    lines.append("")
    lines.append("## 缺失值概览")
    lines.append("")
    lines.append(markdown_table(missingness_table(quotes), rows=15))
    lines.append("")
    image_items = [
        ("前2分钟BTC涨跌幅分布", "figures/btc_move_2m_hist.png"),
        ("2分钟盘口Up概率分布", "figures/entry_prob_hist.png"),
        ("前2分钟BTC涨跌幅 vs 2分钟盘口Up概率", "figures/btc_move_vs_entry_prob.png"),
        ("按涨跌分桶的最终上涨率", "figures/up_rate_by_move_bucket.png"),
        ("按涨跌分桶的最佳方向平均PnL", "figures/best_avg_pnl_by_move_bucket.png"),
        ("2分钟盘口概率校准图", "figures/entry_prob_calibration.png"),
        ("二维区域最佳方向平均PnL热力图", "figures/edge_surface_best_avg_pnl.png"),
        ("Top阈值策略累计PnL", "figures/top_threshold_rules_cum_pnl.png"),
        ("Top组合策略累计PnL", "figures/top_interval_strategies_cum_pnl.png"),
        ("模型累计PnL对比", "figures/model_cum_pnl.png"),
        ("最佳阈值规则累计PnL", "figures/best_threshold_rule_cum_pnl.png"),
        ("最佳模型累计PnL", "figures/best_model_cum_pnl.png"),
    ]
    lines.append("## 图表")
    lines.append("")
    for title, rel in image_items:
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"![{title}]({rel})")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=str, required=True)
    parser.add_argument("--cleaned-dir", type=str, default="data/cleaned/run_24869603988_attempt1")
    parser.add_argument("--features-dir", type=str, default="data/features/run_24869603988_attempt1")
    parser.add_argument("--report-dir", type=str, default="reports/run_24869603988_attempt1_predictive")
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()
    source_dir = Path(args.source_dir); cleaned_dir = Path(args.cleaned_dir); features_dir = Path(args.features_dir); report_dir = Path(args.report_dir); fig_dir = report_dir / "figures"
    cleaned_dir.mkdir(parents=True, exist_ok=True); features_dir.mkdir(parents=True, exist_ok=True); report_dir.mkdir(parents=True, exist_ok=True)
    raw, files = read_source_files(source_dir)
    quotes = prepare_quotes(raw)
    markets = build_markets(quotes)
    features = build_first2m_features(quotes, fee=args.fee)
    move_table = move_bucket_table(features)
    calibration = calibration_table(features)
    edge_surface = build_edge_surface(features)
    threshold_logs = build_threshold_trade_logs(features, fee=args.fee)
    threshold_summary = summarize_logs(threshold_logs, ["strategy", "threshold_usd", "side"])
    interval_strategies = search_interval_strategies(features, fee=args.fee)
    model_table, model_logs = model_comparison(features, fee=args.fee)
    plots = maybe_make_plots(features, move_table, calibration, edge_surface, threshold_summary, threshold_logs, interval_strategies, model_table, model_logs, fig_dir)
    quotes.to_csv(cleaned_dir / "btc_updown_5m_quotes_clean.csv", index=False)
    markets.to_csv(cleaned_dir / "btc_updown_5m_markets_clean.csv", index=False)
    features.to_csv(features_dir / "market_features_first2m.csv", index=False)
    move_table.to_csv(report_dir / "move_bucket_summary.csv", index=False)
    calibration.to_csv(report_dir / "entry_prob_calibration.csv", index=False)
    edge_surface.to_csv(report_dir / "edge_surface.csv", index=False)
    threshold_logs.to_csv(report_dir / "threshold_trade_logs.csv", index=False)
    threshold_summary.to_csv(report_dir / "threshold_rule_comparison.csv", index=False)
    interval_strategies.to_csv(report_dir / "interval_strategy_search.csv", index=False)
    model_table.to_csv(report_dir / "model_comparison.csv", index=False)
    model_logs.to_csv(report_dir / "model_trade_logs.csv", index=False)
    (report_dir / "report.md").write_text(build_report(files, quotes, features, move_table, calibration, edge_surface, threshold_summary, interval_strategies, model_table, args.fee), encoding="utf-8")
    summary = {"run_id": RUN_ID, "source_file_count": len(files), "quote_rows": int(len(quotes)), "market_rows": int(len(markets)), "feature_rows": int(len(features)), "threshold_rule_rows": int(len(threshold_summary)), "interval_strategy_rows": int(len(interval_strategies)), "model_rows": int(len(model_table)), "figure_count": len(plots)}
    write_json(report_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

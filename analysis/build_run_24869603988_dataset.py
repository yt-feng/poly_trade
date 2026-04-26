from __future__ import annotations

import argparse
import json
import math
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
    "buy_up_cents",
    "buy_down_cents",
    "sell_up_cents",
    "sell_down_cents",
    "buy_up_size",
    "buy_down_size",
    "sell_up_size",
    "sell_down_size",
    "mid_up_cents",
    "mid_down_cents",
    "spread_up_cents",
    "spread_down_cents",
    "bid_depth_up_5",
    "ask_depth_up_5",
    "bid_depth_down_5",
    "ask_depth_down_5",
    "level_count_bid_up",
    "level_count_ask_up",
    "level_count_bid_down",
    "level_count_ask_down",
    "target_price",
    "final_price",
    "trade_count_1s",
    "trade_volume_1s",
]

MODEL_FEATURES = [
    "btc_move_2m",
    "btc_return_bps_2m",
    "mid_up_prob_open",
    "mid_up_prob_2m",
    "mid_up_prob_change_2m",
    "spread_up_median_first2m",
    "spread_down_median_first2m",
    "overround_median_first2m",
    "trade_count_sum_first2m",
    "trade_volume_sum_first2m",
    "quote_count_first2m",
]


def first_non_null(series: pd.Series):
    non_null = series.dropna()
    if non_null.empty:
        return np.nan
    return non_null.iloc[0]


def last_non_null(series: pd.Series):
    non_null = series.dropna()
    if non_null.empty:
        return np.nan
    return non_null.iloc[-1]


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


def bucket_seconds_to_close(seconds: float) -> str:
    if pd.isna(seconds):
        return "unknown"
    value = float(seconds)
    if value < 0:
        return "after_close"
    if value <= 30:
        return "00_30s"
    if value <= 60:
        return "30_60s"
    if value <= 120:
        return "01_02m"
    if value <= 180:
        return "02_03m"
    if value <= 240:
        return "03_04m"
    if value <= 300:
        return "04_05m"
    return "gt_05m"


def prepare_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    quotes = raw.copy()
    quotes["ts_utc"] = pd.to_datetime(quotes["ts_iso"], errors="coerce", utc=True)
    to_numeric_inplace(quotes, NUMERIC_COLUMNS)

    if {"mid_up_cents", "buy_up_cents", "sell_up_cents"}.issubset(quotes.columns):
        quotes["mid_up_cents"] = quotes["mid_up_cents"].fillna((quotes["buy_up_cents"] + quotes["sell_up_cents"]) / 2.0)
    if {"mid_down_cents", "buy_down_cents", "sell_down_cents"}.issubset(quotes.columns):
        quotes["mid_down_cents"] = quotes["mid_down_cents"].fillna((quotes["buy_down_cents"] + quotes["sell_down_cents"]) / 2.0)

    quotes["close_ts_utc"] = quotes["slug"].map(parse_close_ts_from_slug)
    quotes["seconds_to_close"] = (quotes["close_ts_utc"] - quotes["ts_utc"]).dt.total_seconds()
    quotes["time_to_close_bucket"] = quotes["seconds_to_close"].map(bucket_seconds_to_close)
    quotes["mid_sum_cents"] = quotes[["mid_up_cents", "mid_down_cents"]].sum(axis=1, min_count=2)
    quotes["mid_overround_cents"] = quotes["mid_sum_cents"] - 100.0
    quotes["mid_up_prob"] = quotes["mid_up_cents"] / 100.0
    quotes["mid_down_prob"] = quotes["mid_down_cents"] / 100.0
    quotes["btc_move_from_target"] = quotes["final_price"] - quotes["target_price"]
    quotes["book_complete"] = (
        quotes["buy_up_cents"].notna()
        & quotes["buy_down_cents"].notna()
        & quotes["sell_up_cents"].notna()
        & quotes["sell_down_cents"].notna()
    )

    quotes = quotes.sort_values(["slug", "ts_utc", "source_file"]).drop_duplicates(subset=["ts_iso", "slug"], keep="last")
    return quotes.reset_index(drop=True)


def build_markets(quotes: pd.DataFrame) -> pd.DataFrame:
    grouped = quotes.groupby("slug", dropna=False, as_index=False)
    markets = grouped.agg(
        market_url=("market_url", first_non_null),
        window_text=("window_text", first_non_null),
        first_quote_ts=("ts_utc", "min"),
        last_quote_ts=("ts_utc", "max"),
        close_ts_utc=("close_ts_utc", first_non_null),
        quote_count=("ts_utc", "size"),
        source_files=("source_file", lambda s: ", ".join(sorted(set(map(str, s.dropna()))))),
        target_price=("target_price", first_non_null),
        final_price_first=("final_price", first_non_null),
        final_price_last=("final_price", last_non_null),
        mid_up_open=("mid_up_cents", first_non_null),
        mid_up_last=("mid_up_cents", last_non_null),
        mid_up_low=("mid_up_cents", "min"),
        mid_up_high=("mid_up_cents", "max"),
        spread_up_median=("spread_up_cents", "median"),
        spread_down_median=("spread_down_cents", "median"),
        overround_median=("mid_overround_cents", "median"),
    )
    markets["can_resolve_outcome"] = markets["target_price"].notna() & markets["final_price_last"].notna()
    markets["outcome_up"] = np.where(
        markets["can_resolve_outcome"],
        (markets["final_price_last"] > markets["target_price"]).astype(float),
        np.nan,
    )
    markets["price_move_usd"] = markets["final_price_last"] - markets["target_price"]
    markets["mid_up_range"] = markets["mid_up_high"] - markets["mid_up_low"]
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
        outcome_up = np.nan
        if pd.notna(target_price) and pd.notna(final_price):
            outcome_up = float(final_price > target_price)
        btc_move_2m = np.nan
        if pd.notna(last_row.get("final_price")) and pd.notna(target_price):
            btc_move_2m = float(last_row["final_price"] - target_price)
        mid_up_prob_open = pd.to_numeric(first_row.get("mid_up_prob"), errors="coerce")
        mid_up_prob_2m = pd.to_numeric(last_row.get("mid_up_prob"), errors="coerce")
        if pd.isna(mid_up_prob_open) and pd.notna(first_row.get("mid_up_cents")):
            mid_up_prob_open = float(first_row["mid_up_cents"]) / 100.0
        if pd.isna(mid_up_prob_2m) and pd.notna(last_row.get("mid_up_cents")):
            mid_up_prob_2m = float(last_row["mid_up_cents"]) / 100.0
        realized_pnl_buy_up = np.nan
        if pd.notna(outcome_up) and pd.notna(mid_up_prob_2m):
            realized_pnl_buy_up = float(outcome_up - mid_up_prob_2m - fee)
        rows.append(
            {
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
            }
        )
    features = pd.DataFrame(rows)
    if not features.empty:
        features = features.sort_values("first_quote_ts").reset_index(drop=True)
    return features


def make_trade_pnl(entry_prob: pd.Series, outcome_up: pd.Series, side: str, fee: float) -> pd.Series:
    if side == "buy_up":
        return outcome_up - entry_prob - fee
    if side == "buy_down":
        return (1.0 - outcome_up) - (1.0 - entry_prob) - fee
    raise ValueError(f"unknown side: {side}")


def build_threshold_trade_logs(features: pd.DataFrame, fee: float) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["mid_up_prob_2m"].notna() & features["btc_move_2m"].notna()].copy()
    rows: List[Dict[str, float]] = []
    thresholds = [10, 20, 30, 40, 50, 75, 100]
    for th in thresholds:
        definitions = [
            ("momentum_buy_up_after_rise", usable["btc_move_2m"] >= th, "buy_up"),
            ("meanrev_buy_down_after_rise", usable["btc_move_2m"] >= th, "buy_down"),
            ("momentum_buy_down_after_drop", usable["btc_move_2m"] <= -th, "buy_down"),
            ("meanrev_buy_up_after_drop", usable["btc_move_2m"] <= -th, "buy_up"),
        ]
        for name, mask, side in definitions:
            subset = usable[mask].copy()
            if subset.empty:
                continue
            pnl = make_trade_pnl(subset["mid_up_prob_2m"], subset["outcome_up"], side=side, fee=fee)
            subset = subset.assign(strategy=name, threshold_usd=th, side=side, pnl=pnl)
            rows.append(subset[["slug", "first_quote_ts", "strategy", "threshold_usd", "side", "btc_move_2m", "mid_up_prob_2m", "outcome_up", "pnl"]])
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).sort_values(["strategy", "threshold_usd", "first_quote_ts"]).reset_index(drop=True)


def summarize_threshold_logs(trade_logs: pd.DataFrame) -> pd.DataFrame:
    if trade_logs.empty:
        return pd.DataFrame()
    grouped = trade_logs.groupby(["strategy", "threshold_usd", "side"], as_index=False).agg(
        trades=("slug", "size"),
        avg_btc_move_2m=("btc_move_2m", "mean"),
        avg_entry_prob=("mid_up_prob_2m", "mean"),
        win_rate=("pnl", lambda s: float((s > 0).mean())),
        avg_pnl=("pnl", "mean"),
        cum_pnl=("pnl", "sum"),
    )
    return grouped.sort_values(["cum_pnl", "avg_pnl"], ascending=False).reset_index(drop=True)


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
    )
    out["edge"] = out["realized_up_rate"] - out["avg_entry_prob"]
    return out


def move_bucket_table(features: pd.DataFrame) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["btc_move_2m"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()
    bins = [-10000, -100, -50, -30, -10, 10, 30, 50, 100, 10000]
    usable["move_bucket"] = pd.cut(usable["btc_move_2m"], bins=bins, include_lowest=True)
    out = usable.groupby("move_bucket", as_index=False, observed=False).agg(
        count=("outcome_up", "size"),
        avg_btc_move_2m=("btc_move_2m", "mean"),
        avg_entry_prob=("mid_up_prob_2m", "mean"),
        realized_up_rate=("outcome_up", "mean"),
        avg_pnl_buy_up=("realized_pnl_buy_up_from_2m", "mean"),
    )
    out["edge"] = out["realized_up_rate"] - out["avg_entry_prob"]
    return out


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


def evaluate_prob_model(name: str, probs: np.ndarray, test_df: pd.DataFrame, fee: float) -> Tuple[Dict[str, float], pd.DataFrame]:
    y = test_df["outcome_up"].astype(float).to_numpy()
    entry = test_df["mid_up_prob_2m"].astype(float).to_numpy()
    decisions = np.where(probs > entry + fee, "buy_up", np.where(probs < entry - fee, "buy_down", "skip"))
    pnl = np.full(len(test_df), np.nan)
    buy_up_mask = decisions == "buy_up"
    buy_down_mask = decisions == "buy_down"
    pnl[buy_up_mask] = y[buy_up_mask] - entry[buy_up_mask] - fee
    pnl[buy_down_mask] = (1.0 - y[buy_down_mask]) - (1.0 - entry[buy_down_mask]) - fee
    traded = ~np.isnan(pnl)
    result = {
        "model": name,
        "test_rows": int(len(test_df)),
        "accuracy": float(accuracy_score(y, probs >= 0.5)),
        "brier": float(brier_score_loss(y, probs)),
        "log_loss": float(log_loss(y, np.clip(probs, 1e-6, 1 - 1e-6))),
        "trades": int(traded.sum()),
        "trade_ratio": float(traded.mean()),
        "avg_pnl": float(np.nanmean(pnl)) if traded.any() else np.nan,
        "cum_pnl": float(np.nansum(pnl)) if traded.any() else np.nan,
        "win_rate": float(np.nanmean(pnl[traded] > 0)) if traded.any() else np.nan,
    }
    trade_log = test_df[["slug", "first_quote_ts", "btc_move_2m", "mid_up_prob_2m", "outcome_up"]].copy()
    trade_log["model"] = name
    trade_log["pred_prob_up"] = probs
    trade_log["decision"] = decisions
    trade_log["pnl"] = pnl
    return result, trade_log


def model_comparison(features: pd.DataFrame, fee: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    train_df, test_df = train_test_split_time(features)
    if train_df.empty or test_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    train_df = train_df.copy()
    test_df = test_df.copy()
    X_train, X_test = fill_with_train_medians(train_df, test_df, MODEL_FEATURES)
    y_train = train_df["outcome_up"].astype(int)

    rows: List[Dict[str, float]] = []
    logs: List[pd.DataFrame] = []

    baseline_prob = float(y_train.mean())
    base_probs = np.full(len(test_df), baseline_prob)
    r, log_df = evaluate_prob_model("baseline_train_up_rate", base_probs, test_df, fee)
    rows.append(r)
    logs.append(log_df)

    try:
        lr = LogisticRegression(max_iter=1000)
        lr.fit(X_train, y_train)
        probs = lr.predict_proba(X_test)[:, 1]
        r, log_df = evaluate_prob_model("logistic_regression", probs, test_df, fee)
        rows.append(r)
        logs.append(log_df)
    except Exception:
        pass

    try:
        rf = RandomForestClassifier(n_estimators=300, random_state=42, min_samples_leaf=3)
        rf.fit(X_train, y_train)
        probs = rf.predict_proba(X_test)[:, 1]
        r, log_df = evaluate_prob_model("random_forest", probs, test_df, fee)
        rows.append(r)
        logs.append(log_df)
    except Exception:
        pass

    return pd.DataFrame(rows).sort_values("cum_pnl", ascending=False).reset_index(drop=True), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def missingness_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "column": df.columns,
            "missing_ratio": [df[c].isna().mean() for c in df.columns],
            "non_null": [int(df[c].notna().sum()) for c in df.columns],
        }
    ).sort_values("missing_ratio", ascending=False).reset_index(drop=True)


def markdown_table(df: pd.DataFrame, rows: int = 20) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    numeric_cols = show.select_dtypes(include=[np.number]).columns
    show[numeric_cols] = show[numeric_cols].round(4)
    return show.to_markdown(index=False)


def write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def maybe_make_plots(
    features: pd.DataFrame,
    move_table: pd.DataFrame,
    calibration: pd.DataFrame,
    threshold_summary: pd.DataFrame,
    threshold_logs: pd.DataFrame,
    model_table: pd.DataFrame,
    model_logs: pd.DataFrame,
    fig_dir: Path,
) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths

    fig_dir.mkdir(parents=True, exist_ok=True)

    if not features.empty and features["btc_move_2m"].notna().any():
        plt.figure(figsize=(8, 4))
        plt.hist(features["btc_move_2m"].dropna(), bins=30)
        plt.title("BTC move during first 2 minutes (USD)")
        plt.tight_layout()
        p = fig_dir / "btc_move_2m_hist.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    if not features.empty and features["mid_up_prob_2m"].notna().any():
        plt.figure(figsize=(8, 4))
        plt.hist(features["mid_up_prob_2m"].dropna(), bins=20)
        plt.title("Entry probability at 2-minute mark")
        plt.tight_layout()
        p = fig_dir / "entry_prob_hist.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    usable = features[features["btc_move_2m"].notna() & features["mid_up_prob_2m"].notna()].copy()
    if not usable.empty:
        plt.figure(figsize=(8, 5))
        plt.scatter(usable["btc_move_2m"], usable["mid_up_prob_2m"], s=10)
        plt.title("First-2-minute BTC move vs entry probability")
        plt.xlabel("BTC move in first 2 minutes (USD)")
        plt.ylabel("Mid up probability at 2m")
        plt.tight_layout()
        p = fig_dir / "btc_move_vs_entry_prob.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    if not move_table.empty:
        plt.figure(figsize=(10, 4))
        labels = move_table["move_bucket"].astype(str)
        plt.bar(labels, move_table["realized_up_rate"])
        plt.xticks(rotation=45, ha="right")
        plt.title("Realized up rate by first-2-minute BTC move bucket")
        plt.tight_layout()
        p = fig_dir / "up_rate_by_move_bucket.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

        plt.figure(figsize=(10, 4))
        labels = move_table["move_bucket"].astype(str)
        plt.bar(labels, move_table["avg_pnl_buy_up"])
        plt.xticks(rotation=45, ha="right")
        plt.title("Average buy-up PnL by first-2-minute BTC move bucket")
        plt.tight_layout()
        p = fig_dir / "avg_buy_up_pnl_by_move_bucket.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    if not calibration.empty:
        plt.figure(figsize=(8, 4))
        plt.plot(calibration["avg_entry_prob"], calibration["realized_up_rate"], marker="o")
        plt.plot([0, 1], [0, 1])
        plt.title("Calibration of 2-minute entry probability")
        plt.xlabel("Average entry probability")
        plt.ylabel("Realized up rate")
        plt.tight_layout()
        p = fig_dir / "entry_prob_calibration.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    if not threshold_summary.empty:
        top = threshold_summary.head(12)
        labels = top["strategy"] + "@" + top["threshold_usd"].astype(str)
        plt.figure(figsize=(10, 5))
        plt.bar(labels, top["cum_pnl"])
        plt.xticks(rotation=45, ha="right")
        plt.title("Top threshold rules by cumulative PnL")
        plt.tight_layout()
        p = fig_dir / "top_threshold_rules_cum_pnl.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    if not model_table.empty:
        plt.figure(figsize=(8, 4))
        plt.bar(model_table["model"], model_table["cum_pnl"])
        plt.xticks(rotation=20, ha="right")
        plt.title("Model cumulative PnL on test split")
        plt.tight_layout()
        p = fig_dir / "model_cum_pnl.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    if not threshold_logs.empty:
        best_rule = threshold_logs.groupby(["strategy", "threshold_usd"], as_index=False)["pnl"].sum().sort_values("pnl", ascending=False).head(1)
        if not best_rule.empty:
            strategy = best_rule.iloc[0]["strategy"]
            threshold = best_rule.iloc[0]["threshold_usd"]
            s = threshold_logs[(threshold_logs["strategy"] == strategy) & (threshold_logs["threshold_usd"] == threshold)].sort_values("first_quote_ts").copy()
            s["cum_pnl"] = s["pnl"].cumsum()
            plt.figure(figsize=(8, 4))
            plt.plot(s["first_quote_ts"], s["cum_pnl"])
            plt.title(f"Cumulative PnL of best threshold rule: {strategy}@{threshold}")
            plt.tight_layout()
            p = fig_dir / "best_threshold_rule_cum_pnl.png"
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))

    if not model_logs.empty:
        traded = model_logs[model_logs["decision"] != "skip"].copy()
        if not traded.empty:
            best_model = traded.groupby("model", as_index=False)["pnl"].sum().sort_values("pnl", ascending=False).iloc[0]["model"]
            s = traded[traded["model"] == best_model].sort_values("first_quote_ts").copy()
            s["cum_pnl"] = s["pnl"].cumsum()
            plt.figure(figsize=(8, 4))
            plt.plot(s["first_quote_ts"], s["cum_pnl"])
            plt.title(f"Cumulative PnL of best model: {best_model}")
            plt.tight_layout()
            p = fig_dir / "best_model_cum_pnl.png"
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))

    return paths


def build_report(
    files: List[Path],
    quotes: pd.DataFrame,
    features: pd.DataFrame,
    move_table: pd.DataFrame,
    calibration: pd.DataFrame,
    threshold_summary: pd.DataFrame,
    model_table: pd.DataFrame,
    plots: List[str],
    fee: float,
) -> str:
    lines: List[str] = []
    lines.append(f"# 基于前2分钟信息的预测研究报告：{RUN_ID}")
    lines.append("")
    lines.append("## 研究问题")
    lines.append("")
    lines.append("我们关心的是：**只看前 2 分钟的 BTC 价格变化、盘口概率和交易活跃度，能否更好地预测后 3 分钟 / 最终 5 分钟 Up/Down 结果，并形成比盲目下单更优的策略？**")
    lines.append("")
    lines.append("## 数据概览")
    lines.append("")
    lines.append(f"- 原始分块文件数：**{len(files)}**")
    lines.append(f"- 清洗后 quotes 行数：**{len(quotes)}**")
    lines.append(f"- 市场级特征行数：**{len(features)}**")
    lines.append(f"- 评估使用的固定单笔成本 fee：**{fee:.4f}**")
    if not features.empty and features["outcome_up"].notna().any():
        lines.append(f"- 已解析 outcome 的样本 Up 比例：**{features.loc[features['outcome_up'].notna(), 'outcome_up'].mean():.4f}**")
    lines.append("")
    lines.append("## 直观理解")
    lines.append("")
    lines.append("这里的核心特征是 `btc_move_2m`：表示从该 5 分钟窗口起点到第 2 分钟时，BTC 价格一共偏离了多少美元。")
    lines.append("例如，如果前 2 分钟已经上涨了 30 美元，那么我们会进一步检验：")
    lines.append("")
    lines.append("- 这是否意味着最终更容易继续收涨（动量）")
    lines.append("- 还是说已经涨得过多，后面更容易回落（均值回归）")
    lines.append("")
    lines.append("## 前2分钟特征样例")
    lines.append("")
    sample_cols = ["slug", "window_text", "btc_move_2m", "mid_up_prob_2m", "outcome_up", "realized_pnl_buy_up_from_2m"]
    lines.append(markdown_table(features[sample_cols] if not features.empty else pd.DataFrame(), rows=20))
    lines.append("")
    lines.append("## BTC前2分钟涨跌幅分桶结果")
    lines.append("")
    lines.append(markdown_table(move_table, rows=20))
    lines.append("")
    lines.append("这里最值得看的是：")
    lines.append("")
    lines.append("- `realized_up_rate`：该分桶里最终收涨的实际比例")
    lines.append("- `avg_entry_prob`：2分钟时盘口给出的平均 Up 概率")
    lines.append("- `edge`：实际收涨率减去盘口概率，正值说明盘口低估了 Up，负值说明盘口高估了 Up")
    lines.append("")
    lines.append("## 2分钟时盘口概率的校准情况")
    lines.append("")
    lines.append(markdown_table(calibration, rows=20))
    lines.append("")
    lines.append("## 常见阈值策略对比")
    lines.append("")
    lines.append("这里测试了几类简单规则：")
    lines.append("")
    lines.append("- `momentum_buy_up_after_rise`：前2分钟上涨超过阈值后，继续买 Up")
    lines.append("- `meanrev_buy_down_after_rise`：前2分钟上涨超过阈值后，反手买 Down")
    lines.append("- `momentum_buy_down_after_drop`：前2分钟下跌超过阈值后，继续买 Down")
    lines.append("- `meanrev_buy_up_after_drop`：前2分钟下跌超过阈值后，反手买 Up")
    lines.append("")
    lines.append(markdown_table(threshold_summary, rows=30))
    lines.append("")
    lines.append("## 简单模型对比（时间顺序切分）")
    lines.append("")
    if model_table.empty:
        lines.append("样本不足或模型未成功训练，当前没有可展示的模型结果。")
    else:
        lines.append(markdown_table(model_table, rows=20))
    lines.append("")
    lines.append("## 缺失值概览")
    lines.append("")
    lines.append(markdown_table(missingness_table(quotes), rows=15))
    lines.append("")
    lines.append("## 图表")
    lines.append("")
    image_map = [
        ("前2分钟BTC涨跌幅分布", "figures/btc_move_2m_hist.png"),
        ("2分钟时入场概率分布", "figures/entry_prob_hist.png"),
        ("前2分钟BTC涨跌幅 vs 入场概率", "figures/btc_move_vs_entry_prob.png"),
        ("按BTC前2分钟涨跌分桶的最终上涨率", "figures/up_rate_by_move_bucket.png"),
        ("按BTC前2分钟涨跌分桶的买Up平均收益", "figures/avg_buy_up_pnl_by_move_bucket.png"),
        ("2分钟时盘口概率的校准图", "figures/entry_prob_calibration.png"),
        ("收益最高的阈值策略", "figures/top_threshold_rules_cum_pnl.png"),
        ("模型累计收益对比", "figures/model_cum_pnl.png"),
        ("最佳阈值规则累计收益曲线", "figures/best_threshold_rule_cum_pnl.png"),
        ("最佳模型累计收益曲线", "figures/best_model_cum_pnl.png"),
    ]
    for title, rel in image_map:
        if rel in [str(Path(p).relative_to(Path(p).parent.parent)) if False else rel for p in plots] or (Path("reports/run_24869603988_attempt1_predictive") / rel).exists():
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"![{title}]({rel})")
            lines.append("")
    lines.append("## 当前可怎么解读")
    lines.append("")
    lines.append("这份报告最适合先回答几个直觉问题：")
    lines.append("")
    lines.append("1. **前2分钟已经大涨 30 美元以后，继续追涨更好，还是反手更好？**")
    lines.append("2. **盘口在2分钟时给出的概率是否校准？**")
    lines.append("3. **简单阈值规则和简单模型，哪个在扣成本后更有优势？**")
    lines.append("")
    lines.append("后续如果你要，我可以继续把报告升级成更完整的版本，例如：")
    lines.append("")
    lines.append("- 做严格 train/validation/test 切分")
    lines.append("- 引入更多盘口深度特征")
    lines.append("- 做 rolling / walk-forward 回测")
    lines.append("- 比较不同 fee 假设下策略是否仍然赚钱")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cleaned dataset and first-2-minute predictive research for source run 24869603988_attempt1")
    parser.add_argument("--source-dir", type=str, required=True)
    parser.add_argument("--cleaned-dir", type=str, default="data/cleaned/run_24869603988_attempt1")
    parser.add_argument("--features-dir", type=str, default="data/features/run_24869603988_attempt1")
    parser.add_argument("--report-dir", type=str, default="reports/run_24869603988_attempt1_predictive")
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    cleaned_dir = Path(args.cleaned_dir)
    features_dir = Path(args.features_dir)
    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "figures"
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    features_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    raw, files = read_source_files(source_dir)
    quotes = prepare_quotes(raw)
    markets = build_markets(quotes)
    features = build_first2m_features(quotes, fee=args.fee)
    move_table = move_bucket_table(features)
    calibration = calibration_table(features)
    threshold_logs = build_threshold_trade_logs(features, fee=args.fee)
    threshold_summary = summarize_threshold_logs(threshold_logs)
    model_table, model_logs = model_comparison(features, fee=args.fee)
    plots = maybe_make_plots(features, move_table, calibration, threshold_summary, threshold_logs, model_table, model_logs, fig_dir)

    quotes.to_csv(cleaned_dir / "btc_updown_5m_quotes_clean.csv", index=False)
    markets.to_csv(cleaned_dir / "btc_updown_5m_markets_clean.csv", index=False)
    features.to_csv(features_dir / "market_features_first2m.csv", index=False)
    move_table.to_csv(report_dir / "move_bucket_summary.csv", index=False)
    calibration.to_csv(report_dir / "entry_prob_calibration.csv", index=False)
    threshold_logs.to_csv(report_dir / "threshold_trade_logs.csv", index=False)
    threshold_summary.to_csv(report_dir / "threshold_rule_comparison.csv", index=False)
    model_table.to_csv(report_dir / "model_comparison.csv", index=False)
    model_logs.to_csv(report_dir / "model_trade_logs.csv", index=False)

    report_md = build_report(files, quotes, features, move_table, calibration, threshold_summary, model_table, plots, fee=args.fee)
    (report_dir / "report.md").write_text(report_md, encoding="utf-8")

    summary = {
        "run_id": RUN_ID,
        "source_dir": str(source_dir),
        "source_file_count": len(files),
        "quote_rows": int(len(quotes)),
        "market_rows": int(len(markets)),
        "feature_rows": int(len(features)),
        "threshold_rule_rows": int(len(threshold_summary)),
        "model_rows": int(len(model_table)),
        "figure_count": len(plots),
    }
    write_json(report_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

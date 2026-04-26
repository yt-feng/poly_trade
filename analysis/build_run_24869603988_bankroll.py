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
    "mid_up_prob_change_2m","buy_up_price_2m","buy_down_price_2m",
    "buy_up_size_2m","buy_down_size_2m","sell_up_size_2m","sell_down_size_2m",
    "size_imbalance_updown_2m","depth_imbalance_updown_2m",
    "spread_up_median_first2m","spread_down_median_first2m","overround_median_first2m",
    "quote_count_first2m",
]

MOVE_BINS = [-10000, -100, -50, -30, -10, 10, 30, 50, 100, 10000]
MOVE_LABELS = ["<=-100","-100~-50","-50~-30","-30~-10","-10~10","10~30","30~50","50~100",">=100"]

KELLY_FRACTIONS = {
    "full_kelly": 1.0,
    "half_kelly": 0.5,
    "quarter_kelly": 0.25,
    "fixed_10pct": None,
    "fixed_20pct": None,
}


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
    if {"mid_up_cents","buy_up_cents","sell_up_cents"}.issubset(quotes.columns):
        quotes["mid_up_cents"] = quotes["mid_up_cents"].fillna((quotes["buy_up_cents"] + quotes["sell_up_cents"]) / 2.0)
    if {"mid_down_cents","buy_down_cents","sell_down_cents"}.issubset(quotes.columns):
        quotes["mid_down_cents"] = quotes["mid_down_cents"].fillna((quotes["buy_down_cents"] + quotes["sell_down_cents"]) / 2.0)
    quotes["close_ts_utc"] = quotes["slug"].map(parse_close_ts_from_slug)
    quotes["mid_sum_cents"] = quotes[["mid_up_cents","mid_down_cents"]].sum(axis=1, min_count=2)
    quotes["mid_overround_cents"] = quotes["mid_sum_cents"] - 100.0
    quotes["mid_up_prob"] = quotes["mid_up_cents"] / 100.0
    quotes["mid_down_prob"] = quotes["mid_down_cents"] / 100.0
    quotes["btc_move_from_target"] = quotes["final_price"] - quotes["target_price"]
    quotes = quotes.sort_values(["slug","ts_utc","source_file"]).drop_duplicates(subset=["ts_iso","slug"], keep="last")
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

        buy_up_price_2m = pd.to_numeric(last_row.get("buy_up_cents"), errors="coerce")
        buy_down_price_2m = pd.to_numeric(last_row.get("buy_down_cents"), errors="coerce")
        buy_up_price_2m = np.nan if pd.isna(buy_up_price_2m) else float(buy_up_price_2m) / 100.0
        buy_down_price_2m = np.nan if pd.isna(buy_down_price_2m) else float(buy_down_price_2m) / 100.0

        buy_up_size_2m = pd.to_numeric(last_row.get("buy_up_size"), errors="coerce")
        buy_down_size_2m = pd.to_numeric(last_row.get("buy_down_size"), errors="coerce")
        sell_up_size_2m = pd.to_numeric(last_row.get("sell_up_size"), errors="coerce")
        sell_down_size_2m = pd.to_numeric(last_row.get("sell_down_size"), errors="coerce")
        bid_depth_up_2m = pd.to_numeric(last_row.get("bid_depth_up_5"), errors="coerce")
        bid_depth_down_2m = pd.to_numeric(last_row.get("bid_depth_down_5"), errors="coerce")

        size_imb = np.nan
        if pd.notna(buy_up_size_2m) and pd.notna(buy_down_size_2m) and (buy_up_size_2m + buy_down_size_2m) > 0:
            size_imb = float((buy_up_size_2m - buy_down_size_2m) / (buy_up_size_2m + buy_down_size_2m))
        depth_imb = np.nan
        if pd.notna(bid_depth_up_2m) and pd.notna(bid_depth_down_2m) and (bid_depth_up_2m + bid_depth_down_2m) > 0:
            depth_imb = float((bid_depth_up_2m - bid_depth_down_2m) / (bid_depth_up_2m + bid_depth_down_2m))

        realized_pnl_buy_up = np.nan
        realized_pnl_buy_down = np.nan
        if pd.notna(outcome_up) and pd.notna(buy_up_price_2m):
            realized_pnl_buy_up = float(outcome_up - buy_up_price_2m - fee)
        if pd.notna(outcome_up) and pd.notna(buy_down_price_2m):
            realized_pnl_buy_down = float((1.0 - outcome_up) - buy_down_price_2m - fee)

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
            "buy_up_price_2m": buy_up_price_2m,
            "buy_down_price_2m": buy_down_price_2m,
            "buy_up_size_2m": buy_up_size_2m,
            "buy_down_size_2m": buy_down_size_2m,
            "sell_up_size_2m": sell_up_size_2m,
            "sell_down_size_2m": sell_down_size_2m,
            "size_imbalance_updown_2m": size_imb,
            "depth_imbalance_updown_2m": depth_imb,
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


def safe_log_loss(y_true: np.ndarray, probs: np.ndarray) -> float:
    try:
        return float(log_loss(y_true, np.clip(probs, 1e-6, 1 - 1e-6), labels=[0,1]))
    except Exception:
        return float("nan")


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
    decisions = np.where(probs > test_df["buy_up_price_2m"].to_numpy() + fee, "buy_up",
                         np.where((1.0 - probs) > test_df["buy_down_price_2m"].to_numpy() + fee, "buy_down", "skip"))
    pnl = np.full(len(test_df), np.nan)
    bu = decisions == "buy_up"
    bd = decisions == "buy_down"
    pnl[bu] = y[bu] - test_df["buy_up_price_2m"].to_numpy()[bu] - fee
    pnl[bd] = (1.0 - y[bd]) - test_df["buy_down_price_2m"].to_numpy()[bd] - fee
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
    log_df = test_df[["slug","first_quote_ts","btc_move_2m","buy_up_price_2m","buy_down_price_2m","outcome_up"]].copy()
    log_df["model"] = name
    log_df["pred_prob_up"] = probs
    log_df["decision"] = decisions
    log_df["pnl_per_share"] = pnl
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


def rolling_model_probabilities(features: pd.DataFrame, min_history: int = 60) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna()].copy().sort_values("first_quote_ts").reset_index(drop=True)
    if usable.empty:
        return pd.DataFrame()
    rows = []
    for i in range(len(usable)):
        row = usable.iloc[i].copy()
        rec = {
            "slug": row["slug"], "first_quote_ts": row["first_quote_ts"], "outcome_up": row["outcome_up"],
            "buy_up_price_2m": row["buy_up_price_2m"], "buy_down_price_2m": row["buy_down_price_2m"],
            "buy_up_size_2m": row["buy_up_size_2m"], "buy_down_size_2m": row["buy_down_size_2m"],
            "btc_move_2m": row["btc_move_2m"], "mid_up_prob_2m": row["mid_up_prob_2m"],
            "pred_prob_up_baseline": np.nan, "pred_prob_up_logistic": np.nan, "pred_prob_up_random_forest": np.nan,
        }
        if i >= min_history:
            hist = usable.iloc[:i].copy()
            x_train = hist[MODEL_FEATURES]
            med = x_train.median(numeric_only=True)
            x_train = x_train.fillna(med)
            x_row = usable.iloc[[i]][MODEL_FEATURES].fillna(med)
            y_train = hist["outcome_up"].astype(int)
            rec["pred_prob_up_baseline"] = float(y_train.mean())
            try:
                if y_train.nunique() >= 2:
                    lr = LogisticRegression(max_iter=1000)
                    lr.fit(x_train, y_train)
                    rec["pred_prob_up_logistic"] = float(lr.predict_proba(x_row)[:,1][0])
            except Exception:
                pass
            try:
                if y_train.nunique() >= 2:
                    rf = RandomForestClassifier(n_estimators=300, random_state=42, min_samples_leaf=3)
                    rf.fit(x_train, y_train)
                    rec["pred_prob_up_random_forest"] = float(rf.predict_proba(x_row)[:,1][0])
            except Exception:
                pass
        rows.append(rec)
    return pd.DataFrame(rows)


def build_rule_signals(features: pd.DataFrame) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna()].copy().sort_values("first_quote_ts").reset_index(drop=True)
    if usable.empty:
        return pd.DataFrame()
    out = usable[[
        "slug","first_quote_ts","outcome_up","buy_up_price_2m","buy_down_price_2m",
        "buy_up_size_2m","buy_down_size_2m","btc_move_2m","mid_up_prob_2m",
    ]].copy()
    out["signal_side_drop10"] = np.where(out["btc_move_2m"] <= -10, "buy_down", "skip")
    out["signal_side_drop20"] = np.where(out["btc_move_2m"] <= -20, "buy_down", "skip")
    out["signal_side_drop30"] = np.where(out["btc_move_2m"] <= -30, "buy_down", "skip")
    out["signal_side_rise30to50"] = np.where((out["btc_move_2m"] > 30) & (out["btc_move_2m"] <= 50), "buy_up", "skip")
    out["signal_side_interval_best"] = np.where((out["btc_move_2m"] > -30) & (out["btc_move_2m"] <= -10), "buy_down",
                                         np.where((out["btc_move_2m"] > 30) & (out["btc_move_2m"] <= 50), "buy_up", "skip"))
    return out


def apply_model_value_strategy(signals: pd.DataFrame, pred_col: str, fee: float, edge_buffer: float = 0.0) -> pd.DataFrame:
    df = signals.copy()
    q = df[pred_col]
    df["signal_side"] = np.where(
        q > df["buy_up_price_2m"] + fee + edge_buffer, "buy_up",
        np.where((1.0 - q) > df["buy_down_price_2m"] + fee + edge_buffer, "buy_down", "skip")
    )
    return df


def kelly_fraction_for_binary(prob: float, price: float) -> float:
    if pd.isna(prob) or pd.isna(price) or price <= 0 or price >= 1:
        return 0.0
    denom = 1.0 - price
    if denom <= 0:
        return 0.0
    return float(max((prob - price) / denom, 0.0))


def simulate_bankroll(df: pd.DataFrame, strategy_name: str, side_col: str, prob_col: str | None, fee: float, starting_bankroll: float = 100.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    ordered = df.copy().sort_values("first_quote_ts").reset_index(drop=True)
    logs = []
    summaries = []
    for sizing_name, mult in KELLY_FRACTIONS.items():
        bankroll = starting_bankroll
        peak = starting_bankroll
        max_dd = 0.0
        trade_rows = []
        for _, row in ordered.iterrows():
            side = row[side_col]
            if side not in {"buy_up","buy_down"}:
                continue
            entry_price = row["buy_up_price_2m"] if side == "buy_up" else row["buy_down_price_2m"]
            size_available = row["buy_up_size_2m"] if side == "buy_up" else row["buy_down_size_2m"]
            outcome = row["outcome_up"]
            if pd.isna(entry_price) or pd.isna(size_available) or pd.isna(outcome) or entry_price <= 0:
                continue
            max_cost_at_best = float(size_available) * float(entry_price)
            if prob_col is None:
                q_side = 0.55
            else:
                q_up = row[prob_col]
                if pd.isna(q_up):
                    continue
                q_side = q_up if side == "buy_up" else 1.0 - q_up
            if sizing_name == "fixed_10pct":
                target_cost = bankroll * 0.10
            elif sizing_name == "fixed_20pct":
                target_cost = bankroll * 0.20
            else:
                target_cost = bankroll * kelly_fraction_for_binary(float(q_side), float(entry_price)) * float(mult)
            target_cost = max(0.0, min(target_cost, bankroll, max_cost_at_best))
            shares = 0.0 if entry_price <= 0 else target_cost / entry_price
            if target_cost <= 0 or shares <= 0:
                continue
            payout = shares * (1.0 if ((side == "buy_up" and outcome == 1.0) or (side == "buy_down" and outcome == 0.0)) else 0.0)
            pnl = payout - target_cost - shares * fee
            bankroll += pnl
            peak = max(peak, bankroll)
            max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
            trade_rows.append({
                "strategy": strategy_name, "sizing": sizing_name, "first_quote_ts": row["first_quote_ts"], "slug": row["slug"],
                "side": side, "entry_price": entry_price, "qhat_for_side": q_side, "target_cost": target_cost,
                "shares": shares, "best_price_max_cost": max_cost_at_best, "outcome_up": outcome, "pnl_usd": pnl,
                "bankroll_after": bankroll,
            })
        trade_log = pd.DataFrame(trade_rows)
        if trade_log.empty:
            summaries.append({"strategy": strategy_name, "sizing": sizing_name, "trades": 0, "ending_bankroll": starting_bankroll, "total_return": 0.0, "avg_trade_return_on_cost": np.nan, "max_drawdown": 0.0})
        else:
            summaries.append({
                "strategy": strategy_name, "sizing": sizing_name, "trades": int(len(trade_log)),
                "ending_bankroll": float(trade_log["bankroll_after"].iloc[-1]),
                "total_return": float(trade_log["bankroll_after"].iloc[-1] / starting_bankroll - 1.0),
                "avg_trade_return_on_cost": float((trade_log["pnl_usd"] / trade_log["target_cost"]).mean()),
                "max_drawdown": float(max_dd),
            })
            logs.append(trade_log)
    return pd.concat(logs, ignore_index=True) if logs else pd.DataFrame(), pd.DataFrame(summaries).sort_values("ending_bankroll", ascending=False).reset_index(drop=True)


def run_all_bankroll_backtests(features: pd.DataFrame, fee: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    rule_df = build_rule_signals(features)
    rolling_df = rolling_model_probabilities(features, min_history=60)
    all_logs = []
    all_summaries = []
    for name, col in [
        ("rule_drop10_down","signal_side_drop10"),
        ("rule_drop20_down","signal_side_drop20"),
        ("rule_drop30_down","signal_side_drop30"),
        ("rule_rise30to50_up","signal_side_rise30to50"),
        ("rule_interval_best","signal_side_interval_best"),
    ]:
        lg, sm = simulate_bankroll(rule_df, name, col, None, fee)
        all_logs.append(lg); all_summaries.append(sm)
    if not rolling_df.empty:
        for pred_col, name in [
            ("pred_prob_up_baseline","model_value_baseline"),
            ("pred_prob_up_logistic","model_value_logistic"),
            ("pred_prob_up_random_forest","model_value_random_forest"),
        ]:
            sig = apply_model_value_strategy(rolling_df, pred_col, fee, edge_buffer=0.0)
            lg, sm = simulate_bankroll(sig, name, "signal_side", pred_col, fee)
            all_logs.append(lg); all_summaries.append(sm)
            sig2 = apply_model_value_strategy(rolling_df, pred_col, fee, edge_buffer=0.02)
            lg2, sm2 = simulate_bankroll(sig2, name + "_edge2pct", "signal_side", pred_col, fee)
            all_logs.append(lg2); all_summaries.append(sm2)
    logs = pd.concat([x for x in all_logs if not x.empty], ignore_index=True) if all_logs else pd.DataFrame()
    summaries = pd.concat([x for x in all_summaries if not x.empty], ignore_index=True) if all_summaries else pd.DataFrame()
    if not summaries.empty:
        summaries = summaries.sort_values("ending_bankroll", ascending=False).reset_index(drop=True)
    return logs, summaries


def move_bucket_table(features: pd.DataFrame) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["btc_move_2m"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()
    usable["move_bucket"] = pd.cut(usable["btc_move_2m"], bins=MOVE_BINS, labels=MOVE_LABELS, include_lowest=True)
    out = usable.groupby("move_bucket", as_index=False, observed=False).agg(
        count=("outcome_up", "size"),
        avg_btc_move_2m=("btc_move_2m", "mean"),
        avg_buy_up_price=("buy_up_price_2m", "mean"),
        avg_buy_down_price=("buy_down_price_2m", "mean"),
        avg_buy_up_size=("buy_up_size_2m", "mean"),
        avg_buy_down_size=("buy_down_size_2m", "mean"),
        realized_up_rate=("outcome_up", "mean"),
        avg_pnl_buy_up=("realized_pnl_buy_up_from_2m", "mean"),
        avg_pnl_buy_down=("realized_pnl_buy_down_from_2m", "mean"),
    )
    out["best_side"] = np.where(out["avg_pnl_buy_up"] >= out["avg_pnl_buy_down"], "buy_up", "buy_down")
    out["best_avg_pnl"] = out[["avg_pnl_buy_up","avg_pnl_buy_down"]].max(axis=1)
    return out


def volume_summary_table(features: pd.DataFrame) -> pd.DataFrame:
    cols = ["buy_up_size_2m","buy_down_size_2m","sell_up_size_2m","sell_down_size_2m","size_imbalance_updown_2m","depth_imbalance_updown_2m"]
    rows = []
    for c in cols:
        s = pd.to_numeric(features[c], errors="coerce")
        rows.append({
            "feature": c,
            "non_null": int(s.notna().sum()),
            "mean": float(s.mean()) if s.notna().any() else np.nan,
            "median": float(s.median()) if s.notna().any() else np.nan,
            "p90": float(s.quantile(0.9)) if s.notna().any() else np.nan,
        })
    return pd.DataFrame(rows)


def markdown_table(df: pd.DataFrame, rows: int = 20) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def missingness_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "column": df.columns,
        "missing_ratio": [df[c].isna().mean() for c in df.columns],
        "non_null": [int(df[c].notna().sum()) for c in df.columns],
    }).sort_values("missing_ratio", ascending=False).reset_index(drop=True)


def maybe_make_plots(features: pd.DataFrame, bankroll_summary: pd.DataFrame, bankroll_logs: pd.DataFrame, move_table: pd.DataFrame, volume_table: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not features.empty and features["btc_move_2m"].notna().any():
        plt.figure(figsize=(8,4))
        plt.hist(features["btc_move_2m"].dropna(), bins=30)
        plt.title("前2分钟BTC涨跌幅分布（美元）")
        plt.tight_layout()
        p = fig_dir / "btc_move_2m_hist.png"
        plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))
    if not move_table.empty:
        plt.figure(figsize=(10,4))
        plt.bar(move_table["move_bucket"].astype(str), move_table["best_avg_pnl"])
        plt.xticks(rotation=45, ha="right")
        plt.title("不同涨跌区间的最佳方向平均每份PnL")
        plt.tight_layout()
        p = fig_dir / "best_avg_pnl_by_move_bucket.png"
        plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))
    if not volume_table.empty:
        plt.figure(figsize=(8,4))
        plt.bar(volume_table["feature"], volume_table["median"])
        plt.xticks(rotation=45, ha="right")
        plt.title("2分钟盘口size/depth特征中位数")
        plt.tight_layout()
        p = fig_dir / "volume_depth_feature_median.png"
        plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))
    if not bankroll_summary.empty:
        top = bankroll_summary.head(15).copy()
        labels = top["strategy"] + "|" + top["sizing"]
        plt.figure(figsize=(12,5))
        plt.bar(labels, top["ending_bankroll"])
        plt.xticks(rotation=60, ha="right")
        plt.title("100美元本金下，各策略-仓位组合的期末本金")
        plt.tight_layout()
        p = fig_dir / "bankroll_top_endings.png"
        plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))
    if not bankroll_logs.empty and not bankroll_summary.empty:
        best = bankroll_summary.iloc[0]
        s = bankroll_logs[(bankroll_logs["strategy"] == best["strategy"]) & (bankroll_logs["sizing"] == best["sizing"])].copy().sort_values("first_quote_ts")
        if not s.empty:
            plt.figure(figsize=(10,4))
            plt.plot(s["first_quote_ts"], s["bankroll_after"])
            plt.title(f"最佳组合本金曲线: {best['strategy']} | {best['sizing']}")
            plt.tight_layout()
            p = fig_dir / "best_bankroll_curve.png"
            plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))
        best_strategy = best["strategy"]
        plt.figure(figsize=(10,4))
        for sizing in bankroll_logs[bankroll_logs["strategy"] == best_strategy]["sizing"].dropna().unique():
            s = bankroll_logs[(bankroll_logs["strategy"] == best_strategy) & (bankroll_logs["sizing"] == sizing)].copy().sort_values("first_quote_ts")
            if not s.empty:
                plt.plot(s["first_quote_ts"], s["bankroll_after"], label=sizing)
        plt.legend()
        plt.title(f"同一策略不同仓位曲线: {best_strategy}")
        plt.tight_layout()
        p = fig_dir / "best_strategy_sizing_curves.png"
        plt.savefig(p, dpi=150); plt.close(); paths.append(str(p))
    return paths


def build_report(files: List[Path], features: pd.DataFrame, volume_table: pd.DataFrame, move_table: pd.DataFrame, model_table: pd.DataFrame, bankroll_summary: pd.DataFrame, fee: float) -> str:
    lines: List[str] = []
    lines.append(f"# 100美元本金 + Kelly 仓位的 5分钟交易回测报告：{RUN_ID}")
    lines.append("")
    lines.append("## 这次升级了什么")
    lines.append("")
    lines.append("- 用 `buy_up_cents / buy_down_cents` 作为真实入场价格，而不是 mid price")
    lines.append("- 把 `buy_up_size / buy_down_size / sell_up_size / sell_down_size` 当作盘口可成交量/流动性特征")
    lines.append("- 按 100 美元初始本金，顺序滚动做 bankroll 回测")
    lines.append("- 同时测试全凯利、半凯利、0.25 凯利、固定 10% 仓位、固定 20% 仓位")
    lines.append("- 回测对象包括规则策略和 walk-forward 模型价值策略")
    lines.append("")
    lines.append("## 关于 volume / size")
    lines.append("")
    lines.append("这次不再把 volume 只理解成 `trade_volume_1s`。在这份 CLOB 快照数据里，更有信息量的是盘口 size：")
    lines.append("")
    lines.append("- `buy_up_size / buy_down_size`：当前最优买入价位可成交的份数上限")
    lines.append("- `sell_up_size / sell_down_size`：对侧卖出挂单的份数快照")
    lines.append("- 同时加入了 `size_imbalance_updown_2m` 和 `depth_imbalance_updown_2m`")
    lines.append("")
    lines.append(markdown_table(volume_table, rows=20))
    lines.append("")
    lines.append("## 数据概览")
    lines.append("")
    lines.append(f"- 原始分块文件数：**{len(files)}**")
    lines.append(f"- 市场级回测样本数：**{len(features)}**")
    lines.append(f"- 每份合约固定附加成本 fee：**{fee:.4f}**")
    if not features.empty and features["outcome_up"].notna().any():
        lines.append(f"- 已解析 outcome 的 Up 比例：**{features.loc[features['outcome_up'].notna(), 'outcome_up'].mean():.4f}**")
    lines.append("")
    lines.append("## 涨跌分桶的单位PnL")
    lines.append("")
    lines.append(markdown_table(move_table, rows=20))
    lines.append("")
    lines.append("## 简单模型的单位份额比较")
    lines.append("")
    lines.append(markdown_table(model_table, rows=20))
    lines.append("")
    lines.append("## 100美元本金 bankroll 回测结果")
    lines.append("")
    lines.append("这里的本金曲线是按时间顺序逐笔滚动的，下一笔交易使用上一笔结算后的本金。")
    lines.append("")
    lines.append(markdown_table(bankroll_summary, rows=30))
    lines.append("")
    if not bankroll_summary.empty:
        best = bankroll_summary.iloc[0]
        lines.append("## 当前最优组合")
        lines.append("")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 仓位：**{best['sizing']}**")
        lines.append(f"- 交易笔数：**{int(best['trades'])}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 总收益率：**{best['total_return']:.2%}**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    for title, rel in [
        ("前2分钟BTC涨跌幅分布", "figures/btc_move_2m_hist.png"),
        ("不同涨跌区间的最佳方向平均每份PnL", "figures/best_avg_pnl_by_move_bucket.png"),
        ("盘口size/depth特征中位数", "figures/volume_depth_feature_median.png"),
        ("各策略-仓位组合的期末本金", "figures/bankroll_top_endings.png"),
        ("最佳组合本金曲线", "figures/best_bankroll_curve.png"),
        ("最佳策略的不同仓位曲线", "figures/best_strategy_sizing_curves.png"),
    ]:
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"![{title}]({rel})")
        lines.append("")
    lines.append("## 缺失值概览")
    lines.append("")
    lines.append(markdown_table(missingness_table(features), rows=20))
    lines.append("")
    lines.append("## 备注")
    lines.append("")
    lines.append("- 这里把 `buy_*_cents` 当作买入该方向的入场价格，`buy_*_size` 当作该最优价位可成交的份数上限。")
    lines.append("- 这是按 top-of-book 的简化回测，还没有模拟扫多档深度。")
    lines.append("- 但它已经比之前的 mid-price 假设更接近真实的 Polymarket CLOB 交易。")
    return "\n".join(lines)


def write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
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
    volume_table = volume_summary_table(features)
    model_table, model_logs = model_comparison(features, fee=args.fee)
    bankroll_logs, bankroll_summary = run_all_bankroll_backtests(features, fee=args.fee)
    plots = maybe_make_plots(features, bankroll_summary, bankroll_logs, move_table, volume_table, fig_dir)

    quotes.to_csv(cleaned_dir / "btc_updown_5m_quotes_clean.csv", index=False)
    markets.to_csv(cleaned_dir / "btc_updown_5m_markets_clean.csv", index=False)
    features.to_csv(features_dir / "market_features_first2m.csv", index=False)
    move_table.to_csv(report_dir / "move_bucket_summary.csv", index=False)
    volume_table.to_csv(report_dir / "volume_feature_summary.csv", index=False)
    model_table.to_csv(report_dir / "model_comparison.csv", index=False)
    model_logs.to_csv(report_dir / "model_trade_logs.csv", index=False)
    bankroll_logs.to_csv(report_dir / "bankroll_trade_logs.csv", index=False)
    bankroll_summary.to_csv(report_dir / "bankroll_strategy_summary.csv", index=False)
    (report_dir / "report.md").write_text(build_report(files, features, volume_table, move_table, model_table, bankroll_summary, args.fee), encoding="utf-8")
    summary = {
        "run_id": RUN_ID,
        "source_file_count": len(files),
        "quote_rows": int(len(quotes)),
        "market_rows": int(len(markets)),
        "feature_rows": int(len(features)),
        "bankroll_strategy_rows": int(len(bankroll_summary)),
        "figure_count": len(plots),
    }
    write_json(report_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

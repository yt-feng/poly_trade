from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1

FEE = 0.01
WINDOW_36H = 144
WINDOW_72H = 864


# ---------- data prep ----------

def rolling_logit_safe(df: pd.DataFrame, min_history: int = 100, retrain_every: int = 25, lookback: int = 1200) -> pd.Series:
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
            hist = hist[pd.notna(hist["outcome_up"])].copy()
            if len(hist) < max(30, min_history // 2):
                next_retrain = i + retrain_every
                continue
            y = hist["outcome_up"].astype(int)
            if y.nunique() < 2:
                next_retrain = i + retrain_every
                continue
            X = hist[base.LOGIT_FEATURES].copy()
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
            x = df.iloc[[i]][base.LOGIT_FEATURES].copy().fillna(med)
            try:
                probs[i] = float(model.predict_proba(x)[:, 1][0])
            except Exception:
                pass
    return pd.Series(probs, index=df.index)


def add_session_labels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ts_et = pd.to_datetime(out["first_quote_ts"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
    mins = ts_et.dt.hour * 60 + ts_et.dt.minute
    weekday = ts_et.dt.dayofweek
    out["session_et"] = np.select(
        [
            (mins >= 1140) | (mins < 120),
            (mins >= 120) & (mins < 570),
            (weekday < 5) & (mins >= 570) & (mins < 720),
            (weekday < 5) & (mins >= 720) & (mins < 960),
        ],
        ["asia", "london", "us_open", "us_afternoon"],
        default="other",
    )
    return out


def add_quality_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["triple_gate_down"] = (
        (out["spread_down_median_first2m"].fillna(1.0) <= 0.05)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.04)
        & (out["buy_down_size_2m"].fillna(0) >= 150)
    )
    out["triple_gate_up"] = (
        (out["spread_up_median_first2m"].fillna(1.0) <= 0.05)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.04)
        & (out["buy_up_size_4m"].fillna(0) >= 120)
    )
    out["triple_gate_down_strict"] = (
        (out["spread_down_median_first2m"].fillna(1.0) <= 0.04)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.03)
        & (out["buy_down_size_2m"].fillna(0) >= 180)
        & (out["quality_milddrop"].fillna(0) >= 0.58 if "quality_milddrop" in out.columns else True)
    )
    out["triple_gate_up_strict"] = (
        (out["spread_up_median_first2m"].fillna(1.0) <= 0.04)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.03)
        & (out["buy_up_size_4m"].fillna(0) >= 150)
    )
    breakout_liq = np.clip(out["buy_up_size_4m"].fillna(0) / 250.0, 0, 1)
    breakout_spread = np.clip(1 - out["spread_up_median_first2m"].fillna(0.2) / 0.08, 0, 1)
    breakout_overround = np.clip(1 - out["overround_median_first2m"].fillna(0.2) / 0.06, 0, 1)
    breakout_signal = np.clip(1 - (out["btc_move_4m"].fillna(0) - 40).abs() / 20.0, 0, 1)
    out["quality_breakout"] = (breakout_liq + breakout_spread + breakout_overround + breakout_signal) / 4.0
    mild_liq = np.clip(out["buy_down_size_2m"].fillna(0) / 250.0, 0, 1)
    mild_spread = np.clip(1 - out["spread_down_median_first2m"].fillna(0.2) / 0.08, 0, 1)
    mild_overround = np.clip(1 - out["overround_median_first2m"].fillna(0.2) / 0.06, 0, 1)
    mild_book = np.clip((-out["size_imbalance_updown_2m"].fillna(1.0) + (out["book_pressure_down_2m"].fillna(-1.0) + 1) / 2.0) / 2.0, 0, 1)
    out["quality_milddrop"] = (mild_liq + mild_spread + mild_overround + mild_book) / 4.0
    out["recent_tail_flag"] = False
    if len(out) >= WINDOW_72H:
        out.loc[out.index[-WINDOW_72H:], "recent_tail_flag"] = True
    return out


def breakout_signal(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_4m")) and 30 < row["btc_move_4m"] <= 50 and row.get("buy_up_price_4m", 1.0) <= 0.90 and row.get("buy_up_size_4m", 0) >= 100


def milddrop_signal(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and -30 < row["btc_move_2m"] <= -10 and row.get("size_imbalance_updown_2m", 1.0) <= 0 and row.get("book_pressure_down_2m", -1.0) >= -0.2


def sharpdrop_reversal(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and -50 < row["btc_move_2m"] <= -30 and row.get("buy_up_price_2m", 1.0) <= 0.45 and row.get("buy_up_size_2m", 0) >= 120


def early_drop_down(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_1m")) and row.get("btc_move_1m") <= -20 and row.get("buy_down_size_1m", 0) >= 80


def quality_fraction(quality: float, low: float, high: float) -> float:
    q = 0.0 if pd.isna(quality) else float(np.clip(quality, 0, 1))
    return float(low + (high - low) * q)


# ---------- strategy library ----------

def choose_trade(row: pd.Series, strategy: str) -> Tuple[str, int, float, str]:
    sess = row.get("session_et")
    q_up = row.get("pred_prob_up_logit", np.nan)

    # Keep old final v1 candidates for direct comparison
    if strategy in {"v1_conservative_mix", "v1_balanced_mix", "v1_adaptive_mix", "v1_active_fill_mix", "v1_logit_overlay_mix"}:
        return v1.choose_trade(row, strategy)

    # More conservative session-aware mixes
    if strategy == "v2_regime_guarded_mix":
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up_strict", False):
            return "buy_up", 4, 0.05, "asia_breakout_strict"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down_strict", False):
            return "buy_down", 2, 0.05, "london_milddrop_strict"
        if sess == "us_afternoon" and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.62:
            return "buy_down", 2, 0.06, "us_afternoon_milddrop_strict"
        return "skip", -1, np.nan, "none"

    if strategy == "v2_regime_guarded_adaptive":
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up_strict", False):
            return "buy_up", 4, quality_fraction(row.get("quality_breakout", np.nan), 0.03, 0.07), "asia_breakout_strict"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down_strict", False):
            return "buy_down", 2, quality_fraction(row.get("quality_milddrop", np.nan), 0.03, 0.07), "london_milddrop_strict"
        if sess == "us_afternoon" and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.62:
            return "buy_down", 2, quality_fraction(row.get("quality_milddrop", np.nan), 0.04, 0.08), "us_afternoon_milddrop_strict"
        return "skip", -1, np.nan, "none"

    if strategy == "v2_shock_absorber_mix":
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up", False) and row.get("quality_breakout", 0) >= 0.62:
            return "buy_up", 4, 0.06, "asia_breakout"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down", False) and row.get("quality_milddrop", 0) >= 0.58:
            return "buy_down", 2, 0.05, "london_milddrop"
        if sess == "us_afternoon" and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.60:
            return "buy_down", 2, 0.07, "us_afternoon_milddrop"
        if sess == "other" and sharpdrop_reversal(row) and row.get("quality_breakout", 0) >= 0.55:
            return "buy_up", 2, 0.03, "other_sharpdrop_rev"
        return "skip", -1, np.nan, "none"

    if strategy == "v2_milddrop_core_light":
        if sess in {"london", "us_afternoon"} and milddrop_signal(row) and row.get("triple_gate_down", False):
            frac = 0.05 if sess == "london" else 0.06
            return "buy_down", 2, frac, f"{sess}_milddrop"
        return "skip", -1, np.nan, "none"

    if strategy == "v2_asia_london_pair":
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up", False):
            return "buy_up", 4, 0.06, "asia_breakout"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down", False):
            return "buy_down", 2, 0.05, "london_milddrop"
        return "skip", -1, np.nan, "none"

    if strategy == "v2_logit_gated_session_mix":
        if pd.isna(q_up):
            return "skip", -1, np.nan, "none"
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up", False):
            edge = q_up - row.get("buy_up_price_4m", 1.0)
            if edge > 0.06:
                return "buy_up", 4, 0.05, "asia_breakout_logit"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down", False):
            edge = (1 - q_up) - row.get("buy_down_price_2m", 1.0)
            if edge > 0.06:
                return "buy_down", 2, 0.05, "london_milddrop_logit"
        if sess == "us_afternoon" and milddrop_signal(row):
            edge = (1 - q_up) - row.get("buy_down_price_2m", 1.0)
            if edge > 0.08:
                return "buy_down", 2, 0.06, "us_afternoon_milddrop_logit"
        return "skip", -1, np.nan, "none"

    if strategy == "v2_early_drop_and_milddrop":
        if sess in {"london", "us_afternoon"} and early_drop_down(row) and row.get("spread_down_median_first2m", 1.0) <= 0.05:
            return "buy_down", 1, 0.04, f"{sess}_earlydrop"
        if sess in {"london", "us_afternoon"} and milddrop_signal(row) and row.get("triple_gate_down", False):
            return "buy_down", 2, 0.05, f"{sess}_milddrop"
        return "skip", -1, np.nan, "none"

    return "skip", -1, np.nan, "none"


def payout(row: pd.Series, side: str, minute: int, fee: float) -> Tuple[float, float, float]:
    if side == "buy_up":
        price = row[f"buy_up_price_{minute}m"]
        size = row.get(f"buy_up_size_{minute}m", np.nan)
        pnl_per_share = row["outcome_up"] - price - fee
    else:
        price = row[f"buy_down_price_{minute}m"]
        size = row.get(f"buy_down_size_{minute}m", np.nan)
        pnl_per_share = (1.0 - row["outcome_up"]) - price - fee
    return float(price), float(size), float(pnl_per_share)


def window_metrics(event_returns: np.ndarray, trade_flags: np.ndarray, window: int) -> Dict[str, float]:
    if len(event_returns) < window:
        return {
            f"worst_{window}_window_return": np.nan,
            f"median_{window}_window_return": np.nan,
            f"pct_positive_{window}_windows": np.nan,
            f"active_{window}_window_rate": np.nan,
            f"num_{window}_windows": 0,
        }
    vals, active = [], []
    for i in range(0, len(event_returns) - window + 1):
        vals.append(float(np.prod(1.0 + event_returns[i:i+window]) - 1.0))
        active.append(int(np.sum(trade_flags[i:i+window]) > 0))
    arr = np.array(vals, dtype=float)
    act = np.array(active, dtype=float)
    return {
        f"worst_{window}_window_return": float(np.min(arr)),
        f"median_{window}_window_return": float(np.median(arr)),
        f"pct_positive_{window}_windows": float(np.mean(arr > 0)),
        f"active_{window}_window_rate": float(np.mean(act > 0)),
        f"num_{window}_windows": int(len(arr)),
    }


def max_drawdown_from_returns(event_returns: np.ndarray) -> float:
    wealth = np.cumprod(1.0 + event_returns)
    if len(wealth) == 0:
        return 0.0
    peak = np.maximum.accumulate(wealth)
    dd = (peak - wealth) / peak
    return float(np.nanmax(dd)) if len(dd) else 0.0


def simulate(df: pd.DataFrame, strategy: str, fee: float = FEE) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = 100.0
    peak = 100.0
    max_dd = 0.0
    loss_streak = 0
    cooldown = 0
    event_returns = np.zeros(len(df), dtype=float)
    trade_flags = np.zeros(len(df), dtype=int)
    logs: List[Dict[str, object]] = []

    # More robust overlays than old v1
    quota_36 = 10 if strategy.startswith("v2_") else (10 if strategy in {"v1_conservative_mix", "v1_adaptive_mix", "v1_logit_overlay_mix"} else 14)
    quota_72 = 18 if strategy.startswith("v2_") else 28
    cooldown_len = 8 if strategy.startswith("v2_") else (10 if strategy in {"v1_conservative_mix", "v1_logit_overlay_mix"} else 6)
    recent_trade_indices_36: List[int] = []
    recent_trade_indices_72: List[int] = []

    for i, row in df.iterrows():
        recent_trade_indices_36 = [x for x in recent_trade_indices_36 if x > i - WINDOW_36H]
        recent_trade_indices_72 = [x for x in recent_trade_indices_72 if x > i - WINDOW_72H]
        if cooldown > 0:
            cooldown -= 1
            continue
        if len(recent_trade_indices_36) >= quota_36 or len(recent_trade_indices_72) >= quota_72:
            continue

        side, minute, frac, component = choose_trade(row, strategy)
        if side == "skip":
            continue
        price, size_avail, pnl_per_share = payout(row, side, minute, fee)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        bankroll_before = bankroll
        target_cost = min(bankroll * frac, bankroll, float(size_avail) * float(price))
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        event_ret = pnl / bankroll_before if bankroll_before > 0 else 0.0
        event_returns[i] = event_ret
        trade_flags[i] = 1
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        recent_trade_indices_36.append(i)
        recent_trade_indices_72.append(i)
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= 2:
                cooldown = cooldown_len
                loss_streak = 0
        else:
            loss_streak = 0
        logs.append({
            "strategy": strategy,
            "first_quote_ts": row["first_quote_ts"],
            "run_name": row["run_name"],
            "market_id": row["market_id"],
            "slug": row["slug"],
            "session_et": row.get("session_et"),
            "component": component,
            "entry_minute": minute,
            "side": side,
            "fraction": frac,
            "target_cost": target_cost,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
            "event_ret": event_ret,
            "sim_max_drawdown": max_dd,
            "recent_tail_flag": bool(row.get("recent_tail_flag", False)),
        })

    trade_log = pd.DataFrame(logs)
    pnl = pd.to_numeric(trade_log.get("pnl_usd"), errors="coerce").dropna() if not trade_log.empty else pd.Series(dtype=float)
    wins = pnl[pnl > 0].sum() if not pnl.empty else 0.0
    losses = pnl[pnl < 0].sum() if not pnl.empty else 0.0
    metrics = {
        "strategy": strategy,
        "trades": int(len(trade_log)),
        "ending_bankroll": float(bankroll),
        "total_return": float(bankroll / 100.0 - 1.0),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else np.nan,
        "profit_factor": float(wins / abs(losses)) if losses != 0 else np.nan,
        "max_drawdown": float(max_dd),
        "avg_fraction": float(pd.to_numeric(trade_log.get("fraction"), errors="coerce").mean()) if not trade_log.empty else np.nan,
        **window_metrics(event_returns, trade_flags, WINDOW_36H),
        **window_metrics(event_returns, trade_flags, WINDOW_72H),
    }
    # recent 72h snapshot
    if len(df) >= WINDOW_72H:
        tail_flags = np.zeros(len(df), dtype=int)
        tail_flags[-WINDOW_72H:] = 1
        tail_rets = event_returns[-WINDOW_72H:]
        metrics["recent_72h_return"] = float(np.prod(1.0 + tail_rets) - 1.0)
        metrics["recent_72h_max_drawdown"] = max_drawdown_from_returns(tail_rets)
        recent_logs = trade_log[trade_log.get("recent_tail_flag", False) == True] if not trade_log.empty else pd.DataFrame()
        recent_pnl = pd.to_numeric(recent_logs.get("pnl_usd"), errors="coerce").dropna() if not recent_logs.empty else pd.Series(dtype=float)
        metrics["recent_72h_trades"] = int(len(recent_logs))
        metrics["recent_72h_win_rate"] = float((recent_pnl > 0).mean()) if len(recent_pnl) else np.nan
    else:
        metrics["recent_72h_return"] = np.nan
        metrics["recent_72h_max_drawdown"] = np.nan
        metrics["recent_72h_trades"] = 0
        metrics["recent_72h_win_rate"] = np.nan
    return trade_log, metrics


def recent_drift_forensics(logs: pd.DataFrame, focus_strategy: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if logs.empty:
        return pd.DataFrame(), pd.DataFrame()
    s = logs[logs["strategy"] == focus_strategy].copy()
    if s.empty:
        return pd.DataFrame(), pd.DataFrame()
    overall = s.groupby(["session_et", "component"], as_index=False).agg(
        trades_all=("market_id", "size"),
        pnl_all=("pnl_usd", "sum"),
        win_rate_all=("pnl_usd", lambda x: float((x > 0).mean())),
    )
    recent = s[s["recent_tail_flag"] == True].groupby(["session_et", "component"], as_index=False).agg(
        trades_recent=("market_id", "size"),
        pnl_recent=("pnl_usd", "sum"),
        win_rate_recent=("pnl_usd", lambda x: float((x > 0).mean())),
    )
    merged = overall.merge(recent, on=["session_et", "component"], how="outer")
    for col in ["trades_all", "pnl_all", "win_rate_all", "trades_recent", "pnl_recent", "win_rate_recent"]:
        if col in merged.columns:
            merged[col] = merged[col].fillna(0)
    if "win_rate_recent" in merged.columns and "win_rate_all" in merged.columns:
        merged["win_rate_delta"] = merged["win_rate_recent"] - merged["win_rate_all"]
    if "pnl_recent" in merged.columns and "pnl_all" in merged.columns and "trades_recent" in merged.columns:
        merged["avg_pnl_recent"] = np.where(merged["trades_recent"] > 0, merged["pnl_recent"] / merged["trades_recent"], 0)
        merged["avg_pnl_all"] = np.where(merged["trades_all"] > 0, merged["pnl_all"] / merged["trades_all"], 0)
        merged["avg_pnl_delta"] = merged["avg_pnl_recent"] - merged["avg_pnl_all"]
    recent_detail = s[s["recent_tail_flag"] == True].copy().sort_values("first_quote_ts")
    return merged.sort_values(["pnl_recent", "win_rate_delta"], ascending=[True, True]), recent_detail


def score_summary(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    out["score_full_end"] = out["ending_bankroll"].rank(pct=True)
    out["score_full_dd"] = (-out["max_drawdown"].fillna(999)).rank(pct=True)
    out["score_36h"] = out[f"worst_{WINDOW_36H}_window_return"].fillna(-999).rank(pct=True)
    out["score_72h"] = out[f"worst_{WINDOW_72H}_window_return"].fillna(-999).rank(pct=True)
    out["score_recent72"] = out["recent_72h_return"].fillna(-999).rank(pct=True)
    out["score_pf"] = out["profit_factor"].fillna(0).rank(pct=True)
    out["score_win"] = out["win_rate"].fillna(0).rank(pct=True)
    out["refreshed_score"] = 0.15*out["score_full_end"] + 0.20*out["score_full_dd"] + 0.20*out["score_36h"] + 0.15*out["score_72h"] + 0.20*out["score_recent72"] + 0.05*out["score_pf"] + 0.05*out["score_win"]
    out["meets_dd_lt_30"] = out["max_drawdown"] < 0.30
    out["meets_recent72_positive"] = out["recent_72h_return"] > 0
    return out.sort_values(["refreshed_score", "ending_bankroll"], ascending=False).reset_index(drop=True)


def build_strategy_names() -> List[str]:
    return [
        "v1_conservative_mix",
        "v1_balanced_mix",
        "v1_adaptive_mix",
        "v1_active_fill_mix",
        "v1_logit_overlay_mix",
        "v2_regime_guarded_mix",
        "v2_regime_guarded_adaptive",
        "v2_shock_absorber_mix",
        "v2_milddrop_core_light",
        "v2_asia_london_pair",
        "v2_logit_gated_session_mix",
        "v2_early_drop_and_milddrop",
    ]


def markdown_table(df: pd.DataFrame, rows: int = 40) -> str:
    return v1.markdown_table(df, rows=rows)


def generate_definitions() -> str:
    return """# refreshed_monthly_runs_strategy_definitions

这份文档对应 `monthly_runs_full_refresh_research.py`：它不是只对旧策略重新打分，而是基于更新后的 `monthly_runs/*` 做了一次“全量刷新 + 漂移复盘 + 重新精选”。

## 复盘主假设

最近72小时变差，最可能的不是“单一 bug”，而是三件事叠加：

1. **session mix 变了**：最近数据里不同交易时段的占比变化了，旧的时段拼接可能不再占优。
2. **fill 过多**：`v1_active_fill_mix` 的设计是提高覆盖率，它在旧数据里有效，但在 regime 变差时，filler 会放大坏交易。
3. **固定阈值过旧**：旧版 milddrop / breakout 的 gate 是按旧历史校准的；新数据下，spread / overround / liquidity 的好坏边界可能已经漂移。

## 新增候选说明

### `v2_regime_guarded_mix`
- 比 `v1_conservative_mix` 更严格
- Asia breakout 和 London milddrop 都用 stricter gate
- 目的：验证是不是 recent slump 主要来自 gate 太松

### `v2_regime_guarded_adaptive`
- 与 `v2_regime_guarded_mix` 相同
- 但仓位按 quality 更小更谨慎地变化
- 目的：降低 recent tail 风险

### `v2_shock_absorber_mix`
- 保留 Asia / London / US afternoon 三块
- 但用更保守的仓位和更高质量阈值
- 少量引入 other 时段 sharpdrop reversal 作为补充
- 目的：提高覆盖的同时减少 filler 伤害

### `v2_milddrop_core_light`
- 只保留 London + US afternoon milddrop
- 去掉大多数 breakout filler
- 目的：检验最近失效是不是来自 breakout / filler，而不是 milddrop 主体

### `v2_asia_london_pair`
- 只保留 Asia breakout + London milddrop
- 目的：做一个更干净的双核心组合

### `v2_logit_gated_session_mix`
- 在时段策略上叠加 rolling logistic edge
- 目的：如果近期失效来自“盘口已经 price in”，模型 edge filter 可能能拦掉一部分坏单

### `v2_early_drop_and_milddrop`
- 增加 early drop continuation
- 目的：测试在最近样本里，更短的延续信号是否比旧 milddrop 更有效
"""


def make_plots(summary: pd.DataFrame, fig_dir: Path) -> int:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return 0
    fig_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    top = summary.sort_values("refreshed_score", ascending=False).head(12)
    if not top.empty:
        plt.figure(figsize=(14, 5))
        plt.bar(top["strategy"], top["refreshed_score"])
        plt.xticks(rotation=45, ha="right")
        plt.title("Refreshed monthly_runs strategy score")
        plt.tight_layout()
        plt.savefig(fig_dir / "refreshed_scores.png", dpi=150)
        plt.close(); n += 1
        plt.figure(figsize=(14, 5))
        plt.bar(top["strategy"], top["recent_72h_return"])
        plt.xticks(rotation=45, ha="right")
        plt.title("Recent 72h return by top refreshed strategies")
        plt.tight_layout()
        plt.savefig(fig_dir / "recent72_returns.png", dpi=150)
        plt.close(); n += 1
    return n


def build_report(coverage: pd.DataFrame, summary: pd.DataFrame, drift: pd.DataFrame, recent_detail: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# monthly_runs 全量刷新：策略失效复盘与重新精选")
    lines.append("")
    lines.append("这份报告使用当前 `data/monthly_runs/*` 的最新全量数据，复盘最近72小时下滑的原因，并把旧策略与新策略统一放在一起重新打分。")
    lines.append("")
    lines.append("## 当前 monthly_runs 覆盖")
    lines.append("")
    lines.append(markdown_table(coverage, rows=100))
    lines.append("")
    lines.append("## 先说复盘判断")
    lines.append("")
    lines.append("最近72小时下滑，更像是**分布漂移 + 过度追求覆盖率 + 旧 gate 阈值失效**叠加，而不只是一个简单的过拟合故事。")
    lines.append("")
    lines.append("- `v1_active_fill_mix` 的优势原本是 active coverage 高；但这也意味着它在坏 regime 里会继续出手。")
    lines.append("- 如果最近72小时里某些 session 的 milddrop / breakout 胜率明显低于长期均值，那么 filler 与宽松 gate 会把组合拖下去。")
    lines.append("- 所以这次刷新把重心放到：更严格 gate、更小仓位、去 filler、以及 recent72h 表现重新加权。")
    lines.append("")
    lines.append("## v1_active_fill_mix 最近72小时漂移拆分")
    lines.append("")
    lines.append(markdown_table(drift, rows=50))
    lines.append("")
    lines.append("## v1_active_fill_mix 最近72小时交易明细")
    lines.append("")
    lines.append(markdown_table(recent_detail, rows=100))
    lines.append("")
    lines.append("## 重新精选后的 Top 20")
    lines.append("")
    lines.append(markdown_table(summary.sort_values("refreshed_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## 满足回撤 < 30% 且 recent72h 为正 的候选")
    lines.append("")
    lines.append(markdown_table(summary[(summary["meets_dd_lt_30"]) & (summary["meets_recent72_positive"])].sort_values("refreshed_score", ascending=False), rows=20))
    lines.append("")
    if not summary.empty:
        best = summary.sort_values("refreshed_score", ascending=False).iloc[0]
        lines.append("## 当前刷新后的首选")
        lines.append("")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append(f"- 最近72小时收益：**{best['recent_72h_return']:.2%}**")
        lines.append(f"- 36小时最差窗口收益：**{best['worst_{WINDOW_36H}_window_return']:.2%}**")
        lines.append(f"- 72小时最差窗口收益：**{best['worst_{WINDOW_72H}_window_return']:.2%}**")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![Refreshed monthly_runs strategy score](monthly_runs_refresh_figures/refreshed_scores.png)")
    lines.append("")
    lines.append("![Recent 72h return by top refreshed strategies](monthly_runs_refresh_figures/recent72_returns.png)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    args = parser.parse_args()

    raw, run_cover = base.read_all_monthly_runs(Path(args.source_root))
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable features built from monthly_runs")
    features["pred_prob_up_logit"] = rolling_logit_safe(features)
    features = add_session_labels(features)
    features = add_quality_features(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)

    logs_list: List[pd.DataFrame] = []
    summary_rows: List[Dict[str, float]] = []
    for name in build_strategy_names():
        lg, sm = simulate(features, name)
        logs_list.append(lg)
        summary_rows.append(sm)
    logs = pd.concat([x for x in logs_list if not x.empty], ignore_index=True) if logs_list else pd.DataFrame()
    summary = score_summary(pd.DataFrame(summary_rows))
    drift, recent_detail = recent_drift_forensics(logs, "v1_active_fill_mix")

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "monthly_runs_refresh_figures"
    report_dir.mkdir(parents=True, exist_ok=True)
    fig_count = make_plots(summary, fig_dir)

    coverage = run_cover.sort_values("run_name").reset_index(drop=True)
    coverage.to_csv(report_dir / "monthly_runs_coverage_snapshot.csv", index=False)
    summary.to_csv(report_dir / "monthly_runs_full_refresh_summary.csv", index=False)
    drift.to_csv(report_dir / "v1_active_fill_mix_drift_breakdown.csv", index=False)
    if not recent_detail.empty:
        recent_detail.to_csv(report_dir / "v1_active_fill_mix_recent72_detail.csv", index=False)
    (report_dir / "monthly_runs_full_refresh_report.md").write_text(build_report(coverage, summary, drift, recent_detail), encoding="utf-8")
    (report_dir / "monthly_runs_full_refresh_strategy_definitions.md").write_text(generate_definitions(), encoding="utf-8")
    (report_dir / "monthly_runs_full_refresh_meta.json").write_text(json.dumps({"rows_features": int(len(features)), "rows_logs": int(len(logs)), "rows_summary": int(len(summary)), "rows_drift": int(len(drift)), "figure_count": fig_count}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_features": int(len(features)), "rows_logs": int(len(logs)), "rows_summary": int(len(summary)), "rows_drift": int(len(drift)), "figure_count": fig_count})


if __name__ == "__main__":
    main()

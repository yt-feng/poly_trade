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
W36 = f"worst_{WINDOW_36H}_window_return"
W72 = f"worst_{WINDOW_72H}_window_return"


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


def add_micro_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    spread_up = out["spread_up_median_first2m"].fillna(1.0)
    spread_down = out["spread_down_median_first2m"].fillna(1.0)
    overround = out["overround_median_first2m"].fillna(1.0)
    buy_up_liq = out["buy_up_size_2m"].fillna(0)
    buy_down_liq = out["buy_down_size_2m"].fillna(0)
    buy_up_liq4 = out["buy_up_size_4m"].fillna(0)
    out["gate_up"] = (spread_up <= 0.05) & (overround <= 0.04) & (buy_up_liq4 >= 120)
    out["gate_down"] = (spread_down <= 0.05) & (overround <= 0.04) & (buy_down_liq >= 150)
    out["gate_up_strict"] = (spread_up <= 0.04) & (overround <= 0.03) & (buy_up_liq4 >= 150)
    out["gate_down_strict"] = (spread_down <= 0.04) & (overround <= 0.03) & (buy_down_liq >= 180)

    breakout_liq = np.clip(buy_up_liq4 / 250.0, 0, 1)
    breakout_spread = np.clip(1 - spread_up / 0.08, 0, 1)
    breakout_overround = np.clip(1 - overround / 0.06, 0, 1)
    breakout_path = np.clip(1 - (out["btc_move_4m"].fillna(0) - 40).abs() / 20.0, 0, 1)
    out["quality_breakout"] = (breakout_liq + breakout_spread + breakout_overround + breakout_path) / 4.0

    mild_liq = np.clip(buy_down_liq / 250.0, 0, 1)
    mild_spread = np.clip(1 - spread_down / 0.08, 0, 1)
    mild_overround = np.clip(1 - overround / 0.06, 0, 1)
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
    return pd.notna(row.get("btc_move_1m")) and row["btc_move_1m"] <= -20 and row.get("buy_down_size_1m", 0) >= 80


def book_consensus_down(row: pd.Series) -> bool:
    return milddrop_signal(row) and row.get("size_imbalance_updown_2m", 1.0) <= -0.10 and row.get("book_pressure_down_2m", -1.0) >= 0.0 and row.get("trade_count_sum_first2m", 0) >= 20


def pm_overshoot_fade_up(row: pd.Series) -> bool:
    return pd.notna(row.get("mid_up_prob_change_2m")) and row["mid_up_prob_change_2m"] <= -0.10 and row.get("btc_move_2m", -999) > -25 and row.get("buy_up_price_2m", 1.0) <= 0.45 and row.get("buy_up_size_2m", 0) >= 120


def ts_momentum_up(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and row["btc_move_2m"] >= 20 and row.get("path_efficiency_first2m", 0) >= 0.55 and row.get("mid_up_prob_change_2m", -1) >= 0.02 and row.get("buy_up_price_2m", 1.0) <= 0.75


def ts_reversion_down(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and row["btc_move_2m"] >= 50 and row.get("realized_vol_first2m", 0) >= 10 and row.get("buy_down_price_2m", 1.0) <= 0.30


def qfrac(q: float, lo: float, hi: float) -> float:
    q = 0.0 if pd.isna(q) else float(np.clip(q, 0, 1))
    return float(lo + (hi - lo) * q)


def choose_trade(row: pd.Series, strategy: str) -> Tuple[str, int, float, str]:
    sess = row.get("session_et")
    q_up = row.get("pred_prob_up_logit", np.nan)

    if strategy in {"v1_conservative_mix", "v1_balanced_mix", "v1_adaptive_mix", "v1_active_fill_mix", "v1_logit_overlay_mix"}:
        return v1.choose_trade(row, strategy)

    if strategy == "classic_milddrop_core":
        if sess in {"london", "us_afternoon"} and milddrop_signal(row) and row.get("gate_down", False):
            return "buy_down", 2, 0.05, f"{sess}_milddrop"
    elif strategy == "classic_breakout_core":
        if sess in {"asia", "us_open"} and breakout_signal(row) and row.get("gate_up", False):
            return "buy_up", 4, 0.05, f"{sess}_breakout"
    elif strategy == "classic_early_drop_down":
        if sess in {"london", "us_afternoon"} and early_drop_down(row) and row.get("spread_down_median_first2m", 1.0) <= 0.05:
            return "buy_down", 1, 0.04, f"{sess}_early_drop"
    elif strategy == "classic_sharpdrop_reversal":
        if sharpdrop_reversal(row) and row.get("spread_up_median_first2m", 1.0) <= 0.06:
            return "buy_up", 2, 0.04, f"{sess}_sharpdrop_rev"
    elif strategy == "microstructure_book_consensus_down":
        if sess in {"london", "us_afternoon"} and book_consensus_down(row) and row.get("gate_down", False):
            return "buy_down", 2, 0.05, f"{sess}_book_consensus"
    elif strategy == "game_pm_overshoot_fade_up":
        if pm_overshoot_fade_up(row) and row.get("spread_up_median_first2m", 1.0) <= 0.05:
            return "buy_up", 2, 0.04, f"{sess}_pm_overshoot_fade"
    elif strategy == "timeseries_momentum_up":
        if ts_momentum_up(row) and row.get("gate_up", False):
            return "buy_up", 2, 0.04, f"{sess}_ts_momentum"
    elif strategy == "timeseries_reversion_down":
        if ts_reversion_down(row) and row.get("spread_down_median_first2m", 1.0) <= 0.06:
            return "buy_down", 2, 0.04, f"{sess}_ts_reversion"
    elif strategy == "v2_strict_session_mix":
        if sess == "asia" and breakout_signal(row) and row.get("gate_up_strict", False):
            return "buy_up", 4, qfrac(row.get("quality_breakout"), 0.03, 0.07), "asia_breakout_strict"
        if sess == "london" and book_consensus_down(row) and row.get("gate_down_strict", False):
            return "buy_down", 2, qfrac(row.get("quality_milddrop"), 0.03, 0.07), "london_book_down_strict"
        if sess == "us_afternoon" and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.62:
            return "buy_down", 2, 0.05, "us_afternoon_milddrop_strict"
    elif strategy == "v2_micro_game_mix":
        if sess in {"london", "us_afternoon"} and book_consensus_down(row):
            return "buy_down", 2, 0.05, f"{sess}_book_consensus"
        if pm_overshoot_fade_up(row):
            return "buy_up", 2, 0.035, f"{sess}_pm_overshoot_fade"
        if sess == "asia" and breakout_signal(row) and row.get("gate_up", False):
            return "buy_up", 4, 0.05, "asia_breakout"
    elif strategy == "v2_time_series_mix":
        if ts_momentum_up(row) and row.get("gate_up", False):
            return "buy_up", 2, 0.04, f"{sess}_ts_momentum"
        if ts_reversion_down(row):
            return "buy_down", 2, 0.04, f"{sess}_ts_reversion"
        if sess in {"london", "us_afternoon"} and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.60:
            return "buy_down", 2, 0.04, f"{sess}_milddrop_quality"
    elif strategy == "v2_logit_value_mix":
        if pd.isna(q_up):
            return "skip", -1, np.nan, "none"
        if sess in {"london", "us_afternoon"} and milddrop_signal(row) and ((1 - q_up) - row.get("buy_down_price_2m", 1.0) > 0.06):
            return "buy_down", 2, 0.04, f"{sess}_logit_down_value"
        if breakout_signal(row) and (q_up - row.get("buy_up_price_4m", 1.0) > 0.06):
            return "buy_up", 4, 0.04, f"{sess}_logit_up_value"
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
        return {f"worst_{window}_window_return": np.nan, f"median_{window}_window_return": np.nan, f"pct_positive_{window}_windows": np.nan, f"active_{window}_window_rate": np.nan, f"num_{window}_windows": 0}
    vals, active = [], []
    for i in range(0, len(event_returns) - window + 1):
        vals.append(float(np.prod(1.0 + event_returns[i:i+window]) - 1.0))
        active.append(int(np.sum(trade_flags[i:i+window]) > 0))
    arr = np.array(vals, dtype=float)
    act = np.array(active, dtype=float)
    return {f"worst_{window}_window_return": float(np.min(arr)), f"median_{window}_window_return": float(np.median(arr)), f"pct_positive_{window}_windows": float(np.mean(arr > 0)), f"active_{window}_window_rate": float(np.mean(act > 0)), f"num_{window}_windows": int(len(arr))}


def max_drawdown_from_returns(event_returns: np.ndarray) -> float:
    wealth = np.cumprod(1.0 + event_returns)
    if len(wealth) == 0:
        return 0.0
    peak = np.maximum.accumulate(wealth)
    return float(np.nanmax((peak - wealth) / peak))


def simulate(df: pd.DataFrame, strategy: str) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll, peak, max_dd = 100.0, 100.0, 0.0
    loss_streak, cooldown = 0, 0
    event_returns = np.zeros(len(df), dtype=float)
    trade_flags = np.zeros(len(df), dtype=int)
    recent36: List[int] = []
    recent72: List[int] = []
    logs: List[Dict[str, object]] = []
    quota36 = 14 if strategy == "v1_active_fill_mix" else 10
    quota72 = 28 if strategy == "v1_active_fill_mix" else 18
    cooldown_len = 6 if strategy == "v1_active_fill_mix" else 8
    for i, row in df.iterrows():
        recent36 = [x for x in recent36 if x > i - WINDOW_36H]
        recent72 = [x for x in recent72 if x > i - WINDOW_72H]
        if cooldown > 0:
            cooldown -= 1
            continue
        if len(recent36) >= quota36 or len(recent72) >= quota72:
            continue
        side, minute, frac, component = choose_trade(row, strategy)
        if side == "skip":
            continue
        price, size_avail, pnl_per_share = payout(row, side, minute, FEE)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        bankroll_before = bankroll
        cost = min(bankroll * frac, bankroll, float(size_avail) * price)
        if cost <= 0:
            continue
        shares = cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        ret = pnl / bankroll_before if bankroll_before > 0 else 0.0
        event_returns[i] = ret
        trade_flags[i] = 1
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        recent36.append(i)
        recent72.append(i)
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= 2:
                cooldown = cooldown_len
                loss_streak = 0
        else:
            loss_streak = 0
        logs.append({"strategy": strategy, "first_quote_ts": row["first_quote_ts"], "run_name": row["run_name"], "market_id": row["market_id"], "slug": row["slug"], "session_et": row.get("session_et"), "component": component, "entry_minute": minute, "side": side, "fraction": frac, "target_cost": cost, "entry_price": price, "pnl_usd": pnl, "bankroll_after": bankroll, "event_ret": ret, "sim_max_drawdown": max_dd, "recent_tail_flag": bool(row.get("recent_tail_flag", False))})
    trade_log = pd.DataFrame(logs)
    pnl_s = pd.to_numeric(trade_log.get("pnl_usd"), errors="coerce").dropna() if not trade_log.empty else pd.Series(dtype=float)
    wins = pnl_s[pnl_s > 0].sum() if not pnl_s.empty else 0.0
    losses = pnl_s[pnl_s < 0].sum() if not pnl_s.empty else 0.0
    metrics = {"strategy": strategy, "trades": int(len(trade_log)), "ending_bankroll": float(bankroll), "total_return": float(bankroll/100 - 1), "win_rate": float((pnl_s > 0).mean()) if len(pnl_s) else np.nan, "profit_factor": float(wins / abs(losses)) if losses != 0 else np.nan, "max_drawdown": float(max_dd), **window_metrics(event_returns, trade_flags, WINDOW_36H), **window_metrics(event_returns, trade_flags, WINDOW_72H)}
    if len(df) >= WINDOW_72H:
        tail_rets = event_returns[-WINDOW_72H:]
        recent_logs = trade_log[trade_log.get("recent_tail_flag", False) == True] if not trade_log.empty else pd.DataFrame()
        recent_pnl = pd.to_numeric(recent_logs.get("pnl_usd"), errors="coerce").dropna() if not recent_logs.empty else pd.Series(dtype=float)
        metrics.update({"recent_72h_return": float(np.prod(1 + tail_rets) - 1), "recent_72h_max_drawdown": max_drawdown_from_returns(tail_rets), "recent_72h_trades": int(len(recent_logs)), "recent_72h_win_rate": float((recent_pnl > 0).mean()) if len(recent_pnl) else np.nan})
    else:
        metrics.update({"recent_72h_return": np.nan, "recent_72h_max_drawdown": np.nan, "recent_72h_trades": 0, "recent_72h_win_rate": np.nan})
    return trade_log, metrics


def recent_forensics(logs: pd.DataFrame, focus: str = "v1_active_fill_mix") -> Tuple[pd.DataFrame, pd.DataFrame]:
    s = logs[logs["strategy"] == focus].copy() if not logs.empty else pd.DataFrame()
    if s.empty:
        return pd.DataFrame(), pd.DataFrame()
    all_grp = s.groupby(["session_et", "component"], as_index=False).agg(trades_all=("market_id", "size"), pnl_all=("pnl_usd", "sum"), win_rate_all=("pnl_usd", lambda x: float((x > 0).mean())))
    recent_grp = s[s["recent_tail_flag"]].groupby(["session_et", "component"], as_index=False).agg(trades_recent=("market_id", "size"), pnl_recent=("pnl_usd", "sum"), win_rate_recent=("pnl_usd", lambda x: float((x > 0).mean())))
    out = all_grp.merge(recent_grp, on=["session_et", "component"], how="outer").fillna(0)
    out["avg_pnl_all"] = np.where(out["trades_all"] > 0, out["pnl_all"] / out["trades_all"], 0)
    out["avg_pnl_recent"] = np.where(out["trades_recent"] > 0, out["pnl_recent"] / out["trades_recent"], 0)
    out["avg_pnl_delta"] = out["avg_pnl_recent"] - out["avg_pnl_all"]
    out["win_rate_delta"] = out["win_rate_recent"] - out["win_rate_all"]
    detail = s[s["recent_tail_flag"]].sort_values("first_quote_ts")
    return out.sort_values(["pnl_recent", "avg_pnl_delta"], ascending=[True, True]), detail


def score_summary(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["score_end"] = out["ending_bankroll"].rank(pct=True)
    out["score_dd"] = (-out["max_drawdown"].fillna(999)).rank(pct=True)
    out["score_36"] = out[W36].fillna(-999).rank(pct=True)
    out["score_72"] = out[W72].fillna(-999).rank(pct=True)
    out["score_recent72"] = out["recent_72h_return"].fillna(-999).rank(pct=True)
    out["score_pf"] = out["profit_factor"].fillna(0).rank(pct=True)
    out["refreshed_score"] = 0.15*out["score_end"] + 0.20*out["score_dd"] + 0.20*out["score_36"] + 0.15*out["score_72"] + 0.25*out["score_recent72"] + 0.05*out["score_pf"]
    out["meets_dd_lt_30"] = out["max_drawdown"] < 0.30
    out["meets_recent72_positive"] = out["recent_72h_return"] > 0
    return out.sort_values(["refreshed_score", "ending_bankroll"], ascending=False).reset_index(drop=True)


def strategy_names() -> List[str]:
    return ["v1_conservative_mix", "v1_balanced_mix", "v1_adaptive_mix", "v1_active_fill_mix", "v1_logit_overlay_mix", "classic_milddrop_core", "classic_breakout_core", "classic_early_drop_down", "classic_sharpdrop_reversal", "microstructure_book_consensus_down", "game_pm_overshoot_fade_up", "timeseries_momentum_up", "timeseries_reversion_down", "v2_strict_session_mix", "v2_micro_game_mix", "v2_time_series_mix", "v2_logit_value_mix"]


def md_table(df: pd.DataFrame, rows: int = 40) -> str:
    return v1.markdown_table(df, rows=rows)


def definitions() -> str:
    return """# monthly_runs full refresh v2 strategy definitions

这份刷新专门把 5 分钟 Polymarket 事件当成微观结构问题，而不是宏观 regime 问题。

## 三类策略直觉

### 经典量化策略
- `classic_milddrop_core`：温和下跌 + Down 侧盘口质量达标，做短线延续。
- `classic_breakout_core`：第4分钟确认突破，且 Up 侧价格/流动性仍可交易，做突破延续。
- `classic_early_drop_down`：第1分钟急跌，测试早期冲击是否继续扩散。
- `classic_sharpdrop_reversal`：较大跌幅后，如果 Up 仍便宜且有流动性，测试过度反应后的反弹。

### Polymarket 博弈/盘口策略
- `microstructure_book_consensus_down`：价格下跌、Down 侧盘口压力和交易活跃度一致，代表订单簿共识延续。
- `game_pm_overshoot_fade_up`：Polymarket Up 概率被快速压低，但 BTC 实际跌幅并不极端，测试 crowding/情绪挤压后的概率回摆。

### 时间序列策略
- `timeseries_momentum_up`：前2分钟路径效率高、Polymarket 概率同向上移，做短线动量延续。
- `timeseries_reversion_down`：前2分钟涨幅和波动都偏大，且 Down 赔率便宜，做短线均值回归。
- `v2_time_series_mix`：把动量、回归和高质量 milddrop 拼成组合。

## 重新精选目标

评分同时考虑：全历史收益、最大回撤、36小时最差窗口、72小时最差窗口、最近72小时收益、profit factor。
"""


def make_plots(summary: pd.DataFrame, fig_dir: Path) -> int:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return 0
    fig_dir.mkdir(parents=True, exist_ok=True)
    top = summary.sort_values("refreshed_score", ascending=False).head(12)
    if top.empty:
        return 0
    plt.figure(figsize=(14, 5))
    plt.bar(top["strategy"], top["refreshed_score"])
    plt.xticks(rotation=45, ha="right")
    plt.title("Refreshed strategy score")
    plt.tight_layout(); plt.savefig(fig_dir / "refreshed_scores.png", dpi=150); plt.close()
    plt.figure(figsize=(14, 5))
    plt.bar(top["strategy"], top["recent_72h_return"])
    plt.xticks(rotation=45, ha="right")
    plt.title("Recent 72h return")
    plt.tight_layout(); plt.savefig(fig_dir / "recent72_returns.png", dpi=150); plt.close()
    return 2


def report_md(coverage: pd.DataFrame, summary: pd.DataFrame, drift: pd.DataFrame, detail: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# monthly_runs 全量刷新 v2：微观结构 / 博弈 / 时间序列策略重选")
    lines.append("")
    lines.append("这版使用当前 `data/monthly_runs/*` 的最新全量数据。这里不再把 5 分钟短线简单称为宏观 regime shift，而是复盘微观结构状态变化：盘口深度、spread、overround、订单簿共识、概率过度反应和时间序列路径。")
    lines.append("")
    lines.append("## 当前 monthly_runs 覆盖")
    lines.append(md_table(coverage, rows=120))
    lines.append("")
    lines.append("## v1_active_fill_mix 最近72小时按组件复盘")
    lines.append(md_table(drift, rows=60))
    lines.append("")
    lines.append("## v1_active_fill_mix 最近72小时交易明细")
    lines.append(md_table(detail, rows=120))
    lines.append("")
    lines.append("## 重新精选 Top 20")
    lines.append(md_table(summary, rows=20))
    lines.append("")
    lines.append("## 满足回撤 < 30% 且 recent72h 为正 的候选")
    lines.append(md_table(summary[(summary["meets_dd_lt_30"]) & (summary["meets_recent72_positive"])], rows=20))
    lines.append("")
    if not summary.empty:
        b = summary.iloc[0]
        lines.append("## 当前首选")
        lines.append(f"- 策略：**{b['strategy']}**")
        lines.append(f"- 期末本金：**{b['ending_bankroll']:.2f} USD**")
        lines.append(f"- 最大回撤：**{b['max_drawdown']:.2%}**")
        lines.append(f"- 最近72小时收益：**{b['recent_72h_return']:.2%}**")
        lines.append(f"- 36小时最差窗口：**{b[W36]:.2%}**")
        lines.append(f"- 72小时最差窗口：**{b[W72]:.2%}**")
    lines.append("")
    lines.append("## 图表")
    lines.append("![Refreshed scores](monthly_runs_refresh_figures/refreshed_scores.png)")
    lines.append("")
    lines.append("![Recent 72h returns](monthly_runs_refresh_figures/recent72_returns.png)")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--source-root", required=True)
    p.add_argument("--report-dir", required=True)
    args = p.parse_args()
    raw, cover = base.read_all_monthly_runs(Path(args.source_root))
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable features built from monthly_runs")
    features["pred_prob_up_logit"] = rolling_logit_safe(features)
    features = add_session_labels(features)
    features = add_micro_features(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    logs_list: List[pd.DataFrame] = []
    rows: List[Dict[str, float]] = []
    for name in strategy_names():
        lg, sm = simulate(features, name)
        logs_list.append(lg)
        rows.append(sm)
    logs = pd.concat([x for x in logs_list if not x.empty], ignore_index=True) if logs_list else pd.DataFrame()
    summary = score_summary(pd.DataFrame(rows))
    drift, detail = recent_forensics(logs)
    outdir = Path(args.report_dir); outdir.mkdir(parents=True, exist_ok=True)
    fig_count = make_plots(summary, outdir / "monthly_runs_refresh_figures")
    coverage = cover.sort_values("run_name").reset_index(drop=True)
    coverage.to_csv(outdir / "monthly_runs_coverage_snapshot.csv", index=False)
    summary.to_csv(outdir / "monthly_runs_full_refresh_summary.csv", index=False)
    drift.to_csv(outdir / "v1_active_fill_mix_drift_breakdown.csv", index=False)
    detail.to_csv(outdir / "v1_active_fill_mix_recent72_detail.csv", index=False)
    (outdir / "monthly_runs_full_refresh_report.md").write_text(report_md(coverage, summary, drift, detail), encoding="utf-8")
    (outdir / "monthly_runs_full_refresh_strategy_definitions.md").write_text(definitions(), encoding="utf-8")
    (outdir / "monthly_runs_full_refresh_meta.json").write_text(json.dumps({"rows_features": int(len(features)), "rows_logs": int(len(logs)), "rows_summary": int(len(summary)), "figure_count": fig_count}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_features": int(len(features)), "rows_logs": int(len(logs)), "rows_summary": int(len(summary)), "figure_count": fig_count})


if __name__ == "__main__":
    main()

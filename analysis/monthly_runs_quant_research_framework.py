from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
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
BANKROLL0 = 100.0
WINDOW_36H = 144
WINDOW_72H = 864


@dataclass(frozen=True)
class StrategySpec:
    name: str
    family: str
    intuition: str
    failure_mode: str


def rolling_logit_safe(df: pd.DataFrame, min_history: int = 120, retrain_every: int = 30, lookback: int = 1400) -> pd.Series:
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
            if len(hist) < 60:
                next_retrain = i + retrain_every
                continue
            y = hist["outcome_up"].astype(int)
            if y.nunique() < 2:
                next_retrain = i + retrain_every
                continue
            X = hist[base.LOGIT_FEATURES].copy()
            med = X.median(numeric_only=True)
            X = X.fillna(med)
            weights = 0.5 ** ((len(hist) - 1 - np.arange(len(hist))) / 250.0)
            try:
                model = LogisticRegression(max_iter=1200)
                model.fit(X, y, sample_weight=weights)
            except Exception:
                model = None
            next_retrain = i + retrain_every
        if model is not None and med is not None:
            try:
                x = df.iloc[[i]][base.LOGIT_FEATURES].copy().fillna(med)
                probs[i] = float(model.predict_proba(x)[:, 1][0])
            except Exception:
                pass
    return pd.Series(probs, index=df.index)


def prepare_features(source_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    raw, cover = base.read_all_monthly_runs(source_root)
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable event features built from monthly_runs")
    features["pred_prob_up_logit"] = rolling_logit_safe(features)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = add_research_features(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    return features, cover.sort_values("run_name").reset_index(drop=True)


def add_research_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["gate_up"] = (
        (out["spread_up_median_first2m"].fillna(1.0) <= 0.05)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.04)
        & (out["buy_up_size_2m"].fillna(0) >= 120)
    )
    out["gate_down"] = (
        (out["spread_down_median_first2m"].fillna(1.0) <= 0.05)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.04)
        & (out["buy_down_size_2m"].fillna(0) >= 150)
    )
    out["gate_up_strict"] = (
        (out["spread_up_median_first2m"].fillna(1.0) <= 0.04)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.03)
        & (out["buy_up_size_2m"].fillna(0) >= 180)
    )
    out["gate_down_strict"] = (
        (out["spread_down_median_first2m"].fillna(1.0) <= 0.04)
        & (out["overround_median_first2m"].fillna(1.0) <= 0.03)
        & (out["buy_down_size_2m"].fillna(0) >= 180)
    )
    out["pm_up_crash_without_btc_crash"] = (
        (out["mid_up_prob_change_2m"].fillna(0) <= -0.10)
        & (out["btc_move_2m"].fillna(-999) > -25)
    )
    out["pm_up_squeeze_without_btc_squeeze"] = (
        (out["mid_up_prob_change_2m"].fillna(0) >= 0.10)
        & (out["btc_move_2m"].fillna(999) < 25)
    )
    out["recent_tail_flag"] = False
    if len(out) >= WINDOW_72H:
        out.loc[out.index[-WINDOW_72H:], "recent_tail_flag"] = True
    return out


def split_dataset(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    n = len(df)
    i1 = max(1, int(n * 0.50))
    i2 = max(i1 + 1, int(n * 0.75))
    parts = {
        "train": df.iloc[:i1].copy(),
        "validation": df.iloc[i1:i2].copy(),
        "test": df.iloc[i2:].copy(),
        "full": df.copy(),
    }
    parts["latest72h"] = df.iloc[-WINDOW_72H:].copy() if n >= WINDOW_72H else df.copy()
    return parts


def breakout(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_4m")) and 30 < row["btc_move_4m"] <= 50 and row.get("buy_up_price_4m", 1.0) <= 0.90


def milddrop(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and -30 < row["btc_move_2m"] <= -10 and row.get("size_imbalance_updown_2m", 1.0) <= 0 and row.get("book_pressure_down_2m", -1.0) >= -0.2


def early_drop(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_1m")) and row["btc_move_1m"] <= -20


def sharpdrop_reversal(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and -50 < row["btc_move_2m"] <= -30 and row.get("buy_up_price_2m", 1.0) <= 0.45


def extreme_up_fade(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and row["btc_move_2m"] >= 50 and row.get("buy_down_price_2m", 1.0) <= 0.30


def book_consensus_down(row: pd.Series) -> bool:
    return milddrop(row) and row.get("size_imbalance_updown_2m", 1.0) <= -0.10 and row.get("book_pressure_down_2m", -1.0) >= 0.0 and row.get("trade_count_sum_first2m", 0) >= 20


def book_consensus_up(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and row["btc_move_2m"] >= 10 and row.get("size_imbalance_updown_2m", -1.0) >= 0.10 and row.get("book_pressure_up_2m", -1.0) >= 0.0 and row.get("buy_up_price_2m", 1.0) <= 0.75


def ts_momentum_up(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and row["btc_move_2m"] >= 20 and row.get("path_efficiency_first2m", 0) >= 0.55 and row.get("mid_up_prob_change_2m", 0) >= 0.02 and row.get("buy_up_price_2m", 1.0) <= 0.75


def ts_momentum_down(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and row["btc_move_2m"] <= -15 and row.get("path_efficiency_first2m", 0) >= 0.50 and row.get("mid_up_prob_change_2m", 0) <= -0.02 and row.get("buy_down_price_2m", 1.0) <= 0.75


def choose_trade(row: pd.Series, name: str) -> Tuple[str, int, float, str]:
    sess = row.get("session_et")
    q_up = row.get("pred_prob_up_logit", np.nan)
    if name in {"v1_conservative_mix", "v1_balanced_mix", "v1_adaptive_mix", "v1_active_fill_mix", "v1_logit_overlay_mix"}:
        return v1.choose_trade(row, name)
    if name == "classic_milddrop_down":
        if milddrop(row) and row.get("gate_down", False):
            return "buy_down", 2, 0.05, "milddrop_down"
    elif name == "classic_breakout_up":
        if breakout(row) and row.get("gate_up", False):
            return "buy_up", 4, 0.05, "breakout_up"
    elif name == "classic_early_drop_down":
        if early_drop(row) and row.get("spread_down_median_first2m", 1.0) <= 0.06:
            return "buy_down", 1, 0.04, "early_drop_down"
    elif name == "classic_sharpdrop_reversal_up":
        if sharpdrop_reversal(row) and row.get("spread_up_median_first2m", 1.0) <= 0.06:
            return "buy_up", 2, 0.04, "sharpdrop_reversal_up"
    elif name == "classic_extremeup_fade_down":
        if extreme_up_fade(row) and row.get("spread_down_median_first2m", 1.0) <= 0.06:
            return "buy_down", 2, 0.04, "extremeup_fade_down"
    elif name == "micro_book_consensus_down":
        if sess in {"london", "us_afternoon", "other"} and book_consensus_down(row) and row.get("gate_down", False):
            return "buy_down", 2, 0.05, "book_consensus_down"
    elif name == "micro_book_consensus_up":
        if sess in {"asia", "us_open", "other"} and book_consensus_up(row) and row.get("gate_up", False):
            return "buy_up", 2, 0.04, "book_consensus_up"
    elif name == "game_pm_overshoot_fade_up":
        if row.get("pm_up_crash_without_btc_crash", False) and row.get("spread_up_median_first2m", 1.0) <= 0.05 and row.get("buy_up_size_2m", 0) >= 120:
            return "buy_up", 2, 0.035, "pm_overshoot_fade_up"
    elif name == "game_pm_squeeze_fade_down":
        if row.get("pm_up_squeeze_without_btc_squeeze", False) and row.get("spread_down_median_first2m", 1.0) <= 0.05 and row.get("buy_down_size_2m", 0) >= 120:
            return "buy_down", 2, 0.035, "pm_squeeze_fade_down"
    elif name == "timeseries_momentum_up":
        if ts_momentum_up(row) and row.get("gate_up", False):
            return "buy_up", 2, 0.04, "ts_momentum_up"
    elif name == "timeseries_momentum_down":
        if ts_momentum_down(row) and row.get("gate_down", False):
            return "buy_down", 2, 0.04, "ts_momentum_down"
    elif name == "timeseries_reversion_down":
        if extreme_up_fade(row):
            return "buy_down", 2, 0.035, "ts_reversion_down"
    elif name == "ml_logit_edge_06":
        if pd.notna(q_up):
            if row.get("gate_up", False) and q_up - row.get("buy_up_price_2m", 1.0) > 0.06:
                return "buy_up", 2, 0.04, "logit_edge_up"
            if row.get("gate_down", False) and (1 - q_up) - row.get("buy_down_price_2m", 1.0) > 0.06:
                return "buy_down", 2, 0.04, "logit_edge_down"
    elif name == "ml_logit_edge_10":
        if pd.notna(q_up):
            if row.get("gate_up_strict", False) and q_up - row.get("buy_up_price_2m", 1.0) > 0.10:
                return "buy_up", 2, 0.035, "logit_edge_up_strict"
            if row.get("gate_down_strict", False) and (1 - q_up) - row.get("buy_down_price_2m", 1.0) > 0.10:
                return "buy_down", 2, 0.035, "logit_edge_down_strict"
    elif name == "portfolio_micro_ts_mix":
        if book_consensus_down(row) and row.get("gate_down", False):
            return "buy_down", 2, 0.045, "book_consensus_down"
        if ts_momentum_up(row) and row.get("gate_up", False):
            return "buy_up", 2, 0.035, "ts_momentum_up"
        if row.get("pm_up_crash_without_btc_crash", False) and row.get("spread_up_median_first2m", 1.0) <= 0.05:
            return "buy_up", 2, 0.03, "pm_overshoot_fade_up"
    elif name == "portfolio_conservative_v2":
        if sess == "asia" and breakout(row) and row.get("gate_up_strict", False):
            return "buy_up", 4, 0.04, "asia_breakout_strict"
        if sess == "london" and book_consensus_down(row) and row.get("gate_down_strict", False):
            return "buy_down", 2, 0.04, "london_book_down_strict"
        if sess == "us_afternoon" and milddrop(row) and row.get("quality_milddrop", 0) >= 0.62:
            return "buy_down", 2, 0.045, "us_afternoon_milddrop"
    return "skip", -1, np.nan, "none"


def strategy_registry() -> List[StrategySpec]:
    return [
        StrategySpec("v1_active_fill_mix", "previous_v1", "旧版最优高覆盖组合；检验是否在新数据中仍然有效。", "覆盖率高，在坏盘口状态下会继续交易。"),
        StrategySpec("v1_conservative_mix", "previous_v1", "旧版保守组合；减少 filler 控制回撤。", "可能交易太少。"),
        StrategySpec("v1_balanced_mix", "previous_v1", "旧版平衡组合；收益与覆盖折中。", "可能受旧阈值影响。"),
        StrategySpec("v1_adaptive_mix", "previous_v1", "旧版按 quality 调仓。", "quality 分数若失真，仓位仍可能不准。"),
        StrategySpec("v1_logit_overlay_mix", "previous_v1", "旧版概率模型过滤。", "edge 阈值过严可能无交易。"),
        StrategySpec("classic_milddrop_down", "classic_quant", "温和下跌且盘口支持 Down，做短线延续。", "市场快速反弹时失效。"),
        StrategySpec("classic_breakout_up", "classic_quant", "第4分钟确认突破且 Up 未过贵，追随慢扩散。", "突破可能是最后一棒。"),
        StrategySpec("classic_early_drop_down", "classic_quant", "第1分钟急跌代表早期冲击，测试继续扩散。", "急跌后立刻均值回归。"),
        StrategySpec("classic_sharpdrop_reversal_up", "classic_quant", "较大跌幅后，Up 便宜且有流动性时做过度反应回摆。", "趋势下跌时持续亏。"),
        StrategySpec("classic_extremeup_fade_down", "classic_quant", "极端上涨后买便宜 Down，测试追涨拥挤回吐。", "强趋势日被持续挤压。"),
        StrategySpec("micro_book_consensus_down", "microstructure", "价格、订单簿压力和成交活跃度都指向 Down，做订单簿共识延续。", "盘口共识可能已被价格充分反映。"),
        StrategySpec("micro_book_consensus_up", "microstructure", "订单簿压力和价格路径都指向 Up，做盘口共识延续。", "Up 价格太贵时 edge 被吃掉。"),
        StrategySpec("game_pm_overshoot_fade_up", "game_theory", "Polymarket Up 概率被快速压低但 BTC 未同步崩，赌 crowding 后回摆。", "PM 可能比 BTC 更早发现信息。"),
        StrategySpec("game_pm_squeeze_fade_down", "game_theory", "Polymarket Up 概率快速上挤但 BTC 未跟随，赌拥挤追单回吐。", "BTC 随后补涨。"),
        StrategySpec("timeseries_momentum_up", "time_series", "路径效率高且 PM 概率同向上移，做短线动量。", "最后几分钟反转。"),
        StrategySpec("timeseries_momentum_down", "time_series", "路径效率高且 PM 概率同向下移，做短线下行动量。", "流动性突然修复。"),
        StrategySpec("timeseries_reversion_down", "time_series", "大涨且 Down 便宜时做短线均值回归。", "趋势强时被挤压。"),
        StrategySpec("ml_logit_edge_06", "ml_value", "滚动逻辑回归估 fair probability，edge > 6% 才交易。", "模型边际不稳定。"),
        StrategySpec("ml_logit_edge_10", "ml_value", "更保守的滚动逻辑回归 edge 策略。", "交易太少。"),
        StrategySpec("portfolio_micro_ts_mix", "portfolio", "订单簿共识、时间序列动量和 PM 过度反应组合。", "多个弱信号相关性上升时一起失效。"),
        StrategySpec("portfolio_conservative_v2", "portfolio", "严格 gate 的 session-aware 组合，目标替代 v1_active_fill_mix。", "过度保守导致收益不足。"),
    ]


def payout(row: pd.Series, side: str, minute: int) -> Tuple[float, float, float]:
    if side == "buy_up":
        price = row[f"buy_up_price_{minute}m"]
        size = row.get(f"buy_up_size_{minute}m", np.nan)
        pnl_per_share = row["outcome_up"] - price - FEE
    else:
        price = row[f"buy_down_price_{minute}m"]
        size = row.get(f"buy_down_size_{minute}m", np.nan)
        pnl_per_share = (1.0 - row["outcome_up"]) - price - FEE
    return float(price), float(size), float(pnl_per_share)


def window_metrics(event_returns: np.ndarray, trade_flags: np.ndarray, window: int) -> Dict[str, float]:
    if len(event_returns) < window:
        return {f"worst_{window}_return": np.nan, f"median_{window}_return": np.nan, f"positive_{window}_rate": np.nan, f"active_{window}_rate": np.nan}
    vals, active = [], []
    for i in range(0, len(event_returns) - window + 1):
        vals.append(float(np.prod(1.0 + event_returns[i:i+window]) - 1.0))
        active.append(float(np.sum(trade_flags[i:i+window]) > 0))
    arr = np.array(vals, dtype=float)
    act = np.array(active, dtype=float)
    return {f"worst_{window}_return": float(np.min(arr)), f"median_{window}_return": float(np.median(arr)), f"positive_{window}_rate": float(np.mean(arr > 0)), f"active_{window}_rate": float(np.mean(act > 0))}


def simulate(df: pd.DataFrame, name: str) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll, peak, max_dd = BANKROLL0, BANKROLL0, 0.0
    event_returns = np.zeros(len(df), dtype=float)
    trade_flags = np.zeros(len(df), dtype=int)
    logs, recent36 = [], []
    loss_streak, cooldown = 0, 0
    quota36 = 14 if name == "v1_active_fill_mix" else 10
    cooldown_len = 6 if name == "v1_active_fill_mix" else 8
    for i, row in df.reset_index(drop=True).iterrows():
        recent36 = [x for x in recent36 if x > i - WINDOW_36H]
        if cooldown > 0:
            cooldown -= 1
            continue
        if len(recent36) >= quota36:
            continue
        side, minute, frac, component = choose_trade(row, name)
        if side == "skip":
            continue
        price, size_avail, pnl_per_share = payout(row, side, minute)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        before = bankroll
        cost = min(bankroll * frac, bankroll, float(size_avail) * price)
        if cost <= 0:
            continue
        pnl = (cost / price) * pnl_per_share
        bankroll += pnl
        ret = pnl / before if before > 0 else 0.0
        event_returns[i] = ret
        trade_flags[i] = 1
        recent36.append(i)
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= 2:
                cooldown = cooldown_len
                loss_streak = 0
        else:
            loss_streak = 0
        logs.append({"strategy": name, "first_quote_ts": row["first_quote_ts"], "run_name": row["run_name"], "market_id": row["market_id"], "session_et": row.get("session_et"), "component": component, "side": side, "entry_minute": minute, "fraction": frac, "target_cost": cost, "entry_price": price, "pnl_usd": pnl, "event_ret": ret, "bankroll_after": bankroll, "recent_tail_flag": bool(row.get("recent_tail_flag", False))})
    trade_log = pd.DataFrame(logs)
    pnl_s = pd.to_numeric(trade_log.get("pnl_usd"), errors="coerce").dropna() if not trade_log.empty else pd.Series(dtype=float)
    wins = pnl_s[pnl_s > 0].sum() if not pnl_s.empty else 0.0
    losses = pnl_s[pnl_s < 0].sum() if not pnl_s.empty else 0.0
    metrics = {"strategy": name, "trades": int(len(trade_log)), "ending_bankroll": float(bankroll), "total_return": float(bankroll / BANKROLL0 - 1.0), "max_drawdown": float(max_dd), "win_rate": float((pnl_s > 0).mean()) if len(pnl_s) else np.nan, "profit_factor": float(wins / abs(losses)) if losses != 0 else np.nan, **window_metrics(event_returns, trade_flags, WINDOW_36H), **window_metrics(event_returns, trade_flags, WINDOW_72H)}
    return trade_log, metrics


def evaluate_all(splits: Dict[str, pd.DataFrame], specs: List[StrategySpec]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rows, logs = [], []
    family = {s.name: s.family for s in specs}
    for split_name, split_df in splits.items():
        for spec in specs:
            lg, met = simulate(split_df, spec.name)
            met["split"] = split_name
            met["family"] = family[spec.name]
            rows.append(met)
            if not lg.empty:
                lg["split"] = split_name
                logs.append(lg)
    return pd.DataFrame(rows), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def score_validation(metrics: pd.DataFrame) -> pd.DataFrame:
    val = metrics[metrics["split"] == "validation"].copy()
    if val.empty:
        return pd.DataFrame()
    val["score_return"] = val["total_return"].rank(pct=True)
    val["score_dd"] = (-val["max_drawdown"].fillna(999)).rank(pct=True)
    val["score_pf"] = val["profit_factor"].fillna(0).rank(pct=True)
    val["score_w36"] = val["worst_144_return"].fillna(-999).rank(pct=True)
    val["validation_score"] = 0.35 * val["score_return"] + 0.25 * val["score_dd"] + 0.20 * val["score_pf"] + 0.20 * val["score_w36"]
    return val.sort_values("validation_score", ascending=False).reset_index(drop=True)


def selected_summary(metrics: pd.DataFrame, val_rank: pd.DataFrame) -> pd.DataFrame:
    if val_rank.empty:
        return pd.DataFrame()
    selected = val_rank.iloc[0]["strategy"]
    out = metrics[metrics["strategy"] == selected].copy()
    out.insert(0, "selected_by_validation", selected)
    return out


def definitions_markdown(specs: List[StrategySpec]) -> str:
    lines = ["# monthly_runs quant research framework strategy registry", "", "每个策略都有 family、交易直觉和主要失效方式。", ""]
    for s in specs:
        lines.append(f"## {s.name}")
        lines.append(f"- family: `{s.family}`")
        lines.append(f"- intuition: {s.intuition}")
        lines.append(f"- likely failure mode: {s.failure_mode}")
        lines.append("")
    lines.append("## anti-overfitting protocol")
    lines.append("- 按时间顺序切分：train 50%，validation 25%，test 25%。")
    lines.append("- 策略选择只看 validation score。")
    lines.append("- test 是盲测，不参与策略选择。")
    lines.append("- latest72h 只作为最新健康检查，不用于调参。")
    return "\n".join(lines)


def md_table(df: pd.DataFrame, n: int = 30) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(n).copy()
    nums = show.select_dtypes(include=[np.number]).columns
    show[nums] = show[nums].round(4)
    return show.to_markdown(index=False)


def build_report(cover: pd.DataFrame, metrics: pd.DataFrame, val_rank: pd.DataFrame, selected: pd.DataFrame, logs: pd.DataFrame) -> str:
    latest = metrics[metrics["split"] == "latest72h"].copy().sort_values("total_return", ascending=False)
    test = metrics[metrics["split"] == "test"].copy().sort_values("total_return", ascending=False)
    full = metrics[metrics["split"] == "full"].copy().sort_values("total_return", ascending=False)
    lines = ["# monthly_runs 完整量化研究框架", ""]
    lines.append("这份报告重新跑旧策略、经典量化策略、Polymarket 博弈/盘口策略、时间序列策略和 ML edge 策略。")
    lines.append("")
    lines.append("## 数据覆盖")
    lines.append(md_table(cover, 120))
    lines.append("")
    lines.append("## 防过拟合协议")
    lines.append("- 时间顺序切分：train 50%，validation 25%，test 25%。")
    lines.append("- 只用 validation 选择策略，test 只做最终盲测。")
    lines.append("- latest72h 只作为健康检查。")
    lines.append("")
    lines.append("## validation 排名 Top 20")
    lines.append(md_table(val_rank, 20))
    lines.append("")
    lines.append("## validation 选出的策略在各 split 上的表现")
    lines.append(md_table(selected, 10))
    lines.append("")
    lines.append("## test 盲测 Top 20")
    lines.append(md_table(test, 20))
    lines.append("")
    lines.append("## latest72h 健康检查 Top 20")
    lines.append(md_table(latest, 20))
    lines.append("")
    lines.append("## full 全量表现 Top 20")
    lines.append(md_table(full, 20))
    lines.append("")
    if not val_rank.empty:
        chosen = val_rank.iloc[0]["strategy"]
        recent_logs = logs[(logs["strategy"] == chosen) & (logs["split"] == "latest72h")].copy() if not logs.empty else pd.DataFrame()
        lines.append(f"## 被 validation 选中的策略 `{chosen}` 的 latest72h 交易明细")
        lines.append(md_table(recent_logs.sort_values("first_quote_ts"), 80))
    return "\n".join(lines)


def make_plots(metrics: pd.DataFrame, outdir: Path) -> int:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return 0
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for split in ["validation", "test", "latest72h"]:
        sub = metrics[metrics["split"] == split].sort_values("total_return", ascending=False).head(15)
        if sub.empty:
            continue
        plt.figure(figsize=(14, 5))
        plt.bar(sub["strategy"], sub["total_return"])
        plt.xticks(rotation=55, ha="right")
        plt.title(f"{split} total return top strategies")
        plt.tight_layout()
        plt.savefig(figdir / f"{split}_returns.png", dpi=150)
        plt.close()
        n += 1
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args()
    features, cover = prepare_features(Path(args.source_root))
    splits = split_dataset(features)
    specs = strategy_registry()
    metrics, logs = evaluate_all(splits, specs)
    val_rank = score_validation(metrics)
    selected = selected_summary(metrics, val_rank)
    outdir = Path(args.report_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    fig_count = make_plots(metrics, outdir)
    cover.to_csv(outdir / "data_coverage.csv", index=False)
    metrics.to_csv(outdir / "strategy_split_metrics.csv", index=False)
    val_rank.to_csv(outdir / "validation_strategy_ranking.csv", index=False)
    selected.to_csv(outdir / "selected_strategy_split_performance.csv", index=False)
    if not logs.empty:
        logs.to_csv(outdir / "strategy_trade_logs.csv", index=False)
    (outdir / "strategy_registry_definitions.md").write_text(definitions_markdown(specs), encoding="utf-8")
    (outdir / "quant_research_framework_report.md").write_text(build_report(cover, metrics, val_rank, selected, logs), encoding="utf-8")
    (outdir / "meta.json").write_text(json.dumps({"rows_features": int(len(features)), "rows_metrics": int(len(metrics)), "rows_logs": int(len(logs)), "figure_count": fig_count}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_features": int(len(features)), "rows_metrics": int(len(metrics)), "rows_logs": int(len(logs)), "figure_count": fig_count})


if __name__ == "__main__":
    main()

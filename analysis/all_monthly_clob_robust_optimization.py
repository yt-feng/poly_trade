from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
from sklearn.linear_model import LogisticRegression

ROLLING_WINDOW_EVENTS = 48
FEE = 0.01
FIXED_SIZES = [0.06, 0.08, 0.10, 0.12]


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
    return out


def breakout_signal(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_4m")) and 30 < row["btc_move_4m"] <= 50 and row.get("buy_up_price_4m", 1.0) <= 0.90 and row.get("buy_up_size_4m", 0) >= 100


def milddrop_signal(row: pd.Series) -> bool:
    return pd.notna(row.get("btc_move_2m")) and -30 < row["btc_move_2m"] <= -10 and row.get("size_imbalance_updown_2m", 1.0) <= 0 and row.get("book_pressure_down_2m", -1.0) >= -0.2


def signal_from_strategy(row: pd.Series, strategy: str) -> Tuple[str, int, float]:
    # returns side, minute, quality
    if strategy.startswith("session_breakout_"):
        session = strategy.split("session_breakout_")[1].split("_fixed_")[0]
        if row.get("session_et") == session and breakout_signal(row):
            return "buy_up", 4, float(row.get("quality_breakout", 0.0))
        return "skip", -1, np.nan
    if strategy.startswith("session_milddrop_"):
        session = strategy.split("session_milddrop_")[1].split("_fixed_")[0]
        if row.get("session_et") == session and milddrop_signal(row):
            return "buy_down", 2, float(row.get("quality_milddrop", 0.0))
        return "skip", -1, np.nan
    if strategy.startswith("gate_breakout_fixed_"):
        if breakout_signal(row) and bool(row.get("triple_gate_up", False)):
            return "buy_up", 4, float(row.get("quality_breakout", 0.0))
        return "skip", -1, np.nan
    if strategy.startswith("gate_milddrop_fixed_"):
        if milddrop_signal(row) and bool(row.get("triple_gate_down", False)):
            return "buy_down", 2, float(row.get("quality_milddrop", 0.0))
        return "skip", -1, np.nan
    if strategy.startswith("combo_guarded_fixed_") or strategy == "combo_guarded_adaptive":
        # prefer breakout during US sessions, milddrop during Asia/London if gates hold
        if row.get("session_et") in {"us_open", "us_afternoon"} and breakout_signal(row) and bool(row.get("triple_gate_up", False)):
            return "buy_up", 4, float(row.get("quality_breakout", 0.0))
        if row.get("session_et") in {"asia", "london"} and milddrop_signal(row) and bool(row.get("triple_gate_down", False)):
            return "buy_down", 2, float(row.get("quality_milddrop", 0.0))
        return "skip", -1, np.nan
    if strategy.startswith("combo_session_mix_fixed_") or strategy == "combo_session_mix_adaptive":
        if row.get("session_et") == "us_open" and breakout_signal(row) and bool(row.get("triple_gate_up", False)):
            return "buy_up", 4, float(row.get("quality_breakout", 0.0))
        if row.get("session_et") in {"london", "asia"} and milddrop_signal(row) and bool(row.get("triple_gate_down", False)) and row.get("quality_milddrop", 0) >= 0.55:
            return "buy_down", 2, float(row.get("quality_milddrop", 0.0))
        return "skip", -1, np.nan
    return "skip", -1, np.nan


def size_fraction(strategy: str, quality: float) -> float:
    if strategy.endswith("_adaptive"):
        q = 0.0 if pd.isna(quality) else float(np.clip(quality, 0, 1))
        return float(np.clip(0.04 + 0.08 * q, 0.04, 0.12))
    if "_fixed_" in strategy:
        frag = strategy.split("_fixed_")[-1]
        return float(frag.replace("pct", "")) / 100.0
    return 0.08


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


def rolling_window_metrics(event_returns: np.ndarray, window: int = ROLLING_WINDOW_EVENTS) -> Dict[str, float]:
    n = len(event_returns)
    if n < window:
        return {
            "worst_48_window_return": np.nan,
            "median_48_window_return": np.nan,
            "pct_positive_48_windows": np.nan,
            "pct_nonnegative_48_windows": np.nan,
            "num_48_windows": 0,
        }
    vals = []
    for i in range(0, n - window + 1):
        window_ret = float(np.prod(1.0 + event_returns[i:i+window]) - 1.0)
        vals.append(window_ret)
    arr = np.array(vals, dtype=float)
    return {
        "worst_48_window_return": float(np.min(arr)),
        "median_48_window_return": float(np.median(arr)),
        "pct_positive_48_windows": float(np.mean(arr > 0)),
        "pct_nonnegative_48_windows": float(np.mean(arr >= 0)),
        "num_48_windows": int(len(arr)),
    }


def simulate_strategy(df: pd.DataFrame, strategy: str, fee: float = FEE) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = 100.0
    peak = 100.0
    max_dd = 0.0
    cooldown = 0
    loss_streak = 0
    recent_trade_indices: List[int] = []
    logs: List[Dict[str, object]] = []
    event_returns = np.zeros(len(df), dtype=float)

    for i, row in df.iterrows():
        recent_trade_indices = [x for x in recent_trade_indices if x > i - ROLLING_WINDOW_EVENTS]
        if cooldown > 0:
            cooldown -= 1
            continue

        side, minute, quality = signal_from_strategy(row, strategy)
        if side == "skip":
            continue

        # overlay: trade quota and post-loss cooldown
        max_trades_48 = 6 if strategy.startswith("combo_guarded") else 8
        if len(recent_trade_indices) >= max_trades_48:
            continue

        frac = size_fraction(strategy, quality)
        price, size_avail, pnl_per_share = payout(row, side, minute, fee)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        target_cost = min(bankroll * frac, bankroll, float(size_avail) * float(price))
        if target_cost <= 0:
            continue
        bankroll_before = bankroll
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        event_ret = pnl / bankroll_before if bankroll_before > 0 else 0.0
        event_returns[i] = event_ret
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        recent_trade_indices.append(i)
        if pnl < 0:
            loss_streak += 1
            if strategy.startswith("combo_") and loss_streak >= 2:
                cooldown = 6
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
            "entry_minute": minute,
            "side": side,
            "quality": quality,
            "fraction": frac,
            "target_cost": target_cost,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
            "event_ret": event_ret,
            "sim_max_drawdown": max_dd,
        })

    trade_log = pd.DataFrame(logs)
    pnl = pd.to_numeric(trade_log.get("pnl_usd"), errors="coerce").dropna() if not trade_log.empty else pd.Series(dtype=float)
    cost = pd.to_numeric(trade_log.get("target_cost"), errors="coerce") if not trade_log.empty else pd.Series(dtype=float)
    rtn = (pnl / cost).replace([np.inf, -np.inf], np.nan).dropna() if not trade_log.empty else pd.Series(dtype=float)
    wins = pnl[pnl > 0].sum() if not pnl.empty else 0.0
    losses = pnl[pnl < 0].sum() if not pnl.empty else 0.0
    roll = rolling_window_metrics(event_returns)
    summary = {
        "strategy": strategy,
        "trades": int(len(trade_log)),
        "ending_bankroll": float(bankroll),
        "total_return": float(bankroll / 100.0 - 1.0),
        "avg_trade_return_on_cost": float(rtn.mean()) if len(rtn) else np.nan,
        "median_trade_return_on_cost": float(rtn.median()) if len(rtn) else np.nan,
        "win_rate": float((pnl > 0).mean()) if len(pnl) else np.nan,
        "profit_factor": np.nan if losses == 0 else float(wins / abs(losses)),
        "max_drawdown": float(max_dd),
        "max_consecutive_losses": int(max_loss_streak(list(pnl))) if len(pnl) else 0,
        "avg_entry_minute": float(trade_log["entry_minute"].mean()) if not trade_log.empty else np.nan,
        "avg_fraction": float(trade_log["fraction"].mean()) if not trade_log.empty else np.nan,
        **roll,
    }
    return trade_log, summary


def max_loss_streak(pnls: List[float]) -> int:
    best = cur = 0
    for x in pnls:
        if x < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def build_strategy_list() -> List[str]:
    strategies: List[str] = []
    for s in ["asia", "london", "us_open", "us_afternoon"]:
        for frac in FIXED_SIZES:
            strategies.append(f"session_breakout_{s}_fixed_{int(frac*100)}pct")
            strategies.append(f"session_milddrop_{s}_fixed_{int(frac*100)}pct")
    for frac in FIXED_SIZES:
        strategies.append(f"gate_breakout_fixed_{int(frac*100)}pct")
        strategies.append(f"gate_milddrop_fixed_{int(frac*100)}pct")
        strategies.append(f"combo_guarded_fixed_{int(frac*100)}pct")
        strategies.append(f"combo_session_mix_fixed_{int(frac*100)}pct")
    strategies.extend(["combo_guarded_adaptive", "combo_session_mix_adaptive"])
    return strategies


def summarize_results(summary: pd.DataFrame) -> pd.DataFrame:
    out = summary.copy()
    if out.empty:
        return out
    out["score_end"] = out["ending_bankroll"].rank(pct=True)
    out["score_dd"] = (-out["max_drawdown"].fillna(999)).rank(pct=True)
    out["score_12h"] = out["worst_48_window_return"].fillna(-999).rank(pct=True)
    out["score_12h_pos"] = out["pct_positive_48_windows"].fillna(0).rank(pct=True)
    out["score_pf"] = out["profit_factor"].fillna(0).rank(pct=True)
    out["score_win"] = out["win_rate"].fillna(0).rank(pct=True)
    out["robustness_12h_score"] = (
        0.15 * out["score_end"]
        + 0.20 * out["score_dd"]
        + 0.25 * out["score_12h"]
        + 0.20 * out["score_12h_pos"]
        + 0.10 * out["score_pf"]
        + 0.10 * out["score_win"]
    )
    out["meets_dd_lt_30"] = out["max_drawdown"] < 0.30
    out["meets_12h_positive_all"] = out["worst_48_window_return"] > 0
    return out.sort_values(["robustness_12h_score", "ending_bankroll"], ascending=False).reset_index(drop=True)


def session_breakdown(logs: pd.DataFrame, finalists: pd.DataFrame) -> pd.DataFrame:
    if logs.empty or finalists.empty:
        return pd.DataFrame()
    keep = finalists[["strategy"]].drop_duplicates()
    out = logs.merge(keep, on="strategy", how="inner")
    grp = out.groupby(["strategy", "session_et"], as_index=False).agg(
        trades=("market_id", "size"),
        avg_pnl_usd=("pnl_usd", "mean"),
        total_pnl_usd=("pnl_usd", "sum"),
        avg_fraction=("fraction", "mean"),
    )
    grp["win_rate"] = out.groupby(["strategy", "session_et"])["pnl_usd"].apply(lambda s: float((s > 0).mean())).values
    return grp.sort_values(["strategy", "session_et"]).reset_index(drop=True)


def generate_strategy_definitions() -> str:
    return """# all_monthly_runs_clob_robust_optimization 策略定义

## 目标

这一层专门测试四个方向：

1. 时间分段版策略（按 ET session 切分）
2. 12小时滚动窗口最差收益约束
3. 仓位自适应 / 降杠杆组合策略
4. 只在 spread / liquidity / overround 同时达标时交易

## Session 定义（America/New_York）

- `asia`：19:00–02:00
- `london`：02:00–09:30
- `us_open`：09:30–12:00
- `us_afternoon`：12:00–16:00
- 其余：`other`

## 策略家族

### A. `session_breakout_<session>_fixed_<N>pct`

- 底层信号：`static_m4_breakout_up_tight`
- 只在指定 session 交易
- 固定仓位

### B. `session_milddrop_<session>_fixed_<N>pct`

- 底层信号：`static_m2_milddrop_down_book`
- 只在指定 session 交易
- 固定仓位

### C. `gate_breakout_fixed_<N>pct`

- breakout 信号 + triple gate
- triple gate = tight spread + low overround + adequate liquidity

### D. `gate_milddrop_fixed_<N>pct`

- milddrop 信号 + triple gate
- triple gate = tight spread + low overround + adequate liquidity

### E. `combo_guarded_fixed_<N>pct`

- US 时段优先做 breakout
- Asia/London 时段优先做 milddrop
- 使用交易配额和连亏后 cooldown 保护

### F. `combo_guarded_adaptive`

- 与 `combo_guarded_fixed` 相同
- 但仓位按 quality score 自适应，范围约 4%–12%

### G. `combo_session_mix_fixed_<N>pct`

- 只在 `us_open` 做 breakout
- 只在 `asia/london` 做高质量 milddrop
- 比一般组合更强调 session 切分

### H. `combo_session_mix_adaptive`

- 与 `combo_session_mix_fixed` 相同
- 但仓位按 quality score 自适应，范围约 4%–12%

## 12小时窗口指标

每条策略都会额外评估：

- `worst_48_window_return`
- `median_48_window_return`
- `pct_positive_48_windows`
- `pct_nonnegative_48_windows`

因为你要求的不只是终值高，而是：

- 最大回撤低于 30%
- 从任意起点开始，未来 12 小时表现尽量不要太差
"""


def markdown_table(df: pd.DataFrame, rows: int = 25) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def make_plots(summary: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    good = summary[summary["meets_dd_lt_30"]].sort_values("robustness_12h_score", ascending=False).head(20)
    if not good.empty:
        labels = good["strategy"]
        plt.figure(figsize=(14, 5))
        plt.bar(labels, good["robustness_12h_score"])
        plt.xticks(rotation=70, ha="right")
        plt.title("满足回撤<30%策略的12小时稳健得分 Top")
        plt.tight_layout()
        p = fig_dir / "top_12h_robustness_under_dd30.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
    win = summary.sort_values("worst_48_window_return", ascending=False).head(20)
    if not win.empty:
        labels = win["strategy"]
        plt.figure(figsize=(14, 5))
        plt.bar(labels, win["worst_48_window_return"])
        plt.xticks(rotation=70, ha="right")
        plt.title("12小时最差窗口收益 Top")
        plt.tight_layout()
        p = fig_dir / "top_worst48_return.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
    return paths


def build_report(summary: pd.DataFrame, sess: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# 全历史 CLOB 策略库扩展：session / 12小时窗口 / adaptive sizing / triple gate")
    lines.append("")
    lines.append("这份报告专门回测四个方向：")
    lines.append("")
    lines.append("1. 时间分段版策略（按 ET session 切分）")
    lines.append("2. 12 小时滚动窗口最差收益约束")
    lines.append("3. 仓位自适应 / 降杠杆组合策略")
    lines.append("4. spread / liquidity / overround 三重过滤")
    lines.append("")
    lines.append("## 满足回撤 < 30% 的候选")
    lines.append("")
    lines.append(markdown_table(summary[summary["meets_dd_lt_30"]].sort_values("robustness_12h_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## 满足回撤 < 30% 且 12小时最差窗口为正的候选")
    lines.append("")
    lines.append(markdown_table(summary[(summary["meets_dd_lt_30"]) & (summary["meets_12h_positive_all"])].sort_values("robustness_12h_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## 12小时稳健得分 Top 20")
    lines.append("")
    lines.append(markdown_table(summary.sort_values("robustness_12h_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## session 拆分表现（finalists）")
    lines.append("")
    lines.append(markdown_table(sess, rows=80))
    lines.append("")
    if not summary.empty:
        best = summary.sort_values("robustness_12h_score", ascending=False).iloc[0]
        lines.append("## 当前最值得关注的策略")
        lines.append("")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append(f"- 12小时最差窗口收益：**{best['worst_48_window_return']:.2%}**")
        lines.append(f"- 12小时正收益窗口占比：**{best['pct_positive_48_windows']:.2%}**")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![满足回撤<30%策略的12小时稳健得分 Top](all_monthly_clob_robust_figures/top_12h_robustness_under_dd30.png)")
    lines.append("")
    lines.append("![12小时最差窗口收益 Top](all_monthly_clob_robust_figures/top_worst48_return.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=FEE)
    args = parser.parse_args()

    raw, _ = base.read_all_monthly_runs(Path(args.source_root))
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No features built from monthly_runs")
    features["pred_prob_up_logit"] = rolling_logit_safe(features)
    features = add_session_labels(features)
    features = add_quality_features(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)

    all_logs: List[pd.DataFrame] = []
    summaries: List[Dict[str, float]] = []
    for strategy in build_strategy_list():
        lg, sm = simulate_strategy(features, strategy, fee=args.fee)
        all_logs.append(lg)
        summaries.append(sm)
    logs = pd.concat([x for x in all_logs if not x.empty], ignore_index=True) if all_logs else pd.DataFrame()
    summary = summarize_results(pd.DataFrame(summaries))
    finalists = pd.concat([
        summary.sort_values("robustness_12h_score", ascending=False).head(10),
        summary[(summary["meets_dd_lt_30"])].sort_values("worst_48_window_return", ascending=False).head(10)
    ], ignore_index=True).drop_duplicates(subset=["strategy"]) if not summary.empty else pd.DataFrame()
    sess = session_breakdown(logs, finalists)

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "all_monthly_clob_robust_figures"
    report_dir.mkdir(parents=True, exist_ok=True)
    figs = make_plots(summary, fig_dir)

    summary.to_csv(report_dir / "all_monthly_clob_robust_summary.csv", index=False)
    sess.to_csv(report_dir / "all_monthly_clob_robust_session_breakdown.csv", index=False)
    (report_dir / "all_monthly_clob_robust_report.md").write_text(build_report(summary, sess), encoding="utf-8")
    (report_dir / "strategy_definitions_robust.md").write_text(generate_strategy_definitions(), encoding="utf-8")
    (report_dir / "all_monthly_clob_robust_summary.json").write_text(json.dumps({"rows_summary": int(len(summary)), "rows_logs": int(len(logs)), "rows_sessions": int(len(sess)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_summary": int(len(summary)), "rows_logs": int(len(logs)), "rows_sessions": int(len(sess)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

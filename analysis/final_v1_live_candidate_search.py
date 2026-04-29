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

FEE = 0.01
WINDOW_36H = 144
LOGIT_FEATURES = base.LOGIT_FEATURES


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
            X = hist[LOGIT_FEATURES].copy()
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
            x = df.iloc[[i]][LOGIT_FEATURES].copy().fillna(med)
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


def quality_fraction(quality: float, low: float, high: float) -> float:
    q = 0.0 if pd.isna(quality) else float(np.clip(quality, 0, 1))
    return float(low + (high - low) * q)


def event_window_metrics(event_returns: np.ndarray, trade_flags: np.ndarray, window: int = WINDOW_36H) -> Dict[str, float]:
    if len(event_returns) < window:
        return {
            "worst_144_window_return": np.nan,
            "median_144_window_return": np.nan,
            "pct_positive_144_windows": np.nan,
            "pct_over_10pct_144_windows": np.nan,
            "active_144_window_rate": np.nan,
            "num_144_windows": 0,
        }
    rets, active = [], []
    for i in range(0, len(event_returns) - window + 1):
        r = float(np.prod(1.0 + event_returns[i:i+window]) - 1.0)
        a = int(np.sum(trade_flags[i:i+window]) > 0)
        rets.append(r)
        active.append(a)
    arr = np.array(rets, dtype=float)
    act = np.array(active, dtype=float)
    return {
        "worst_144_window_return": float(np.min(arr)),
        "median_144_window_return": float(np.median(arr)),
        "pct_positive_144_windows": float(np.mean(arr > 0)),
        "pct_over_10pct_144_windows": float(np.mean(arr > 0.10)),
        "active_144_window_rate": float(np.mean(act > 0)),
        "num_144_windows": int(len(arr)),
    }


def choose_trade(row: pd.Series, strategy: str) -> Tuple[str, int, float, str]:
    sess = row.get("session_et")
    if strategy == "v1_conservative_mix":
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up", False):
            return "buy_up", 4, 0.08, "asia_breakout"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down", False) and row.get("quality_milddrop", 0) >= 0.60:
            return "buy_down", 2, 0.06, "london_milddrop"
        if sess == "us_afternoon" and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.58:
            return "buy_down", 2, 0.08, "us_afternoon_milddrop"
        return "skip", -1, np.nan, "none"

    if strategy == "v1_balanced_mix":
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up", False):
            return "buy_up", 4, 0.10, "asia_breakout"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down", False):
            return "buy_down", 2, 0.08, "london_milddrop"
        if sess == "us_afternoon" and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.55:
            return "buy_down", 2, 0.10, "us_afternoon_milddrop"
        if sess in {"asia", "us_open"} and breakout_signal(row) and row.get("triple_gate_up", False) and row.get("quality_breakout", 0) >= 0.60:
            return "buy_up", 4, 0.06, "breakout_filler"
        return "skip", -1, np.nan, "none"

    if strategy == "v1_adaptive_mix":
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up", False):
            return "buy_up", 4, quality_fraction(row.get("quality_breakout", np.nan), 0.05, 0.10), "asia_breakout"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down", False):
            return "buy_down", 2, quality_fraction(row.get("quality_milddrop", np.nan), 0.04, 0.09), "london_milddrop"
        if sess == "us_afternoon" and milddrop_signal(row) and row.get("quality_milddrop", 0) >= 0.55:
            return "buy_down", 2, quality_fraction(row.get("quality_milddrop", np.nan), 0.05, 0.10), "us_afternoon_milddrop"
        return "skip", -1, np.nan, "none"

    if strategy == "v1_active_fill_mix":
        if sess == "asia" and breakout_signal(row):
            return "buy_up", 4, 0.08 if row.get("triple_gate_up", False) else 0.05, "asia_breakout"
        if sess == "london" and milddrop_signal(row):
            return "buy_down", 2, 0.06 if row.get("triple_gate_down", False) else 0.04, "london_milddrop"
        if sess == "us_afternoon" and milddrop_signal(row):
            return "buy_down", 2, 0.08 if row.get("quality_milddrop", 0) >= 0.55 else 0.05, "us_afternoon_milddrop"
        if sess == "us_open" and breakout_signal(row) and row.get("quality_breakout", 0) >= 0.58:
            return "buy_up", 4, 0.04, "us_open_breakout"
        return "skip", -1, np.nan, "none"

    if strategy == "v1_logit_overlay_mix":
        q_up = row.get("pred_prob_up_logit", np.nan)
        if pd.isna(q_up):
            return "skip", -1, np.nan, "none"
        if sess == "asia" and breakout_signal(row) and row.get("triple_gate_up", False):
            edge = q_up - row.get("buy_up_price_4m", 1.0)
            if edge > 0.08:
                return "buy_up", 4, 0.08, "asia_breakout_logit"
        if sess == "london" and milddrop_signal(row) and row.get("triple_gate_down", False):
            edge = (1 - q_up) - row.get("buy_down_price_2m", 1.0)
            if edge > 0.08:
                return "buy_down", 2, 0.06, "london_milddrop_logit"
        if sess == "us_afternoon" and milddrop_signal(row):
            edge = (1 - q_up) - row.get("buy_down_price_2m", 1.0)
            if edge > 0.10:
                return "buy_down", 2, 0.08, "us_afternoon_milddrop_logit"
        return "skip", -1, np.nan, "none"

    return "skip", -1, np.nan, "none"


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


def simulate(df: pd.DataFrame, strategy: str, fee: float = FEE) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = 100.0
    peak = 100.0
    max_dd = 0.0
    loss_streak = 0
    cooldown = 0
    recent_trade_indices: List[int] = []
    logs: List[Dict[str, object]] = []
    event_returns = np.zeros(len(df), dtype=float)
    trade_flags = np.zeros(len(df), dtype=int)

    quota = 10 if strategy in {"v1_conservative_mix", "v1_adaptive_mix", "v1_logit_overlay_mix"} else 14
    cooldown_len = 10 if strategy in {"v1_conservative_mix", "v1_logit_overlay_mix"} else 6

    for i, row in df.iterrows():
        recent_trade_indices = [x for x in recent_trade_indices if x > i - WINDOW_36H]
        if cooldown > 0:
            cooldown -= 1
            continue
        if len(recent_trade_indices) >= quota:
            continue
        side, minute, frac, component = choose_trade(row, strategy)
        if side == "skip":
            continue
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
        trade_flags[i] = 1
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        recent_trade_indices.append(i)
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
        })

    trade_log = pd.DataFrame(logs)
    pnl = pd.to_numeric(trade_log.get("pnl_usd"), errors="coerce").dropna() if not trade_log.empty else pd.Series(dtype=float)
    cost = pd.to_numeric(trade_log.get("target_cost"), errors="coerce") if not trade_log.empty else pd.Series(dtype=float)
    rtn = (pnl / cost).replace([np.inf, -np.inf], np.nan).dropna() if not trade_log.empty else pd.Series(dtype=float)
    wins = pnl[pnl > 0].sum() if not pnl.empty else 0.0
    losses = pnl[pnl < 0].sum() if not pnl.empty else 0.0
    roll = event_window_metrics(event_returns, trade_flags)
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
        "avg_fraction": float(trade_log["fraction"].mean()) if not trade_log.empty else np.nan,
        **roll,
    }
    return trade_log, summary


def summarize(summary_df: pd.DataFrame) -> pd.DataFrame:
    out = summary_df.copy()
    if out.empty:
        return out
    out["score_end"] = out["ending_bankroll"].rank(pct=True)
    out["score_dd"] = (-out["max_drawdown"].fillna(999)).rank(pct=True)
    out["score_36h"] = out["worst_144_window_return"].fillna(-999).rank(pct=True)
    out["score_36h_10"] = out["pct_over_10pct_144_windows"].fillna(0).rank(pct=True)
    out["score_active"] = out["active_144_window_rate"].fillna(0).rank(pct=True)
    out["score_pf"] = out["profit_factor"].fillna(0).rank(pct=True)
    out["v1_score"] = 0.15*out["score_end"] + 0.20*out["score_dd"] + 0.25*out["score_36h"] + 0.20*out["score_36h_10"] + 0.10*out["score_active"] + 0.10*out["score_pf"]
    out["meets_dd_lt_30"] = out["max_drawdown"] < 0.30
    out["meets_36h_gt_10_all"] = out["worst_144_window_return"] > 0.10
    out["meets_v1_goal"] = out["meets_dd_lt_30"] & out["meets_36h_gt_10_all"]
    return out.sort_values(["v1_score", "ending_bankroll"], ascending=False).reset_index(drop=True)


def breakdown(logs: pd.DataFrame) -> pd.DataFrame:
    if logs.empty:
        return pd.DataFrame()
    grp = logs.groupby(["strategy", "session_et", "component"], as_index=False).agg(
        trades=("market_id", "size"),
        avg_pnl_usd=("pnl_usd", "mean"),
        total_pnl_usd=("pnl_usd", "sum"),
        avg_fraction=("fraction", "mean"),
    )
    grp["win_rate"] = logs.groupby(["strategy", "session_et", "component"])["pnl_usd"].apply(lambda s: float((s > 0).mean())).values
    return grp.sort_values(["strategy", "session_et", "component"]).reset_index(drop=True)


def definitions_md() -> str:
    return """# final_v1_live_candidate_search 策略定义

## 目标

这一层专门针对更接近实盘的目标函数：

- 最大回撤 < 30%
- 从任意起点开始，未来 36 小时（144 个潜在窗口）收益尽量 > 10%
- 同时允许不同 session 的子策略混合

## 候选组合

### 1. `v1_conservative_mix`
- Asia：只做 breakout，必须 triple gate
- London：只做 milddrop，必须 triple gate，且 quality 更高
- US afternoon：只做高质量 milddrop
- 仓位偏低，trade quota 更紧

### 2. `v1_balanced_mix`
- 保留 Asia breakout、London milddrop、US afternoon milddrop
- 允许少量 breakout filler
- 比 conservative 覆盖更高

### 3. `v1_adaptive_mix`
- 与 conservative 结构类似
- 但仓位按 quality 自适应变化

### 4. `v1_active_fill_mix`
- 允许更多 session filler 交易
- 目标是提高 36 小时窗口覆盖度

### 5. `v1_logit_overlay_mix`
- 在 session mix 基础上再叠一层 rolling logistic edge 过滤
- 只有模型概率与盘口价格偏差足够大时才交易
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
    top = summary.sort_values("v1_score", ascending=False).head(10)
    labels = top["strategy"]
    plt.figure(figsize=(12, 5))
    plt.bar(labels, top["v1_score"])
    plt.xticks(rotation=45, ha="right")
    plt.title("Final v1 candidate score")
    plt.tight_layout()
    p = fig_dir / "final_v1_scores.png"
    plt.savefig(p, dpi=150)
    plt.close()
    paths.append(str(p))
    plt.figure(figsize=(12, 5))
    plt.bar(labels, top["worst_144_window_return"])
    plt.xticks(rotation=45, ha="right")
    plt.title("36小时最差窗口收益")
    plt.tight_layout()
    p = fig_dir / "final_v1_worst144.png"
    plt.savefig(p, dpi=150)
    plt.close()
    paths.append(str(p))
    return paths


def build_report(summary: pd.DataFrame, sess: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# Final v1 live candidate search")
    lines.append("")
    lines.append("这份报告把之前有效的 session 子策略重新混合，并把目标函数改成更贴近实盘的版本：")
    lines.append("")
    lines.append("- 最大回撤 < 30%")
    lines.append("- 任意起点开始，未来 36 小时尽量 > 10%")
    lines.append("- 用 session mix + trade quota + cooldown + triple gate + optional logistic overlay")
    lines.append("")
    lines.append("## 满足回撤 < 30% 的候选")
    lines.append("")
    lines.append(markdown_table(summary[summary["meets_dd_lt_30"]].sort_values("v1_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## 满足回撤 < 30% 且 36小时最差窗口 > 10% 的候选")
    lines.append("")
    lines.append(markdown_table(summary[summary["meets_v1_goal"]].sort_values("v1_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## v1 评分 Top")
    lines.append("")
    lines.append(markdown_table(summary.sort_values("v1_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## session / component 拆分")
    lines.append("")
    lines.append(markdown_table(sess, rows=80))
    lines.append("")
    if not summary.empty:
        best = summary.sort_values("v1_score", ascending=False).iloc[0]
        lines.append("## 当前最终候选")
        lines.append("")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append(f"- 36小时最差窗口收益：**{best['worst_144_window_return']:.2%}**")
        lines.append(f"- 36小时 >10% 窗口占比：**{best['pct_over_10pct_144_windows']:.2%}**")
        lines.append(f"- active 36小时窗口占比：**{best['active_144_window_rate']:.2%}**")
        lines.append("")
        if not bool(best.get("meets_v1_goal", False)):
            lines.append("当前还没有策略同时满足你设定的两个硬目标；这里给出的是现阶段最接近实盘要求的候选。")
            lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![Final v1 candidate score](final_v1_figures/final_v1_scores.png)")
    lines.append("")
    lines.append("![36小时最差窗口收益](final_v1_figures/final_v1_worst144.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    args = parser.parse_args()

    raw, _ = base.read_all_monthly_runs(Path(args.source_root))
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable features built from monthly_runs")
    features["pred_prob_up_logit"] = rolling_logit_safe(features)
    features = add_session_labels(features)
    features = add_quality_features(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)

    strategy_names = [
        "v1_conservative_mix",
        "v1_balanced_mix",
        "v1_adaptive_mix",
        "v1_active_fill_mix",
        "v1_logit_overlay_mix",
    ]
    logs_list, summaries = [], []
    for s in strategy_names:
        lg, sm = simulate(features, s)
        logs_list.append(lg)
        summaries.append(sm)
    logs = pd.concat([x for x in logs_list if not x.empty], ignore_index=True) if logs_list else pd.DataFrame()
    summary = summarize(pd.DataFrame(summaries))
    sess = breakdown(logs)

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "final_v1_figures"
    report_dir.mkdir(parents=True, exist_ok=True)
    figs = make_plots(summary, fig_dir)

    summary.to_csv(report_dir / "final_v1_summary.csv", index=False)
    sess.to_csv(report_dir / "final_v1_session_breakdown.csv", index=False)
    (report_dir / "final_v1_report.md").write_text(build_report(summary, sess), encoding="utf-8")
    (report_dir / "final_v1_strategy_definitions.md").write_text(definitions_md(), encoding="utf-8")
    (report_dir / "final_v1_summary.json").write_text(json.dumps({"rows_summary": int(len(summary)), "rows_logs": int(len(logs)), "rows_sessions": int(len(sess)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_summary": int(len(summary)), "rows_logs": int(len(logs)), "rows_sessions": int(len(sess)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

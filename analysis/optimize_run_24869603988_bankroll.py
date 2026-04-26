from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

MOVE_BINS = [-10000, -100, -50, -30, -10, 10, 30, 50, 100, 10000]
MOVE_LABELS = ["<=-100", "-100~-50", "-50~-30", "-30~-10", "-10~10", "10~30", "30~50", "50~100", ">=100"]
PRICE_BINS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
PRICE_LABELS = ["0.0~0.2", "0.2~0.4", "0.4~0.6", "0.6~0.8", "0.8~1.0"]


def load_features(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["first_quote_ts"] = pd.to_datetime(df["first_quote_ts"], utc=True, errors="coerce")
    df = df[df["outcome_up"].notna()].copy().sort_values("first_quote_ts").reset_index(drop=True)
    df["move_bucket"] = pd.cut(df["btc_move_2m"], bins=MOVE_BINS, labels=MOVE_LABELS, include_lowest=True)
    df["down_price_bucket"] = pd.cut(df["buy_down_price_2m"], bins=PRICE_BINS, labels=PRICE_LABELS, include_lowest=True)
    df["up_price_bucket"] = pd.cut(df["buy_up_price_2m"], bins=PRICE_BINS, labels=PRICE_LABELS, include_lowest=True)
    return df


def kelly_fraction(prob: float, price: float) -> float:
    if pd.isna(prob) or pd.isna(price) or price <= 0 or price >= 1:
        return 0.0
    denom = 1.0 - price
    if denom <= 0:
        return 0.0
    return float(max((prob - price) / denom, 0.0))


def add_rule_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    move = out["btc_move_2m"]
    down_p = out["buy_down_price_2m"]
    up_p = out["buy_up_price_2m"]
    imb = out["size_imbalance_updown_2m"]
    dsz = out["buy_down_size_2m"]
    usz = out["buy_up_size_2m"]

    out["rule_drop10_down"] = np.where(move <= -10, "buy_down", "skip")
    out["rule_drop10_to50_down"] = np.where((move <= -10) & (move > -50), "buy_down", "skip")
    out["rule_drop10_to30_down"] = np.where((move <= -10) & (move > -30), "buy_down", "skip")
    out["rule_drop20_down"] = np.where(move <= -20, "buy_down", "skip")
    out["rule_drop30_down"] = np.where(move <= -30, "buy_down", "skip")
    out["rule_drop10_down_pricecap80"] = np.where((move <= -10) & (down_p <= 0.80), "buy_down", "skip")
    out["rule_drop10_down_pricecap75"] = np.where((move <= -10) & (down_p <= 0.75), "buy_down", "skip")
    out["rule_drop10_down_size_neg"] = np.where((move <= -10) & (imb <= 0), "buy_down", "skip")
    out["rule_drop10_down_size_strong_neg"] = np.where((move <= -10) & (imb <= -0.10), "buy_down", "skip")
    out["rule_drop10_down_good_liq250"] = np.where((move <= -10) & (dsz >= 250), "buy_down", "skip")
    out["rule_drop10_down_good_liq400"] = np.where((move <= -10) & (dsz >= 400), "buy_down", "skip")
    out["rule_drop10_down_combo80_neg"] = np.where((move <= -10) & (down_p <= 0.80) & (imb <= 0), "buy_down", "skip")
    out["rule_drop10_down_combo75_neg"] = np.where((move <= -10) & (down_p <= 0.75) & (imb <= 0), "buy_down", "skip")
    out["rule_drop10_down_combo80_liq"] = np.where((move <= -10) & (down_p <= 0.80) & (dsz >= 250), "buy_down", "skip")

    out["rule_rise30to50_up"] = np.where((move > 30) & (move <= 50), "buy_up", "skip")
    out["rule_rise30to50_up_pricecap80"] = np.where((move > 30) & (move <= 50) & (up_p <= 0.80), "buy_up", "skip")
    out["rule_rise30to50_up_size_pos"] = np.where((move > 30) & (move <= 50) & (imb >= 0), "buy_up", "skip")
    out["rule_rise30to50_up_good_liq250"] = np.where((move > 30) & (move <= 50) & (usz >= 250), "buy_up", "skip")
    return out


def candidate_rule_names(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if c.startswith("rule_")]


def pnl_and_price(row: pd.Series, side: str, fee: float) -> tuple[float, float, float]:
    if side == "buy_up":
        price = row["buy_up_price_2m"]
        size = row["buy_up_size_2m"]
        pnl_per_share = row["outcome_up"] - price - fee
    elif side == "buy_down":
        price = row["buy_down_price_2m"]
        size = row["buy_down_size_2m"]
        pnl_per_share = (1.0 - row["outcome_up"]) - price - fee
    else:
        return np.nan, np.nan, np.nan
    return float(price), float(size), float(pnl_per_share)


def historical_q_for_rule(df: pd.DataFrame, signal_col: str, fee: float) -> pd.Series:
    rows = []
    hist_success = []
    hist_side = []
    for _, row in df.iterrows():
        side = row[signal_col]
        if side not in {"buy_up", "buy_down"}:
            rows.append(np.nan)
            continue
        if len(hist_success) >= 8:
            rows.append(float(np.mean(hist_success)))
        else:
            rows.append(np.nan)
        success = 1.0 if ((side == "buy_up" and row["outcome_up"] == 1.0) or (side == "buy_down" and row["outcome_up"] == 0.0)) else 0.0
        hist_success.append(success)
        hist_side.append(side)
    return pd.Series(rows, index=df.index)


def simulate_fixed(df: pd.DataFrame, signal_col: str, fixed_frac: float, fee: float, starting_bankroll: float = 100.0) -> tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = starting_bankroll
    peak = starting_bankroll
    max_dd = 0.0
    rows = []
    for _, row in df.iterrows():
        side = row[signal_col]
        if side not in {"buy_up", "buy_down"}:
            continue
        price, size_avail, pnl_per_share = pnl_and_price(row, side, fee)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        target_cost = min(bankroll * fixed_frac, bankroll, size_avail * price)
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        rows.append({
            "strategy": signal_col,
            "sizing": f"fixed_{int(fixed_frac*100)}pct",
            "first_quote_ts": row["first_quote_ts"],
            "slug": row["slug"],
            "side": side,
            "target_cost": target_cost,
            "shares": shares,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
        })
    trade_log = pd.DataFrame(rows)
    summary = {
        "strategy": signal_col,
        "sizing": f"fixed_{int(fixed_frac*100)}pct",
        "trades": int(len(trade_log)),
        "ending_bankroll": float(trade_log["bankroll_after"].iloc[-1]) if not trade_log.empty else starting_bankroll,
        "total_return": float((trade_log["bankroll_after"].iloc[-1] / starting_bankroll) - 1.0) if not trade_log.empty else 0.0,
        "avg_trade_return_on_cost": float((trade_log["pnl_usd"] / trade_log["target_cost"]).mean()) if not trade_log.empty else np.nan,
        "max_drawdown": float(max_dd),
    }
    return trade_log, summary


def simulate_kelly(df: pd.DataFrame, signal_col: str, qhat: pd.Series, fee: float, multiplier: float, cap: float | None, starting_bankroll: float = 100.0) -> tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = starting_bankroll
    peak = starting_bankroll
    max_dd = 0.0
    rows = []
    for idx, row in df.iterrows():
        side = row[signal_col]
        if side not in {"buy_up", "buy_down"}:
            continue
        q = qhat.loc[idx]
        price, size_avail, pnl_per_share = pnl_and_price(row, side, fee)
        if pd.isna(q) or pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        f = kelly_fraction(float(q), float(price)) * multiplier
        if cap is not None:
            f = min(f, cap)
        target_cost = min(bankroll * max(f, 0.0), bankroll, size_avail * price)
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        rows.append({
            "strategy": signal_col,
            "sizing": f"{'full' if multiplier==1 else 'half' if multiplier==0.5 else 'quarter'}_kelly" + ("_capped20" if cap is not None else ""),
            "first_quote_ts": row["first_quote_ts"],
            "slug": row["slug"],
            "side": side,
            "q_hat": q,
            "target_cost": target_cost,
            "shares": shares,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
        })
    trade_log = pd.DataFrame(rows)
    summary = {
        "strategy": signal_col,
        "sizing": f"{'full' if multiplier==1 else 'half' if multiplier==0.5 else 'quarter'}_kelly" + ("_capped20" if cap is not None else ""),
        "trades": int(len(trade_log)),
        "ending_bankroll": float(trade_log["bankroll_after"].iloc[-1]) if not trade_log.empty else starting_bankroll,
        "total_return": float((trade_log["bankroll_after"].iloc[-1] / starting_bankroll) - 1.0) if not trade_log.empty else 0.0,
        "avg_trade_return_on_cost": float((trade_log["pnl_usd"] / trade_log["target_cost"]).mean()) if not trade_log.empty else np.nan,
        "max_drawdown": float(max_dd),
    }
    return trade_log, summary


def run_search(df: pd.DataFrame, fee: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    rule_names = candidate_rule_names(df)
    logs = []
    summaries = []
    for rule in rule_names:
        lg, sm = simulate_fixed(df, rule, 0.10, fee)
        logs.append(lg); summaries.append(sm)
        lg, sm = simulate_fixed(df, rule, 0.20, fee)
        logs.append(lg); summaries.append(sm)
        qhat = historical_q_for_rule(df, rule, fee)
        for mult, cap in [(1.0, None), (0.5, None), (0.25, None), (0.5, 0.20), (0.25, 0.20)]:
            lg, sm = simulate_kelly(df, rule, qhat, fee, mult, cap)
            logs.append(lg); summaries.append(sm)
    logs_df = pd.concat([x for x in logs if not x.empty], ignore_index=True) if logs else pd.DataFrame()
    summary_df = pd.DataFrame(summaries)
    summary_df = summary_df.sort_values("ending_bankroll", ascending=False).reset_index(drop=True)
    return logs_df, summary_df


def make_plots(summary: pd.DataFrame, logs: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not summary.empty:
        top = summary.head(15)
        labels = top["strategy"] + "|" + top["sizing"]
        plt.figure(figsize=(12,5))
        plt.bar(labels, top["ending_bankroll"])
        plt.xticks(rotation=60, ha="right")
        plt.title("优化策略Top期末本金")
        plt.tight_layout()
        p = fig_dir / "optimizer_top_endings.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
        best = summary.iloc[0]
        s = logs[(logs["strategy"] == best["strategy"]) & (logs["sizing"] == best["sizing"])].copy().sort_values("first_quote_ts")
        if not s.empty:
            plt.figure(figsize=(10,4))
            plt.plot(s["first_quote_ts"], s["bankroll_after"])
            plt.title(f"最佳优化策略本金曲线: {best['strategy']} | {best['sizing']}")
            plt.tight_layout()
            p = fig_dir / "optimizer_best_curve.png"
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))
    return paths


def build_report(summary: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# 优化策略回测补充报告")
    lines.append("")
    lines.append("## 修正说明")
    lines.append("")
    lines.append("上一版优化报告把多条策略放在同一条资金路径里再按策略名切片汇总，口径不对。这一版已经改成**每条策略独立回测**，因此现在可以和 `rule_drop10_down + fixed_20pct` 做正面对比。")
    lines.append("")
    lines.append("## 这版优化在搜什么")
    lines.append("")
    lines.append("- 基线：`rule_drop10_down`")
    lines.append("- 过滤器：price cap、size imbalance、流动性阈值、组合过滤")
    lines.append("- 仓位：fixed 10%、fixed 20%、历史命中率版 Kelly / capped Kelly")
    lines.append("")
    lines.append("## 候选策略-仓位结果")
    lines.append("")
    lines.append(summary.head(30).to_markdown(index=False))
    lines.append("")
    if not summary.empty:
        best = summary.iloc[0]
        lines.append("## 当前最佳优化结果")
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
    lines.append("### 优化策略Top期末本金")
    lines.append("")
    lines.append("![优化策略Top期末本金](optimization_figures/optimizer_top_endings.png)")
    lines.append("")
    lines.append("### 最佳优化策略本金曲线")
    lines.append("")
    lines.append("![最佳优化策略本金曲线](optimization_figures/optimizer_best_curve.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-file", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    features = add_rule_columns(load_features(Path(args.features_file)))
    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "optimization_figures"
    report_dir.mkdir(parents=True, exist_ok=True)

    logs, summary = run_search(features, fee=args.fee)
    fig_paths = make_plots(summary, logs, fig_dir)

    logs.to_csv(report_dir / "optimizer_trade_logs.csv", index=False)
    summary.to_csv(report_dir / "optimizer_strategy_summary.csv", index=False)
    (report_dir / "optimization_report.md").write_text(build_report(summary), encoding="utf-8")
    meta = {"rows_features": int(len(features)), "rows_summary": int(len(summary)), "figure_count": len(fig_paths)}
    (report_dir / "optimizer_summary.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()

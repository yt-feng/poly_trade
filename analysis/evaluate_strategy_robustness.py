from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

LOG_FILES = [
    ("bankroll", "bankroll_trade_logs.csv"),
    ("classic", "classic_strategy_trade_logs.csv"),
    ("extended_classic", "extended_classic_trade_logs.csv"),
    ("latest_regime", "latest_regime_trade_logs.csv"),
]
SUMMARY_FILES = [
    ("bankroll", "bankroll_strategy_summary.csv"),
    ("classic", "classic_strategy_summary.csv"),
    ("extended_classic", "extended_classic_summary.csv"),
    ("latest_regime", "latest_regime_summary.csv"),
]


def max_loss_streak(pnls: List[float]) -> int:
    best, cur = 0, 0
    for x in pnls:
        if x < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def read_logs(report_dir: Path) -> pd.DataFrame:
    frames = []
    for layer, fn in LOG_FILES:
        p = report_dir / fn
        if not p.exists():
            continue
        df = pd.read_csv(p)
        if "strategy_name" in df.columns and "strategy" not in df.columns:
            df = df.rename(columns={"strategy_name": "strategy"})
        if "first_quote_ts" in df.columns:
            df["first_quote_ts"] = pd.to_datetime(df["first_quote_ts"], utc=True, errors="coerce")
        df["source_layer"] = layer
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def read_summaries(report_dir: Path) -> pd.DataFrame:
    frames = []
    for layer, fn in SUMMARY_FILES:
        p = report_dir / fn
        if not p.exists():
            continue
        df = pd.read_csv(p)
        if "strategy_name" in df.columns and "strategy" not in df.columns:
            df = df.rename(columns={"strategy_name": "strategy"})
        df["source_layer"] = layer
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def summarize_logs(logs: pd.DataFrame) -> pd.DataFrame:
    if logs.empty:
        return pd.DataFrame()
    rows = []
    group_cols = ["source_layer", "strategy", "sizing"]
    for keys, g in logs.groupby(group_cols, dropna=False):
        pnl = pd.to_numeric(g.get("pnl_usd"), errors="coerce").dropna()
        cost = pd.to_numeric(g.get("target_cost"), errors="coerce")
        rtn = (pnl / cost).replace([np.inf, -np.inf], np.nan).dropna()
        wins = pnl[pnl > 0].sum()
        losses = pnl[pnl < 0].sum()
        profit_factor = np.nan if losses == 0 else float(wins / abs(losses))
        downside = rtn[rtn < 0]
        downside_dev = float(np.sqrt(np.mean(np.square(downside)))) if len(downside) > 0 else 0.0
        rows.append({
            "source_layer": keys[0],
            "strategy": keys[1],
            "sizing": keys[2],
            "trades_from_logs": int(len(g)),
            "win_rate": float((pnl > 0).mean()) if len(pnl) else np.nan,
            "loss_rate": float((pnl < 0).mean()) if len(pnl) else np.nan,
            "profit_factor": profit_factor,
            "median_trade_return_on_cost": float(rtn.median()) if len(rtn) else np.nan,
            "p10_trade_return_on_cost": float(rtn.quantile(0.10)) if len(rtn) else np.nan,
            "worst_trade_return_on_cost": float(rtn.min()) if len(rtn) else np.nan,
            "max_consecutive_losses": int(max_loss_streak(list(pnl))) if len(pnl) else 0,
            "downside_deviation": downside_dev,
        })
    return pd.DataFrame(rows)


def build_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    # normalize robust dimensions to [0,1] ranks; bigger is better
    out["score_end"] = out["ending_bankroll"].rank(pct=True)
    out["score_win"] = out["win_rate"].rank(pct=True)
    out["score_pf"] = out["profit_factor"].fillna(0).rank(pct=True)
    out["score_dd"] = (-out["max_drawdown"].fillna(999)).rank(pct=True)
    out["score_streak"] = (-out["max_consecutive_losses"].fillna(999)).rank(pct=True)
    out["score_tail"] = out["p10_trade_return_on_cost"].fillna(-999).rank(pct=True)
    out["robustness_score"] = (
        0.15 * out["score_end"] +
        0.20 * out["score_win"] +
        0.20 * out["score_pf"] +
        0.20 * out["score_dd"] +
        0.15 * out["score_streak"] +
        0.10 * out["score_tail"]
    )
    return out.sort_values(["robustness_score", "ending_bankroll"], ascending=False).reset_index(drop=True)


def markdown_table(df: pd.DataFrame, rows: int = 40) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def make_plots(df: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not df.empty:
        top = df.head(15)
        labels = top["strategy"] + "|" + top["sizing"]
        plt.figure(figsize=(13, 5))
        plt.bar(labels, top["robustness_score"])
        plt.xticks(rotation=65, ha="right")
        plt.title("策略稳定性综合得分 Top 15")
        plt.tight_layout()
        p = fig_dir / "robustness_top_scores.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
        plt.figure(figsize=(8, 5))
        plt.scatter(df["max_drawdown"], df["ending_bankroll"])
        plt.xlabel("max_drawdown")
        plt.ylabel("ending_bankroll")
        plt.title("收益 vs 回撤")
        plt.tight_layout()
        p = fig_dir / "robustness_return_vs_drawdown.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
    return paths


def build_report(df: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# 策略稳定性 / 失败体验评估")
    lines.append("")
    lines.append("这份报告不只看谁赚得多，还看谁更稳、更少失败。")
    lines.append("")
    lines.append("## 指标说明")
    lines.append("")
    lines.append("- `ending_bankroll`：期末本金")
    lines.append("- `max_drawdown`：最大回撤，越低越好")
    lines.append("- `win_rate`：单笔胜率，越高越好")
    lines.append("- `profit_factor`：总盈利 / 总亏损绝对值，越高越好")
    lines.append("- `max_consecutive_losses`：最大连续亏损笔数，越低越好")
    lines.append("- `p10_trade_return_on_cost`：最差 10% 左右单笔体验的代表值，越高越好")
    lines.append("- `robustness_score`：把收益、胜率、利润因子、回撤、连亏、尾部体验综合后的稳定性分数")
    lines.append("")
    lines.append("## 稳定性 Top 20")
    lines.append("")
    lines.append(markdown_table(df, rows=20))
    lines.append("")
    if not df.empty:
        best = df.iloc[0]
        lines.append("## 当前最稳的策略")
        lines.append("")
        lines.append(f"- 层级：**{best['source_layer']}**")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 仓位：**{best['sizing']}**")
        lines.append(f"- 稳定性得分：**{best['robustness_score']:.4f}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append(f"- 胜率：**{best['win_rate']:.2%}**")
        lines.append(f"- 最大连续亏损：**{int(best['max_consecutive_losses'])}**")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![策略稳定性综合得分 Top 15](robustness_figures/robustness_top_scores.png)")
    lines.append("")
    lines.append("![收益 vs 回撤](robustness_figures/robustness_return_vs_drawdown.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=str, required=True)
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    logs = read_logs(report_dir)
    sums = read_summaries(report_dir)
    log_summary = summarize_logs(logs)
    if not sums.empty:
        merged = sums.merge(log_summary, on=["source_layer", "strategy", "sizing"], how="left")
    else:
        merged = log_summary
    merged = merged[(merged.get("trades", merged.get("trades_from_logs", 0)).fillna(0) > 0)].copy() if not merged.empty else merged
    merged = build_scores(merged)
    merged.to_csv(report_dir / "strategy_robustness_summary.csv", index=False)
    figs = make_plots(merged, report_dir / "robustness_figures")
    (report_dir / "strategy_robustness_report.md").write_text(build_report(merged), encoding="utf-8")
    (report_dir / "strategy_robustness_summary.json").write_text(json.dumps({"rows": int(len(merged)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows": int(len(merged)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

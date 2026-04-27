from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

SUMMARY_FILES = [
    ("bankroll", "bankroll_strategy_summary.csv"),
    ("optimizer", "optimizer_strategy_summary.csv"),
    ("classic", "classic_strategy_summary.csv"),
    ("extended_classic", "extended_classic_summary.csv"),
]


def normalize(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    out = df.copy()
    if "strategy_name" in out.columns and "strategy" not in out.columns:
        out = out.rename(columns={"strategy_name": "strategy"})
    keep = [c for c in ["strategy", "sizing", "trades", "ending_bankroll", "total_return", "avg_trade_return_on_cost", "max_drawdown"] if c in out.columns]
    out = out[keep].copy()
    out["source_layer"] = source_name
    return out


def markdown_table(df: pd.DataFrame, n: int = 50) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(n).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--min-ending-bankroll", type=float, default=400.0)
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    frames: List[pd.DataFrame] = []
    missing: List[str] = []
    for layer, filename in SUMMARY_FILES:
        path = report_dir / filename
        if not path.exists():
            missing.append(filename)
            continue
        df = pd.read_csv(path)
        frames.append(normalize(df, layer))

    all_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["strategy", "sizing", "trades", "ending_bankroll", "total_return", "avg_trade_return_on_cost", "max_drawdown", "source_layer"])
    all_df = all_df.sort_values("ending_bankroll", ascending=False).reset_index(drop=True) if not all_df.empty else all_df
    winners = all_df[all_df["ending_bankroll"] > args.min_ending_bankroll].copy() if not all_df.empty else all_df
    winners = winners.sort_values(["ending_bankroll", "total_return"], ascending=False).reset_index(drop=True) if not winners.empty else winners

    if not all_df.empty:
        all_df.to_csv(report_dir / "all_strategy_summary_latest.csv", index=False)
    winners.to_csv(report_dir / "strategies_over_400_summary.csv", index=False)

    lines: List[str] = []
    lines.append("# 最新数据回测：期末本金大于 400 USD 的策略总结")
    lines.append("")
    lines.append(f"筛选条件：`ending_bankroll > {args.min_ending_bankroll:.0f}`")
    lines.append("")
    if missing:
        lines.append("## 未找到的 summary 文件")
        lines.append("")
        for x in missing:
            lines.append(f"- `{x}`")
        lines.append("")
    lines.append("## 策略结果")
    lines.append("")
    lines.append(markdown_table(winners, n=100))
    lines.append("")
    if not winners.empty:
        best = winners.iloc[0]
        lines.append("## 当前最佳")
        lines.append("")
        lines.append(f"- 层级：**{best['source_layer']}**")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 仓位：**{best['sizing']}**")
        lines.append(f"- 交易笔数：**{int(best['trades'])}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 总收益率：**{best['total_return']:.2%}**")
        if pd.notna(best.get('max_drawdown')):
            lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append("")
    (report_dir / "strategies_over_400_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print({"rows_all": int(len(all_df)), "rows_winners": int(len(winners))})


if __name__ == "__main__":
    main()

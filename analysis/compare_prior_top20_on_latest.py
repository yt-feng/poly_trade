from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

SUMMARY_FILES = [
    ("bankroll", "bankroll_strategy_summary.csv"),
    ("optimizer", "optimizer_strategy_summary.csv"),
    ("classic", "classic_strategy_summary.csv"),
    ("extended_classic", "extended_classic_summary.csv"),
]


def normalize(df: pd.DataFrame, source_layer: str) -> pd.DataFrame:
    out = df.copy()
    if "strategy_name" in out.columns and "strategy" not in out.columns:
        out = out.rename(columns={"strategy_name": "strategy"})
    for col in ["strategy", "sizing", "trades", "ending_bankroll", "total_return", "avg_trade_return_on_cost", "max_drawdown"]:
        if col not in out.columns:
            out[col] = np.nan
    out = out[["strategy", "sizing", "trades", "ending_bankroll", "total_return", "avg_trade_return_on_cost", "max_drawdown"]].copy()
    out["source_layer"] = source_layer
    return out


def read_all(report_dir: Path) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for layer, fn in SUMMARY_FILES:
        path = report_dir / fn
        if path.exists():
            frames.append(normalize(pd.read_csv(path), layer))
    if not frames:
        return pd.DataFrame(columns=["strategy", "sizing", "trades", "ending_bankroll", "total_return", "avg_trade_return_on_cost", "max_drawdown", "source_layer"])
    out = pd.concat(frames, ignore_index=True)
    out = out[(out["trades"].fillna(0) > 0)].copy()
    out = out[~out["strategy"].astype(str).str.endswith("_q")].copy()
    out = out[~out["strategy"].astype(str).isin(["pressure_imbalance_2m", "rolling_mean_pnl_20", "rolling_winrate_20", "rolling_sharpe_proxy_20"])].copy()
    return out.reset_index(drop=True)


def markdown_table(df: pd.DataFrame, rows: int = 50) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prior-report-dir", type=str, required=True)
    parser.add_argument("--latest-report-dir", type=str, required=True)
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args()

    prior_dir = Path(args.prior_report_dir)
    latest_dir = Path(args.latest_report_dir)

    prior = read_all(prior_dir)
    latest = read_all(latest_dir)

    prior = prior.sort_values(["ending_bankroll", "total_return"], ascending=False).reset_index(drop=True)
    top = prior.drop_duplicates(subset=["source_layer", "strategy", "sizing"]).head(args.top_k).copy()
    top["prior_rank"] = range(1, len(top) + 1)

    merged = top.merge(
        latest,
        on=["source_layer", "strategy", "sizing"],
        how="left",
        suffixes=("_prior", "_latest"),
    )
    for c in ["trades_latest", "ending_bankroll_latest", "total_return_latest", "avg_trade_return_on_cost_latest", "max_drawdown_latest"]:
        if c not in merged.columns:
            merged[c] = np.nan
    if not merged.empty:
        merged["delta_ending_bankroll"] = merged["ending_bankroll_latest"] - merged["ending_bankroll_prior"]
        merged["delta_total_return"] = merged["total_return_latest"] - merged["total_return_prior"]
        merged = merged.sort_values("prior_rank").reset_index(drop=True)

    merged.to_csv(latest_dir / "prior_top20_rerun_on_latest.csv", index=False)

    lines: List[str] = []
    lines.append("# 旧数据 Top 20 强策略在最新数据上的复测对照")
    lines.append("")
    lines.append("这份报告回答的问题是：")
    lines.append("")
    lines.append("- 先在旧数据集里挑出历史最强的 Top 20 策略")
    lines.append("- 再看这些策略在最新月度数据上是否还能打")
    lines.append("")
    lines.append(f"- 旧数据目录：`{prior_dir}`")
    lines.append(f"- 最新数据目录：`{latest_dir}`")
    lines.append(f"- Top K：**{args.top_k}**")
    lines.append("")
    lines.append("## 对照表")
    lines.append("")
    lines.append(markdown_table(merged, rows=100))
    lines.append("")
    if not merged.empty:
        best_latest = merged.sort_values("ending_bankroll_latest", ascending=False).iloc[0]
        worst_drop = merged.sort_values("delta_ending_bankroll", ascending=True).iloc[0]
        lines.append("## 观察")
        lines.append("")
        lines.append(f"- 旧数据 Top 20 中，在最新数据上表现最好的，是 **{best_latest['strategy']} | {best_latest['sizing']} | {best_latest['source_layer']}**，最新期末本金 **{best_latest['ending_bankroll_latest']:.2f} USD**。")
        lines.append(f"- 衰减最明显的，是 **{worst_drop['strategy']} | {worst_drop['sizing']} | {worst_drop['source_layer']}**，期末本金变化 **{worst_drop['delta_ending_bankroll']:.2f} USD**。")
        lines.append("")
    (latest_dir / "prior_top20_rerun_on_latest.md").write_text("\n".join(lines), encoding="utf-8")
    print({"rows_prior": int(len(prior)), "rows_top": int(len(top)), "rows_merged": int(len(merged))})


if __name__ == "__main__":
    main()

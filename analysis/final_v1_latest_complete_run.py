from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1


def choose_latest_complete_run(run_cover: pd.DataFrame) -> tuple[str, int]:
    cover = run_cover.copy()
    cover = cover[pd.notna(cover["file_count"])].copy()
    if cover.empty:
        raise RuntimeError("No monthly runs found")
    max_file_count = int(cover["file_count"].max())
    full = cover[cover["file_count"] == max_file_count].copy()
    full = full.sort_values("run_name")
    latest_run = str(full.iloc[-1]["run_name"])
    return latest_run, max_file_count


def build_report(latest_run: str, max_file_count: int, coverage: pd.DataFrame, summary: pd.DataFrame, sess: pd.DataFrame) -> str:
    lines: list[str] = []
    lines.append("# Final v1 on latest complete monthly run")
    lines.append("")
    lines.append("这份报告只回测 `monthly_runs` 中**最新的完整 run**。完整的定义是：")
    lines.append("")
    lines.append("- 先看当前 `monthly_runs/*` 每个 run 的文件数")
    lines.append(f"- 以**最大文件数 = {max_file_count}** 作为“完整 run”的标准")
    lines.append(f"- 在所有完整 run 中，取 `run_name` 最新的一个：**{latest_run}**")
    lines.append("")
    lines.append("## 当前 monthly_runs 覆盖")
    lines.append("")
    lines.append(v1.markdown_table(coverage, rows=100))
    lines.append("")
    lines.append(f"## 仅针对 `{latest_run}` 的 final v1 候选结果")
    lines.append("")
    lines.append(v1.markdown_table(summary.sort_values("v1_score", ascending=False), rows=20))
    lines.append("")
    lines.append("## session / component 拆分")
    lines.append("")
    lines.append(v1.markdown_table(sess, rows=80))
    lines.append("")
    if not summary.empty:
        best = summary.sort_values("v1_score", ascending=False).iloc[0]
        lines.append("## 当前最新完整 run 的最优候选")
        lines.append("")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append(f"- 36小时最差窗口收益：**{best['worst_144_window_return']:.2%}**")
        lines.append(f"- 36小时 >10% 窗口占比：**{best['pct_over_10pct_144_windows']:.2%}**")
        lines.append(f"- active 36小时窗口占比：**{best['active_144_window_rate']:.2%}**")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![Final v1 candidate score](final_v1_latest_complete_figures/final_v1_scores.png)")
    lines.append("")
    lines.append("![36小时最差窗口收益](final_v1_latest_complete_figures/final_v1_worst144.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    args = parser.parse_args()

    raw, run_cover = base.read_all_monthly_runs(Path(args.source_root))
    latest_run, max_file_count = choose_latest_complete_run(run_cover)
    raw_latest = raw[raw["run_name"] == latest_run].copy()

    quotes = base.prepare_quotes(raw_latest)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError(f"No usable features built for latest complete run: {latest_run}")
    features["pred_prob_up_logit"] = v1.rolling_logit_safe(features)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
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
        lg, sm = v1.simulate(features, s)
        logs_list.append(lg)
        summaries.append(sm)
    logs = pd.concat([x for x in logs_list if not x.empty], ignore_index=True) if logs_list else pd.DataFrame()
    summary = v1.summarize(pd.DataFrame(summaries))
    sess = v1.breakdown(logs)

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "final_v1_latest_complete_figures"
    report_dir.mkdir(parents=True, exist_ok=True)
    figs = v1.make_plots(summary, fig_dir)

    summary.to_csv(report_dir / "final_v1_latest_complete_summary.csv", index=False)
    sess.to_csv(report_dir / "final_v1_latest_complete_session_breakdown.csv", index=False)
    coverage_out = run_cover.sort_values("run_name").reset_index(drop=True)
    coverage_out.to_csv(report_dir / "monthly_runs_coverage_snapshot.csv", index=False)
    (report_dir / "final_v1_latest_complete_report.md").write_text(
        build_report(latest_run, max_file_count, coverage_out, summary, sess),
        encoding="utf-8",
    )
    (report_dir / "latest_complete_run_metadata.json").write_text(
        json.dumps(
            {
                "latest_complete_run": latest_run,
                "max_file_count": max_file_count,
                "rows_features": int(len(features)),
                "rows_summary": int(len(summary)),
                "rows_logs": int(len(logs)),
                "rows_sessions": int(len(sess)),
                "figure_count": len(figs),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(
        {
            "latest_complete_run": latest_run,
            "max_file_count": max_file_count,
            "rows_features": int(len(features)),
            "rows_summary": int(len(summary)),
            "rows_logs": int(len(logs)),
            "rows_sessions": int(len(sess)),
            "figure_count": len(figs),
        }
    )


if __name__ == "__main__":
    main()

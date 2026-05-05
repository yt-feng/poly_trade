from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import monthly_runs_quant_research_framework as qrf

MIN_TRAIN_RUNS = 3
WINDOW_72H = qrf.WINDOW_72H


def prepare_complete_features(source_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    raw, cover = base.read_all_monthly_runs(source_root)
    cover = cover.sort_values("run_name").reset_index(drop=True)
    if cover.empty:
        raise RuntimeError("No monthly_runs coverage found")
    max_files = int(cover["file_count"].max())
    cover["is_complete_run"] = cover["file_count"] == max_files
    complete_runs = cover.loc[cover["is_complete_run"], "run_name"].astype(str).tolist()
    if len(complete_runs) < MIN_TRAIN_RUNS + 2:
        raise RuntimeError(f"Need at least {MIN_TRAIN_RUNS + 2} complete runs, found {len(complete_runs)}")

    raw_complete = raw[raw["run_name"].astype(str).isin(complete_runs)].copy()
    quotes = base.prepare_quotes(raw_complete)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable features built from complete monthly_runs")
    features["pred_prob_up_logit"] = qrf.rolling_logit_safe(features)
    features = qrf.v1.add_session_labels(features)
    features = qrf.v1.add_quality_features(features)
    features = qrf.add_research_features(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    return features, cover, complete_runs


def make_run_folds(complete_runs: List[str], min_train_runs: int = MIN_TRAIN_RUNS) -> List[Dict[str, object]]:
    folds: List[Dict[str, object]] = []
    # train = complete_runs[:i], validation = complete_runs[i], test = complete_runs[i + 1]
    for i in range(min_train_runs, len(complete_runs) - 1):
        folds.append(
            {
                "fold_id": len(folds) + 1,
                "train_runs": complete_runs[:i],
                "validation_run": complete_runs[i],
                "test_run": complete_runs[i + 1],
            }
        )
    return folds


def validation_score(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    out["score_return"] = out["total_return"].rank(pct=True)
    out["score_dd"] = (-out["max_drawdown"].fillna(999)).rank(pct=True)
    out["score_pf"] = out["profit_factor"].fillna(0).rank(pct=True)
    out["score_w36"] = out["worst_144_return"].fillna(-999).rank(pct=True)
    out["score_trades"] = np.where(out["trades"] >= 3, 1.0, 0.35)
    out["wf_validation_score"] = (
        0.30 * out["score_return"]
        + 0.25 * out["score_dd"]
        + 0.20 * out["score_pf"]
        + 0.20 * out["score_w36"]
        + 0.05 * out["score_trades"]
    )
    return out.sort_values("wf_validation_score", ascending=False).reset_index(drop=True)


def eval_strategy_set(df: pd.DataFrame, split_name: str, fold_id: int, run_label: str, family_map: Dict[str, str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    logs = []
    for spec in qrf.strategy_registry():
        lg, met = qrf.simulate(df, spec.name)
        met["split"] = split_name
        met["fold_id"] = fold_id
        met["run_label"] = run_label
        met["family"] = family_map[spec.name]
        rows.append(met)
        if not lg.empty:
            lg["split"] = split_name
            lg["fold_id"] = fold_id
            lg["run_label"] = run_label
            logs.append(lg)
    return pd.DataFrame(rows), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def run_walk_forward(features: pd.DataFrame, folds: List[Dict[str, object]]) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    family_map = {s.name: s.family for s in qrf.strategy_registry()}
    all_metrics: List[pd.DataFrame] = []
    all_logs: List[pd.DataFrame] = []
    selections: List[Dict[str, object]] = []
    selected_perf: List[pd.DataFrame] = []

    for fold in folds:
        fold_id = int(fold["fold_id"])
        train_runs = list(fold["train_runs"])
        val_run = str(fold["validation_run"])
        test_run = str(fold["test_run"])
        train_df = features[features["run_name"].isin(train_runs)].copy()
        val_df = features[features["run_name"] == val_run].copy()
        test_df = features[features["run_name"] == test_run].copy()

        train_m, train_l = eval_strategy_set(train_df, "train", fold_id, "+".join(train_runs), family_map)
        val_m, val_l = eval_strategy_set(val_df, "validation", fold_id, val_run, family_map)
        test_m, test_l = eval_strategy_set(test_df, "test", fold_id, test_run, family_map)
        ranked = validation_score(val_m)
        selected = str(ranked.iloc[0]["strategy"])
        selections.append(
            {
                "fold_id": fold_id,
                "train_runs": ",".join(train_runs),
                "validation_run": val_run,
                "test_run": test_run,
                "selected_strategy": selected,
                "selected_family": family_map[selected],
                "validation_score": float(ranked.iloc[0]["wf_validation_score"]),
                "validation_return": float(ranked.iloc[0]["total_return"]),
                "validation_drawdown": float(ranked.iloc[0]["max_drawdown"]),
            }
        )
        chosen_rows = pd.concat([train_m, val_m, test_m], ignore_index=True)
        chosen_rows = chosen_rows[chosen_rows["strategy"] == selected].copy()
        chosen_rows.insert(0, "selected_strategy", selected)
        selected_perf.append(chosen_rows)
        all_metrics.extend([train_m, val_m, test_m])
        for lg in [train_l, val_l, test_l]:
            if not lg.empty:
                all_logs.append(lg)

    metrics = pd.concat(all_metrics, ignore_index=True) if all_metrics else pd.DataFrame()
    logs = pd.concat(all_logs, ignore_index=True) if all_logs else pd.DataFrame()
    selections_df = pd.DataFrame(selections)
    selected_perf_df = pd.concat(selected_perf, ignore_index=True) if selected_perf else pd.DataFrame()
    return metrics, logs, selections_df, selected_perf_df


def aggregate_test_metrics(metrics: pd.DataFrame, selections: pd.DataFrame) -> pd.DataFrame:
    test = metrics[metrics["split"] == "test"].copy()
    if test.empty:
        return pd.DataFrame()
    grp = test.groupby(["strategy", "family"], as_index=False).agg(
        folds_tested=("fold_id", "nunique"),
        avg_test_return=("total_return", "mean"),
        median_test_return=("total_return", "median"),
        min_test_return=("total_return", "min"),
        pct_positive_test=("total_return", lambda s: float((s > 0).mean())),
        avg_test_drawdown=("max_drawdown", "mean"),
        max_test_drawdown=("max_drawdown", "max"),
        avg_test_win_rate=("win_rate", "mean"),
        avg_profit_factor=("profit_factor", "mean"),
    )
    selected_counts = selections.groupby("selected_strategy").size().rename("selected_count").reset_index()
    grp = grp.merge(selected_counts, left_on="strategy", right_on="selected_strategy", how="left").drop(columns=["selected_strategy"])
    grp["selected_count"] = grp["selected_count"].fillna(0).astype(int)
    grp["robust_wf_score"] = (
        0.30 * grp["avg_test_return"].rank(pct=True)
        + 0.25 * grp["pct_positive_test"].rank(pct=True)
        + 0.20 * (-grp["max_test_drawdown"].fillna(999)).rank(pct=True)
        + 0.15 * grp["avg_profit_factor"].fillna(0).rank(pct=True)
        + 0.10 * grp["selected_count"].rank(pct=True)
    )
    return grp.sort_values(["robust_wf_score", "avg_test_return"], ascending=False).reset_index(drop=True)


def latest72h_health(features: pd.DataFrame) -> pd.DataFrame:
    tail = features.iloc[-WINDOW_72H:].copy() if len(features) >= WINDOW_72H else features.copy()
    family_map = {s.name: s.family for s in qrf.strategy_registry()}
    rows = []
    for spec in qrf.strategy_registry():
        _, met = qrf.simulate(tail, spec.name)
        met["family"] = family_map[spec.name]
        met["split"] = "latest72h_complete_only"
        rows.append(met)
    return pd.DataFrame(rows).sort_values("total_return", ascending=False).reset_index(drop=True)


def md_table(df: pd.DataFrame, n: int = 30) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(n).copy()
    nums = show.select_dtypes(include=[np.number]).columns
    show[nums] = show[nums].round(4)
    return show.to_markdown(index=False)


def make_plots(outdir: Path, wf_summary: pd.DataFrame, latest72: pd.DataFrame) -> int:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return 0
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    count = 0
    if not wf_summary.empty:
        top = wf_summary.head(15)
        plt.figure(figsize=(14, 5))
        plt.bar(top["strategy"], top["avg_test_return"])
        plt.xticks(rotation=55, ha="right")
        plt.title("Walk-forward average test return")
        plt.tight_layout()
        plt.savefig(figdir / "walk_forward_avg_test_return.png", dpi=150)
        plt.close(); count += 1
        plt.figure(figsize=(14, 5))
        plt.bar(top["strategy"], top["pct_positive_test"])
        plt.xticks(rotation=55, ha="right")
        plt.title("Walk-forward positive test fold rate")
        plt.tight_layout()
        plt.savefig(figdir / "walk_forward_positive_test_rate.png", dpi=150)
        plt.close(); count += 1
    if not latest72.empty:
        top = latest72.head(15)
        plt.figure(figsize=(14, 5))
        plt.bar(top["strategy"], top["total_return"])
        plt.xticks(rotation=55, ha="right")
        plt.title("Latest 72h health check return, complete runs only")
        plt.tight_layout()
        plt.savefig(figdir / "latest72h_returns.png", dpi=150)
        plt.close(); count += 1
    return count


def build_report(coverage: pd.DataFrame, complete_runs: List[str], folds: List[Dict[str, object]], selections: pd.DataFrame, selected_perf: pd.DataFrame, wf_summary: pd.DataFrame, latest72: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# monthly_runs walk-forward validation")
    lines.append("")
    lines.append("这份报告只使用 `data/monthly_runs/*` 中当前最新且完整的 run。完整 run 的定义是：`file_count == 当前 monthly_runs 中的最大 file_count`。不完整的最新 partial run 不参与 walk-forward 选策略。")
    lines.append("")
    lines.append("## 数据覆盖与完整 run 标记")
    lines.append(md_table(coverage, 120))
    lines.append("")
    lines.append("## 参与 walk-forward 的完整 run")
    lines.append("`" + "`, `".join(complete_runs) + "`")
    lines.append("")
    lines.append("## walk-forward 设计")
    lines.append("- 每一折：train = 更早的完整 run；validation = 下一个完整 run；test = 再下一个完整 run。")
    lines.append("- 每一折只用 validation 选择策略，test 作为真正盲测。")
    lines.append("- latest72h 只用完整 run 的末尾 864 个 5分钟事件做健康检查，不参与选策略。")
    lines.append("")
    lines.append("## fold 设计")
    lines.append(md_table(pd.DataFrame(folds), 50))
    lines.append("")
    lines.append("## 每折 validation 选出的策略")
    lines.append(md_table(selections, 50))
    lines.append("")
    lines.append("## 被选策略在各 split 的表现")
    lines.append(md_table(selected_perf, 80))
    lines.append("")
    lines.append("## 全策略 walk-forward test 聚合排名")
    lines.append(md_table(wf_summary, 30))
    lines.append("")
    lines.append("## latest72h 健康检查，complete runs only")
    lines.append(md_table(latest72, 30))
    lines.append("")
    if not wf_summary.empty:
        best = wf_summary.iloc[0]
        lines.append("## 当前 walk-forward 首选")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- family：`{best['family']}`")
        lines.append(f"- 平均 test 收益：**{best['avg_test_return']:.2%}**")
        lines.append(f"- test 正收益折数占比：**{best['pct_positive_test']:.2%}**")
        lines.append(f"- 最大 test 回撤：**{best['max_test_drawdown']:.2%}**")
        lines.append(f"- validation 被选次数：**{int(best['selected_count'])}**")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args()

    features, coverage, complete_runs = prepare_complete_features(Path(args.source_root))
    folds = make_run_folds(complete_runs)
    metrics, logs, selections, selected_perf = run_walk_forward(features, folds)
    wf_summary = aggregate_test_metrics(metrics, selections)
    latest72 = latest72h_health(features)

    outdir = Path(args.report_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    fig_count = make_plots(outdir, wf_summary, latest72)

    coverage.to_csv(outdir / "coverage_complete_runs.csv", index=False)
    pd.DataFrame(folds).to_csv(outdir / "walk_forward_folds.csv", index=False)
    metrics.to_csv(outdir / "walk_forward_strategy_metrics.csv", index=False)
    logs.to_csv(outdir / "walk_forward_trade_logs.csv", index=False)
    selections.to_csv(outdir / "walk_forward_selected_by_fold.csv", index=False)
    selected_perf.to_csv(outdir / "walk_forward_selected_strategy_performance.csv", index=False)
    wf_summary.to_csv(outdir / "walk_forward_strategy_summary.csv", index=False)
    latest72.to_csv(outdir / "latest72h_complete_only_health.csv", index=False)
    (outdir / "walk_forward_report.md").write_text(build_report(coverage, complete_runs, folds, selections, selected_perf, wf_summary, latest72), encoding="utf-8")
    (outdir / "meta.json").write_text(json.dumps({"complete_runs": complete_runs, "fold_count": len(folds), "rows_features": int(len(features)), "rows_metrics": int(len(metrics)), "rows_logs": int(len(logs)), "figure_count": fig_count}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"complete_runs": complete_runs, "fold_count": len(folds), "rows_features": int(len(features)), "rows_metrics": int(len(metrics)), "rows_logs": int(len(logs)), "figure_count": fig_count})


if __name__ == "__main__":
    main()

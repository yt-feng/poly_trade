from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1

WINDOW_72H = 72 * 60 // 5  # 864 five-minute windows


def build_report(coverage: pd.DataFrame, logs: pd.DataFrame, latest_ts: str, metrics: dict) -> str:
    lines: list[str] = []
    lines.append("# v1_active_fill_mix 最近72小时快照")
    lines.append("")
    lines.append("这份报告只看 `v1_active_fill_mix`，并回答一个问题：")
    lines.append("")
    lines.append("- 如果从当前数据末端往回看 72 小时，在这段最近窗口里，这个策略表现如何？")
    lines.append("")
    lines.append("## 当前 monthly_runs 覆盖")
    lines.append("")
    lines.append(v1.markdown_table(coverage, rows=100))
    lines.append("")
    lines.append(f"## 当前数据末端时间\n\n- latest timestamp: **{latest_ts}**")
    lines.append("")
    lines.append("## 最近72小时结果")
    lines.append("")
    lines.append(f"- 是否数据充足：**{metrics['enough_72h_data']}**")
    lines.append(f"- 最近72小时覆盖的5分钟窗口数：**{metrics['window_count']}**")
    lines.append(f"- 最近72小时交易笔数：**{metrics['trades_72h']}**")
    lines.append(f"- 最近72小时总收益率：**{metrics['return_72h']:.2%}**")
    lines.append(f"- 最近72小时期末本金（从100起算）：**{metrics['ending_bankroll_72h']:.2f} USD**")
    lines.append(f"- 最近72小时最大回撤：**{metrics['max_drawdown_72h']:.2%}**")
    lines.append(f"- 最近72小时胜率：**{metrics['win_rate_72h']:.2%}**")
    lines.append(f"- 最近72小时利润因子：**{metrics['profit_factor_72h']:.4f}**")
    lines.append("")
    lines.append("## 最近72小时交易明细")
    lines.append("")
    lines.append(v1.markdown_table(logs.sort_values("first_quote_ts"), rows=100))
    lines.append("")
    return "\n".join(lines)


def max_drawdown_from_returns(event_returns: np.ndarray) -> float:
    wealth = np.cumprod(1.0 + event_returns)
    if len(wealth) == 0:
        return 0.0
    peak = np.maximum.accumulate(wealth)
    dd = (peak - wealth) / peak
    return float(np.nanmax(dd)) if len(dd) else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    args = parser.parse_args()

    raw, run_cover = base.read_all_monthly_runs(Path(args.source_root))
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable features built from monthly_runs")
    features["pred_prob_up_logit"] = v1.rolling_logit_safe(features)
    features = v1.add_session_labels(features)
    features = v1.add_quality_features(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)

    logs, _ = v1.simulate(features, "v1_active_fill_mix")
    latest_ts = str(features["first_quote_ts"].max())
    if len(features) >= WINDOW_72H:
        tail = features.iloc[-WINDOW_72H:].copy()
        cutoff = tail["first_quote_ts"].min()
        tail_logs = logs[logs["first_quote_ts"] >= cutoff].copy() if not logs.empty else pd.DataFrame()
        event_returns = np.zeros(len(tail), dtype=float)
        if not tail_logs.empty:
            tail_index = {ts: i for i, ts in enumerate(tail["first_quote_ts"].tolist())}
            for _, r in tail_logs.iterrows():
                idx = tail_index.get(r["first_quote_ts"])
                if idx is not None:
                    event_returns[idx] = float(r["event_ret"])
        pnl = pd.to_numeric(tail_logs.get("pnl_usd"), errors="coerce").dropna() if not tail_logs.empty else pd.Series(dtype=float)
        wins = pnl[pnl > 0].sum() if not pnl.empty else 0.0
        losses = pnl[pnl < 0].sum() if not pnl.empty else 0.0
        wealth = float(np.prod(1.0 + event_returns))
        metrics = {
            "enough_72h_data": True,
            "window_count": int(len(tail)),
            "trades_72h": int(len(tail_logs)),
            "return_72h": float(wealth - 1.0),
            "ending_bankroll_72h": float(100.0 * wealth),
            "max_drawdown_72h": max_drawdown_from_returns(event_returns),
            "win_rate_72h": float((pnl > 0).mean()) if len(pnl) else 0.0,
            "profit_factor_72h": float(wins / abs(losses)) if losses != 0 else np.nan,
        }
    else:
        tail_logs = pd.DataFrame()
        metrics = {
            "enough_72h_data": False,
            "window_count": int(len(features)),
            "trades_72h": 0,
            "return_72h": np.nan,
            "ending_bankroll_72h": np.nan,
            "max_drawdown_72h": np.nan,
            "win_rate_72h": np.nan,
            "profit_factor_72h": np.nan,
        }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    coverage_out = run_cover.sort_values("run_name").reset_index(drop=True)
    coverage_out.to_csv(report_dir / "monthly_runs_coverage_snapshot.csv", index=False)
    if not tail_logs.empty:
        tail_logs.to_csv(report_dir / "v1_active_fill_mix_recent_72h_trades.csv", index=False)
    (report_dir / "v1_active_fill_mix_recent_72h_report.md").write_text(
        build_report(coverage_out, tail_logs, latest_ts, metrics),
        encoding="utf-8",
    )
    (report_dir / "v1_active_fill_mix_recent_72h_metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print({"latest_ts": latest_ts, **metrics})


if __name__ == "__main__":
    main()

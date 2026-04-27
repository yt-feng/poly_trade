from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

FILE_GLOB = "*btc-updown-5m_quotes.csv"
ENTRY_MINUTES = [1, 2, 3, 4]


def first_non_null(series: pd.Series):
    non_null = series.dropna()
    return np.nan if non_null.empty else non_null.iloc[0]


def last_non_null(series: pd.Series):
    non_null = series.dropna()
    return np.nan if non_null.empty else non_null.iloc[-1]


def parse_close_ts_from_slug(slug: str) -> pd.Timestamp:
    m = re.search(r"(\d{10})$", str(slug))
    if not m:
        return pd.NaT
    return pd.to_datetime(int(m.group(1)), unit="s", utc=True)


def read_source_files(source_dir: Path) -> pd.DataFrame:
    files = sorted(source_dir.glob(FILE_GLOB))
    if not files:
        raise FileNotFoundError(f"No files matching {FILE_GLOB} under {source_dir}")
    frames = []
    for path in files:
        df = pd.read_csv(path)
        df["source_file"] = path.name
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    raw.columns = [str(c).strip() for c in raw.columns]
    return raw


def prepare_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    q = raw.copy()
    q["ts_utc"] = pd.to_datetime(q["ts_iso"], utc=True, errors="coerce")
    for col in [
        "buy_up_cents","buy_down_cents","sell_up_cents","sell_down_cents",
        "buy_up_size","buy_down_size","sell_up_size","sell_down_size",
        "target_price","final_price",
    ]:
        if col in q.columns:
            q[col] = pd.to_numeric(q[col], errors="coerce")
    q["close_ts_utc"] = q["slug"].map(parse_close_ts_from_slug)
    q = q.sort_values(["slug","ts_utc","source_file"]).drop_duplicates(subset=["ts_iso","slug"], keep="last")
    return q.reset_index(drop=True)


def build_entry_features(quotes: pd.DataFrame, entry_minute: int, fee: float) -> pd.DataFrame:
    rows: List[Dict[str, float]] = []
    for slug, g in quotes.groupby("slug", dropna=False):
        g = g.sort_values("ts_utc").copy()
        if g.empty:
            continue
        first_ts = g["ts_utc"].min()
        cutoff = first_ts + pd.Timedelta(minutes=entry_minute)
        snap = g[g["ts_utc"] <= cutoff].copy()
        if snap.empty:
            continue
        row = snap.iloc[-1]
        target = first_non_null(g["target_price"])
        final = last_non_null(g["final_price"])
        if pd.isna(target) or pd.isna(final):
            continue
        outcome_up = float(final > target)
        move = np.nan if pd.isna(row.get("final_price")) else float(row["final_price"] - target)
        buy_up = pd.to_numeric(row.get("buy_up_cents"), errors="coerce")
        buy_down = pd.to_numeric(row.get("buy_down_cents"), errors="coerce")
        buy_up_price = np.nan if pd.isna(buy_up) else float(buy_up) / 100.0
        buy_down_price = np.nan if pd.isna(buy_down) else float(buy_down) / 100.0
        buy_up_size = pd.to_numeric(row.get("buy_up_size"), errors="coerce")
        buy_down_size = pd.to_numeric(row.get("buy_down_size"), errors="coerce")
        rows.append({
            "slug": slug,
            "first_quote_ts": first_ts,
            "entry_minute": entry_minute,
            "remaining_minutes": float(5 - entry_minute),
            "btc_move_entry": move,
            "buy_up_price": buy_up_price,
            "buy_down_price": buy_down_price,
            "buy_up_size": buy_up_size,
            "buy_down_size": buy_down_size,
            "outcome_up": outcome_up,
            "pnl_up_per_share": np.nan if pd.isna(buy_up_price) else float(outcome_up - buy_up_price - fee),
            "pnl_down_per_share": np.nan if pd.isna(buy_down_price) else float((1.0 - outcome_up) - buy_down_price - fee),
        })
    return pd.DataFrame(rows).sort_values("first_quote_ts").reset_index(drop=True)


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    move = out["btc_move_entry"]
    out["rule_drop10_down"] = np.where(move <= -10, "buy_down", "skip")
    out["rule_drop20_down"] = np.where(move <= -20, "buy_down", "skip")
    out["rule_drop10_to50_down"] = np.where((move <= -10) & (move > -50), "buy_down", "skip")
    out["rule_rise30to50_up"] = np.where((move > 30) & (move <= 50), "buy_up", "skip")
    return out


def max_loss_streak(pnls: List[float]) -> int:
    best = 0
    cur = 0
    for x in pnls:
        if x < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def simulate(df: pd.DataFrame, signal_col: str, fixed_frac: float, fee: float, starting_bankroll: float = 100.0) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = starting_bankroll
    peak = starting_bankroll
    max_dd = 0.0
    trade_rows = []
    pnl_list: List[float] = []
    for _, row in df.iterrows():
        side = row[signal_col]
        if side not in {"buy_up", "buy_down"}:
            continue
        price = row["buy_up_price"] if side == "buy_up" else row["buy_down_price"]
        size_avail = row["buy_up_size"] if side == "buy_up" else row["buy_down_size"]
        pnl_per_share = row["pnl_up_per_share"] if side == "buy_up" else row["pnl_down_per_share"]
        if pd.isna(price) or pd.isna(size_avail) or pd.isna(pnl_per_share) or price <= 0:
            continue
        target_cost = min(bankroll * fixed_frac, bankroll, float(size_avail) * float(price))
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        pnl_list.append(float(pnl))
        trade_rows.append({
            "strategy": signal_col,
            "sizing": f"fixed_{int(fixed_frac*100)}pct",
            "entry_minute": int(row["entry_minute"]),
            "remaining_minutes": float(row["remaining_minutes"]),
            "first_quote_ts": row["first_quote_ts"],
            "slug": row["slug"],
            "side": side,
            "target_cost": target_cost,
            "shares": shares,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
        })
    trade_log = pd.DataFrame(trade_rows)
    summary = {
        "strategy": signal_col,
        "sizing": f"fixed_{int(fixed_frac*100)}pct",
        "entry_minute": int(df["entry_minute"].iloc[0]) if not df.empty else np.nan,
        "remaining_minutes": float(df["remaining_minutes"].iloc[0]) if not df.empty else np.nan,
        "trades": int(len(trade_log)),
        "ending_bankroll": float(trade_log["bankroll_after"].iloc[-1]) if not trade_log.empty else starting_bankroll,
        "total_return": float((trade_log["bankroll_after"].iloc[-1] / starting_bankroll) - 1.0) if not trade_log.empty else 0.0,
        "avg_trade_return_on_cost": float((trade_log["pnl_usd"] / trade_log["target_cost"]).mean()) if not trade_log.empty else np.nan,
        "win_rate": float((trade_log["pnl_usd"] > 0).mean()) if not trade_log.empty else np.nan,
        "max_drawdown": float(max_dd),
        "max_consecutive_losses": int(max_loss_streak(pnl_list)),
    }
    return trade_log, summary


def run_all(quotes: pd.DataFrame, fee: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    logs = []
    summaries = []
    for entry_minute in ENTRY_MINUTES:
        feat = add_signals(build_entry_features(quotes, entry_minute, fee))
        for strat in ["rule_drop10_down", "rule_drop20_down", "rule_drop10_to50_down", "rule_rise30to50_up"]:
            for frac in [0.20, 0.25]:
                lg, sm = simulate(feat, strat, frac, fee)
                logs.append(lg)
                summaries.append(sm)
    logs_df = pd.concat([x for x in logs if not x.empty], ignore_index=True) if logs else pd.DataFrame()
    summary_df = pd.DataFrame(summaries).sort_values(["strategy", "entry_minute", "ending_bankroll"], ascending=[True, True, False]).reset_index(drop=True)
    return logs_df, summary_df


def make_plots(summary: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not summary.empty:
        for metric, filename, title in [
            ("ending_bankroll", "entry_timing_ending_bankroll.png", "不同入场时点的期末本金"),
            ("max_drawdown", "entry_timing_max_drawdown.png", "不同入场时点的最大回撤"),
            ("win_rate", "entry_timing_win_rate.png", "不同入场时点的胜率"),
        ]:
            pivot = summary.pivot_table(index="entry_minute", columns="strategy", values=metric, aggfunc="max")
            plt.figure(figsize=(10, 4))
            for col in pivot.columns:
                plt.plot(pivot.index, pivot[col], marker="o", label=col)
            plt.legend()
            plt.xlabel("entry_minute")
            plt.title(title)
            plt.tight_layout()
            p = fig_dir / filename
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))
    return paths


def markdown_table(df: pd.DataFrame, rows: int = 50) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def build_report(summary: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# 入场时点与剩余持有时间体验分析")
    lines.append("")
    lines.append("这份报告用于观察：如果在不同的时间入场，剩余交易时间不够长，是否会显著降低体验。")
    lines.append("")
    lines.append("## 回测设置")
    lines.append("")
    lines.append("- 入场时点：第 1 / 2 / 3 / 4 分钟")
    lines.append("- 代表性策略：drop10_down / drop20_down / drop10_to50_down / rise30to50_up")
    lines.append("- 仓位：fixed 20% / fixed 25%")
    lines.append("")
    lines.append("## 结果表")
    lines.append("")
    lines.append(markdown_table(summary.sort_values(["ending_bankroll"], ascending=False), rows=100))
    lines.append("")
    if not summary.empty:
        best = summary.sort_values("ending_bankroll", ascending=False).iloc[0]
        worst = summary.sort_values("ending_bankroll", ascending=True).iloc[0]
        lines.append("## 观察")
        lines.append("")
        lines.append(f"- 最佳组合：**{best['strategy']} | {best['sizing']} | 第{int(best['entry_minute'])}分钟入场**，期末本金 **{best['ending_bankroll']:.2f} USD**。")
        lines.append(f"- 最差组合：**{worst['strategy']} | {worst['sizing']} | 第{int(worst['entry_minute'])}分钟入场**，期末本金 **{worst['ending_bankroll']:.2f} USD**。")
        lines.append("- 重点看 `max_drawdown`、`win_rate` 和 `max_consecutive_losses`，它们更能反映“体验差不差”。")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("### 不同入场时点的期末本金")
    lines.append("")
    lines.append("![不同入场时点的期末本金](entry_timing_figures/entry_timing_ending_bankroll.png)")
    lines.append("")
    lines.append("### 不同入场时点的最大回撤")
    lines.append("")
    lines.append("![不同入场时点的最大回撤](entry_timing_figures/entry_timing_max_drawdown.png)")
    lines.append("")
    lines.append("### 不同入场时点的胜率")
    lines.append("")
    lines.append("![不同入场时点的胜率](entry_timing_figures/entry_timing_win_rate.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "entry_timing_figures"
    report_dir.mkdir(parents=True, exist_ok=True)

    raw = read_source_files(Path(args.source_dir))
    quotes = prepare_quotes(raw)
    logs, summary = run_all(quotes, fee=args.fee)
    figs = make_plots(summary, fig_dir)
    logs.to_csv(report_dir / "entry_timing_trade_logs.csv", index=False)
    summary.to_csv(report_dir / "entry_timing_summary.csv", index=False)
    (report_dir / "entry_timing_experience_report.md").write_text(build_report(summary), encoding="utf-8")
    (report_dir / "entry_timing_summary.json").write_text(json.dumps({"rows_summary": int(len(summary)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_summary": int(len(summary)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

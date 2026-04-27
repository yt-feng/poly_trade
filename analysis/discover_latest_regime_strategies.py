from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

FILE_GLOB = "*btc-updown-5m_quotes.csv"
FIXED_FRACS = [0.10, 0.15, 0.20, 0.25]
ENTRY_MINUTES = [1, 2, 4]


def first_non_null(series: pd.Series):
    s = series.dropna()
    return np.nan if s.empty else s.iloc[0]


def last_non_null(series: pd.Series):
    s = series.dropna()
    return np.nan if s.empty else s.iloc[-1]


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
    for p in files:
        df = pd.read_csv(p)
        df["source_file"] = p.name
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    raw.columns = [str(c).strip() for c in raw.columns]
    return raw


def prepare_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    q = raw.copy()
    q["ts_utc"] = pd.to_datetime(q["ts_iso"], utc=True, errors="coerce")
    for c in ["buy_up_cents","buy_down_cents","buy_up_size","buy_down_size","target_price","final_price"]:
        if c in q.columns:
            q[c] = pd.to_numeric(q[c], errors="coerce")
    q["close_ts_utc"] = q["slug"].map(parse_close_ts_from_slug)
    q = q.sort_values(["slug","ts_utc","source_file"]).drop_duplicates(subset=["ts_iso","slug"], keep="last")
    return q.reset_index(drop=True)


def build_snapshot(quotes: pd.DataFrame, minute: int) -> pd.DataFrame:
    rows: List[Dict[str, float]] = []
    for slug, g in quotes.groupby("slug", dropna=False):
        g = g.sort_values("ts_utc").copy()
        if g.empty:
            continue
        first_ts = g["ts_utc"].min()
        snap = g[g["ts_utc"] <= first_ts + pd.Timedelta(minutes=minute)].copy()
        if snap.empty:
            continue
        row = snap.iloc[-1]
        target = first_non_null(g["target_price"])
        final = last_non_null(g["final_price"])
        if pd.isna(target) or pd.isna(final):
            continue
        move = np.nan if pd.isna(row.get("final_price")) else float(row["final_price"] - target)
        up = pd.to_numeric(row.get("buy_up_cents"), errors="coerce")
        down = pd.to_numeric(row.get("buy_down_cents"), errors="coerce")
        rows.append({
            "slug": slug,
            "first_quote_ts": first_ts,
            "outcome_up": float(final > target),
            f"move_m{minute}": move,
            f"buy_up_price_m{minute}": np.nan if pd.isna(up) else float(up) / 100.0,
            f"buy_down_price_m{minute}": np.nan if pd.isna(down) else float(down) / 100.0,
            f"buy_up_size_m{minute}": pd.to_numeric(row.get("buy_up_size"), errors="coerce"),
            f"buy_down_size_m{minute}": pd.to_numeric(row.get("buy_down_size"), errors="coerce"),
        })
    return pd.DataFrame(rows)


def build_wide(quotes: pd.DataFrame) -> pd.DataFrame:
    out = None
    for m in ENTRY_MINUTES:
        snap = build_snapshot(quotes, m)
        out = snap if out is None else out.merge(snap, on=["slug","first_quote_ts","outcome_up"], how="outer")
    return out.sort_values("first_quote_ts").reset_index(drop=True)


def choose_trade(row: pd.Series, strategy: str) -> Tuple[str, int]:
    m1, m2, m4 = row.get("move_m1", np.nan), row.get("move_m2", np.nan), row.get("move_m4", np.nan)
    p1d, p2u, p2d, p4u = row.get("buy_down_price_m1", np.nan), row.get("buy_up_price_m2", np.nan), row.get("buy_down_price_m2", np.nan), row.get("buy_up_price_m4", np.nan)
    if strategy == "latest_m1_drop20_down":
        return ("buy_down", 1) if pd.notna(m1) and m1 <= -20 else ("skip", -1)
    if strategy == "latest_m2_sharpdrop_reversal_up":
        return ("buy_up", 2) if pd.notna(m2) and -50 < m2 <= -30 else ("skip", -1)
    if strategy == "latest_m2_milddrop_down":
        return ("buy_down", 2) if pd.notna(m2) and -30 < m2 <= -10 else ("skip", -1)
    if strategy == "latest_m2_neutral_down":
        return ("buy_down", 2) if pd.notna(m2) and -10 < m2 <= 10 else ("skip", -1)
    if strategy == "latest_m2_extremeup_fade_down":
        return ("buy_down", 2) if pd.notna(m2) and m2 >= 50 else ("skip", -1)
    if strategy == "latest_m4_rise30to50_up":
        return ("buy_up", 4) if pd.notna(m4) and 30 < m4 <= 50 else ("skip", -1)
    if strategy == "latest_switch_v1":
        if pd.notna(m1) and m1 <= -20 and pd.notna(p1d) and p1d <= 0.90:
            return ("buy_down", 1)
        if pd.notna(m2) and -50 < m2 <= -30 and pd.notna(p2u) and p2u <= 0.40:
            return ("buy_up", 2)
        if pd.notna(m2) and -30 < m2 <= -10 and pd.notna(p2d) and p2d <= 0.75:
            return ("buy_down", 2)
        if pd.notna(m2) and m2 >= 50 and pd.notna(p2d) and p2d <= 0.20:
            return ("buy_down", 2)
        if pd.notna(m4) and 30 < m4 <= 50 and pd.notna(p4u) and p4u <= 0.90:
            return ("buy_up", 4)
        return ("skip", -1)
    if strategy == "latest_switch_v2":
        if pd.notna(m2) and -50 < m2 <= -30 and pd.notna(p2u) and p2u <= 0.35:
            return ("buy_up", 2)
        if pd.notna(m1) and m1 <= -20 and pd.notna(p1d) and p1d <= 0.85:
            return ("buy_down", 1)
        if pd.notna(m4) and 30 < m4 <= 50 and pd.notna(p4u) and p4u <= 0.90:
            return ("buy_up", 4)
        if pd.notna(m2) and m2 >= 50 and pd.notna(p2d) and p2d <= 0.20:
            return ("buy_down", 2)
        return ("skip", -1)
    raise ValueError(strategy)


def payout(row: pd.Series, side: str, minute: int, fee: float) -> Tuple[float, float, float]:
    if side == "buy_up":
        price = row[f"buy_up_price_m{minute}"]
        size = row[f"buy_up_size_m{minute}"]
        pnl_per_share = row["outcome_up"] - price - fee
    else:
        price = row[f"buy_down_price_m{minute}"]
        size = row[f"buy_down_size_m{minute}"]
        pnl_per_share = (1.0 - row["outcome_up"]) - price - fee
    return float(price), float(size), float(pnl_per_share)


def simulate(df: pd.DataFrame, strategy: str, frac: float, fee: float) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll, peak, max_dd = 100.0, 100.0, 0.0
    rows = []
    for _, row in df.iterrows():
        side, minute = choose_trade(row, strategy)
        if side == "skip":
            continue
        price, size_avail, pnl_per_share = payout(row, side, minute, fee)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        target_cost = min(bankroll * frac, bankroll, size_avail * price)
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        rows.append({"strategy": strategy, "sizing": f"fixed_{int(frac*100)}pct", "first_quote_ts": row["first_quote_ts"], "slug": row["slug"], "entry_minute": minute, "side": side, "target_cost": target_cost, "shares": shares, "entry_price": price, "pnl_usd": pnl, "bankroll_after": bankroll})
    trade_log = pd.DataFrame(rows)
    summary = {"strategy": strategy, "sizing": f"fixed_{int(frac*100)}pct", "trades": int(len(trade_log)), "ending_bankroll": float(trade_log["bankroll_after"].iloc[-1]) if not trade_log.empty else 100.0, "total_return": float(trade_log["bankroll_after"].iloc[-1] / 100.0 - 1.0) if not trade_log.empty else 0.0, "avg_trade_return_on_cost": float((trade_log["pnl_usd"] / trade_log["target_cost"]).mean()) if not trade_log.empty else np.nan, "max_drawdown": float(max_dd), "avg_entry_minute": float(trade_log["entry_minute"].mean()) if not trade_log.empty else np.nan}
    return trade_log, summary


def run_all(df: pd.DataFrame, fee: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    strategies = ["latest_m1_drop20_down", "latest_m2_sharpdrop_reversal_up", "latest_m2_milddrop_down", "latest_m2_neutral_down", "latest_m2_extremeup_fade_down", "latest_m4_rise30to50_up", "latest_switch_v1", "latest_switch_v2"]
    logs, summaries = [], []
    for s in strategies:
        for frac in FIXED_FRACS:
            lg, sm = simulate(df, s, frac, fee)
            logs.append(lg); summaries.append(sm)
    logs_df = pd.concat([x for x in logs if not x.empty], ignore_index=True) if logs else pd.DataFrame()
    summary_df = pd.DataFrame(summaries).sort_values("ending_bankroll", ascending=False).reset_index(drop=True)
    return logs_df, summary_df


def make_plots(summary: pd.DataFrame, logs: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not summary.empty:
        top = summary.head(20)
        labels = top["strategy"] + "|" + top["sizing"]
        plt.figure(figsize=(13, 5))
        plt.bar(labels, top["ending_bankroll"])
        plt.xticks(rotation=65, ha="right")
        plt.title("最新 regime 策略 Top 期末本金")
        plt.tight_layout()
        p = fig_dir / "latest_regime_top_endings.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
        best = summary.iloc[0]
        s = logs[(logs["strategy"] == best["strategy"]) & (logs["sizing"] == best["sizing"])]
        if not s.empty:
            s = s.sort_values("first_quote_ts")
            plt.figure(figsize=(10, 4))
            plt.plot(s["first_quote_ts"], s["bankroll_after"])
            plt.title(f"最佳最新 regime 策略本金曲线: {best['strategy']} | {best['sizing']}")
            plt.tight_layout()
            p = fig_dir / "latest_regime_best_curve.png"
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))
    return paths


def build_report(summary: pd.DataFrame) -> str:
    lines = []
    lines.append("# 最新数据集上的新 regime 策略探索")
    lines.append("")
    lines.append("- `latest_m1_drop20_down`：第1分钟跌超20，买Down")
    lines.append("- `latest_m2_sharpdrop_reversal_up`：第2分钟跌在(-50,-30]，买Up")
    lines.append("- `latest_m2_milddrop_down`：第2分钟跌在(-30,-10]，买Down")
    lines.append("- `latest_m2_neutral_down`：第2分钟在(-10,10]，买Down")
    lines.append("- `latest_m2_extremeup_fade_down`：第2分钟涨超50，买Down")
    lines.append("- `latest_m4_rise30to50_up`：第4分钟涨在(30,50]，买Up")
    lines.append("- `latest_switch_v1 / v2`：分钟感知组合策略")
    lines.append("")
    lines.append("## 回测结果")
    lines.append("")
    lines.append(summary.head(40).to_markdown(index=False))
    lines.append("")
    if not summary.empty:
        best = summary.iloc[0]
        lines.append(f"当前最佳：**{best['strategy']} | {best['sizing']}**，期末本金 **{best['ending_bankroll']:.2f} USD**，最大回撤 **{best['max_drawdown']:.2%}**。")
        lines.append("")
    lines.append("![最新 regime 策略 Top 期末本金](latest_regime_figures/latest_regime_top_endings.png)")
    lines.append("")
    lines.append("![最佳最新 regime 策略本金曲线](latest_regime_figures/latest_regime_best_curve.png)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    raw = read_source_files(Path(args.source_dir))
    quotes = prepare_quotes(raw)
    wide = build_wide(quotes)

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "latest_regime_figures"
    report_dir.mkdir(parents=True, exist_ok=True)
    logs, summary = run_all(wide, fee=args.fee)
    figs = make_plots(summary, logs, fig_dir)
    logs.to_csv(report_dir / "latest_regime_trade_logs.csv", index=False)
    summary.to_csv(report_dir / "latest_regime_summary.csv", index=False)
    (report_dir / "latest_regime_report.md").write_text(build_report(summary), encoding="utf-8")
    (report_dir / "latest_regime_summary.json").write_text(json.dumps({"rows_summary": int(len(summary)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_summary": int(len(summary)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

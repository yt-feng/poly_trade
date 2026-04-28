from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

FILE_GLOB = "*btc-updown-5m_quotes.csv"
ENTRY_MINUTES = [1, 2, 4]
FIXED_FRACS = [0.10, 0.15, 0.20, 0.25]


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
    for c in [
        "buy_up_cents","buy_down_cents","sell_up_cents","sell_down_cents",
        "buy_up_size","buy_down_size","sell_up_size","sell_down_size",
        "target_price","final_price"
    ]:
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
        buy_up = pd.to_numeric(row.get("buy_up_cents"), errors="coerce")
        buy_down = pd.to_numeric(row.get("buy_down_cents"), errors="coerce")
        sell_up = pd.to_numeric(row.get("sell_up_cents"), errors="coerce")
        sell_down = pd.to_numeric(row.get("sell_down_cents"), errors="coerce")
        up_price = np.nan if pd.isna(buy_up) else float(buy_up) / 100.0
        down_price = np.nan if pd.isna(buy_down) else float(buy_down) / 100.0
        up_mid = np.nan if pd.isna(buy_up) or pd.isna(sell_up) else float((buy_up + sell_up) / 2.0 / 100.0)
        down_mid = np.nan if pd.isna(buy_down) or pd.isna(sell_down) else float((buy_down + sell_down) / 2.0 / 100.0)
        spread_up = np.nan if pd.isna(buy_up) or pd.isna(sell_up) else float((sell_up - buy_up) / 100.0)
        spread_down = np.nan if pd.isna(buy_down) or pd.isna(sell_down) else float((sell_down - buy_down) / 100.0)
        buy_up_size = pd.to_numeric(row.get("buy_up_size"), errors="coerce")
        buy_down_size = pd.to_numeric(row.get("buy_down_size"), errors="coerce")
        size_imb = np.nan
        if pd.notna(buy_up_size) and pd.notna(buy_down_size) and (buy_up_size + buy_down_size) > 0:
            size_imb = float((buy_up_size - buy_down_size) / (buy_up_size + buy_down_size))
        rows.append({
            "slug": slug,
            "first_quote_ts": first_ts,
            "outcome_up": float(final > target),
            f"move_m{minute}": move,
            f"buy_up_price_m{minute}": up_price,
            f"buy_down_price_m{minute}": down_price,
            f"mid_up_price_m{minute}": up_mid,
            f"mid_down_price_m{minute}": down_mid,
            f"spread_up_m{minute}": spread_up,
            f"spread_down_m{minute}": spread_down,
            f"buy_up_size_m{minute}": buy_up_size,
            f"buy_down_size_m{minute}": buy_down_size,
            f"size_imb_m{minute}": size_imb,
        })
    return pd.DataFrame(rows)


def build_wide(quotes: pd.DataFrame) -> pd.DataFrame:
    out = None
    for m in ENTRY_MINUTES:
        snap = build_snapshot(quotes, m)
        out = snap if out is None else out.merge(snap, on=["slug","first_quote_ts","outcome_up"], how="outer")
    return out.sort_values("first_quote_ts").reset_index(drop=True)


def add_buckets(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["move_bucket_m1"] = pd.cut(out["move_m1"], bins=[-1e9,-50,-20,-10,10,1e9], labels=["<=-50","-50~-20","-20~-10","-10~10",">=10"], include_lowest=True)
    out["move_bucket_m2"] = pd.cut(out["move_m2"], bins=[-1e9,-50,-30,-10,10,50,1e9], labels=["<=-50","-50~-30","-30~-10","-10~10","10~50",">=50"], include_lowest=True)
    out["move_bucket_m4"] = pd.cut(out["move_m4"], bins=[-1e9,-10,10,30,50,1e9], labels=["<=-10","-10~10","10~30","30~50",">=50"], include_lowest=True)
    out["up_price_bucket_m2"] = pd.cut(out["buy_up_price_m2"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["down_price_bucket_m2"] = pd.cut(out["buy_down_price_m2"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["up_price_bucket_m4"] = pd.cut(out["buy_up_price_m4"], bins=[0,0.2,0.4,0.6,0.8,1.0], labels=["0.0~0.2","0.2~0.4","0.4~0.6","0.6~0.8","0.8~1.0"], include_lowest=True)
    out["imb_sign_m2"] = np.where(out["size_imb_m2"] <= -0.1, "neg", np.where(out["size_imb_m2"] >= 0.1, "pos", "neu"))
    out["liq_sign_down_m2"] = np.where(out["buy_down_size_m2"] >= 350, "high", np.where(out["buy_down_size_m2"] >= 150, "mid", "low"))
    out["liq_sign_up_m2"] = np.where(out["buy_up_size_m2"] >= 350, "high", np.where(out["buy_up_size_m2"] >= 150, "mid", "low"))
    out["spread_sign_m2"] = np.where(out["spread_up_m2"].fillna(1) <= 0.04, "tight", "wide")
    return out


def regime_for_row(row: pd.Series) -> str:
    m1, m2, m4 = row.get("move_m1", np.nan), row.get("move_m2", np.nan), row.get("move_m4", np.nan)
    if pd.notna(m1) and m1 <= -20:
        return "early_drop"
    if pd.notna(m2) and -50 < m2 <= -30:
        return "sharp_drop_reversal"
    if pd.notna(m2) and -30 < m2 <= -10:
        return "mild_drop"
    if pd.notna(m2) and m2 >= 50:
        return "extreme_up"
    if pd.notna(m4) and 30 < m4 <= 50:
        return "late_breakout"
    return "neutral"


def exp_weighted_prob(values: np.ndarray, halflife: float = 20.0) -> float:
    if len(values) == 0:
        return np.nan
    idx = np.arange(len(values))
    weights = 0.5 ** ((len(values) - 1 - idx) / halflife)
    return float(np.dot(values, weights) / weights.sum())


def state_prob(hist: pd.DataFrame, row: pd.Series, side: str) -> Tuple[float, int, str]:
    if side == "buy_up":
        outcome = hist["outcome_up"].astype(float).to_numpy()
        subsets = [
            hist[(hist["regime"] == row["regime"]) & (hist["move_bucket_m2"] == row["move_bucket_m2"]) & (hist["up_price_bucket_m2"] == row["up_price_bucket_m2"])],
            hist[(hist["regime"] == row["regime"]) & (hist["move_bucket_m4"] == row["move_bucket_m4"]) & (hist["up_price_bucket_m4"] == row["up_price_bucket_m4"])],
            hist[(hist["regime"] == row["regime"]) & (hist["imb_sign_m2"] == row["imb_sign_m2"])],
            hist[(hist["regime"] == row["regime"])],
            hist,
        ]
        bucket_name = "up"
    else:
        outcome = (1.0 - hist["outcome_up"].astype(float)).to_numpy()
        subsets = [
            hist[(hist["regime"] == row["regime"]) & (hist["move_bucket_m2"] == row["move_bucket_m2"]) & (hist["down_price_bucket_m2"] == row["down_price_bucket_m2"])],
            hist[(hist["regime"] == row["regime"]) & (hist["imb_sign_m2"] == row["imb_sign_m2"]) & (hist["liq_sign_down_m2"] == row["liq_sign_down_m2"])],
            hist[(hist["regime"] == row["regime"])],
            hist,
        ]
        bucket_name = "down"
    for idx, sub in enumerate(subsets):
        if len(sub) >= 10:
            vals = (sub["outcome_up"].astype(float).to_numpy() if side == "buy_up" else 1.0 - sub["outcome_up"].astype(float).to_numpy())
            return exp_weighted_prob(vals), int(len(sub)), f"{bucket_name}_level_{idx+1}"
    return np.nan, 0, f"{bucket_name}_none"


def candidate_strategies(row: pd.Series) -> List[Tuple[str, str, int, float]]:
    cands: List[Tuple[str, str, int, float]] = []
    m1, m2, m4 = row.get("move_m1", np.nan), row.get("move_m2", np.nan), row.get("move_m4", np.nan)
    if pd.notna(m1) and m1 <= -20 and pd.notna(row.get("buy_down_price_m1")):
        cands.append(("early_drop_continuation", "buy_down", 1, float(row["buy_down_price_m1"])))
    if pd.notna(m2) and -50 < m2 <= -30 and pd.notna(row.get("buy_up_price_m2")):
        cands.append(("sharp_drop_reversal", "buy_up", 2, float(row["buy_up_price_m2"])))
    if pd.notna(m2) and -30 < m2 <= -10 and pd.notna(row.get("buy_down_price_m2")):
        cands.append(("mild_drop_continuation", "buy_down", 2, float(row["buy_down_price_m2"])))
    if pd.notna(m2) and -10 < m2 <= 10 and pd.notna(row.get("buy_down_price_m2")):
        cands.append(("neutral_down_value", "buy_down", 2, float(row["buy_down_price_m2"])))
    if pd.notna(m2) and m2 >= 50 and pd.notna(row.get("buy_down_price_m2")):
        cands.append(("extreme_up_fade", "buy_down", 2, float(row["buy_down_price_m2"])))
    if pd.notna(m4) and 30 < m4 <= 50 and pd.notna(row.get("buy_up_price_m4")):
        cands.append(("late_breakout_up", "buy_up", 4, float(row["buy_up_price_m4"])))
    return cands


def choose_best_trade(hist: pd.DataFrame, row: pd.Series, fee: float) -> Dict[str, object]:
    best = {"strategy": "skip", "side": "skip", "entry_minute": -1, "market_price": np.nan, "fair_prob": np.nan, "edge": np.nan, "support": 0, "bucket": "none"}
    best_edge = -1e9
    spread_penalty = float(max(0.0, row.get("spread_up_m2", np.nan) if pd.notna(row.get("spread_up_m2", np.nan)) else 0.0))
    base_margin = fee + 0.02 + min(spread_penalty, 0.05)
    for strategy, side, minute, market_price in candidate_strategies(row):
        fair_prob, support, bucket = state_prob(hist, row, side)
        if pd.isna(fair_prob):
            continue
        edge = float(fair_prob - market_price)
        # regime-specific safety margins
        margin = base_margin
        if strategy in {"sharp_drop_reversal", "extreme_up_fade"}:
            margin += 0.01
        if support < 15:
            margin += 0.01
        if strategy == "late_breakout_up" and row.get("buy_up_price_m4", 1.0) > 0.90:
            margin += 0.03
        if edge <= margin:
            continue
        score = edge - 0.005 * max(0, 20 - support)
        if score > best_edge:
            best_edge = score
            best = {"strategy": strategy, "side": side, "entry_minute": minute, "market_price": market_price, "fair_prob": fair_prob, "edge": edge, "support": support, "bucket": bucket}
    return best


def simulate_selected(df: pd.DataFrame, fee: float, frac: float) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll, peak, max_dd = 100.0, 100.0, 0.0
    rows = []
    for i in range(len(df)):
        row = df.iloc[i]
        hist = df.iloc[:i].copy()
        if len(hist) < 40:
            continue
        choice = choose_best_trade(hist, row, fee)
        if choice["side"] == "skip":
            continue
        minute = int(choice["entry_minute"])
        side = str(choice["side"])
        price = row[f"buy_up_price_m{minute}"] if side == "buy_up" else row[f"buy_down_price_m{minute}"]
        size = row[f"buy_up_size_m{minute}"] if side == "buy_up" else row[f"buy_down_size_m{minute}"]
        pnl_per_share = row["outcome_up"] - price - fee if side == "buy_up" else (1.0 - row["outcome_up"]) - price - fee
        if pd.isna(price) or pd.isna(size) or price <= 0:
            continue
        target_cost = min(bankroll * frac, bankroll, float(size) * float(price))
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        rows.append({
            "strategy": "selected_fairprob_switch",
            "sizing": f"fixed_{int(frac*100)}pct",
            "first_quote_ts": row["first_quote_ts"],
            "slug": row["slug"],
            "entry_minute": minute,
            "regime": row["regime"],
            "micro_strategy": choice["strategy"],
            "side": side,
            "market_price": choice["market_price"],
            "fair_prob": choice["fair_prob"],
            "edge": choice["edge"],
            "support": choice["support"],
            "bucket": choice["bucket"],
            "target_cost": target_cost,
            "shares": shares,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
        })
    trade_log = pd.DataFrame(rows)
    summary = {
        "strategy": "selected_fairprob_switch",
        "sizing": f"fixed_{int(frac*100)}pct",
        "trades": int(len(trade_log)),
        "ending_bankroll": float(trade_log["bankroll_after"].iloc[-1]) if not trade_log.empty else 100.0,
        "total_return": float(trade_log["bankroll_after"].iloc[-1] / 100.0 - 1.0) if not trade_log.empty else 0.0,
        "avg_trade_return_on_cost": float((trade_log["pnl_usd"] / trade_log["target_cost"]).mean()) if not trade_log.empty else np.nan,
        "max_drawdown": float(max_dd),
        "avg_entry_minute": float(trade_log["entry_minute"].mean()) if not trade_log.empty else np.nan,
        "avg_edge": float(trade_log["edge"].mean()) if not trade_log.empty else np.nan,
    }
    return trade_log, summary


def trade_type_breakdown(logs: pd.DataFrame) -> pd.DataFrame:
    if logs.empty:
        return pd.DataFrame()
    out = logs.groupby(["micro_strategy", "side", "entry_minute"], as_index=False).agg(
        trades=("slug", "size"),
        avg_edge=("edge", "mean"),
        avg_pnl_usd=("pnl_usd", "mean"),
        total_pnl_usd=("pnl_usd", "sum"),
    )
    return out.sort_values("total_pnl_usd", ascending=False).reset_index(drop=True)


def make_plots(summary: pd.DataFrame, logs: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not summary.empty:
        plt.figure(figsize=(8, 4))
        plt.bar(summary["sizing"], summary["ending_bankroll"])
        plt.title("精选策略不同仓位的期末本金")
        plt.tight_layout()
        p = fig_dir / "selected_strategy_bankroll_by_sizing.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
    if not logs.empty and not summary.empty:
        best = summary.sort_values("ending_bankroll", ascending=False).iloc[0]
        s = logs[logs["sizing"] == best["sizing"]].sort_values("first_quote_ts")
        plt.figure(figsize=(10, 4))
        plt.plot(s["first_quote_ts"], s["bankroll_after"])
        plt.title(f"精选策略本金曲线: {best['sizing']}")
        plt.tight_layout()
        p = fig_dir / "selected_strategy_best_curve.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
    return paths


def markdown_table(df: pd.DataFrame, rows: int = 30) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    num_cols = show.select_dtypes(include=[np.number]).columns
    show[num_cols] = show[num_cols].round(4)
    return show.to_markdown(index=False)


def build_report(summary: pd.DataFrame, breakdown: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# 最新整天数据：精选策略与 fair probability / regime gate 回测")
    lines.append("")
    lines.append("这份报告专门针对最新整天的 5 分钟 BTC 事件做策略精选，并尽量贴近你 comment 里的思路：")
    lines.append("")
    lines.append("- 不直接做硬方向预测，而是先估计 fair probability")
    lines.append("- 再跟市场买价比较，只在 edge 足够大时交易")
    lines.append("- 用 regime + 微观结构近似（分钟、价格、size、spread、流动性）做 gating")
    lines.append("")
    lines.append("## 这版补了什么")
    lines.append("")
    lines.append("- `selected_fairprob_switch`：用 walk-forward 历史条件概率估计 fair p")
    lines.append("- 候选微策略包括：early_drop_continuation / sharp_drop_reversal / mild_drop_continuation / neutral_down_value / extreme_up_fade / late_breakout_up")
    lines.append("- 只有当 `fair_prob - market_price > fee + safety_margin + spread_penalty` 时才开仓")
    lines.append("- safety margin 对 high-vol / low-support / expensive breakout 做了额外加严")
    lines.append("")
    lines.append("## 仓位结果")
    lines.append("")
    lines.append(markdown_table(summary, rows=20))
    lines.append("")
    lines.append("## 微策略拆分")
    lines.append("")
    lines.append(markdown_table(breakdown, rows=30))
    lines.append("")
    if not summary.empty:
        best = summary.sort_values("ending_bankroll", ascending=False).iloc[0]
        lines.append("## 当前精选结果")
        lines.append("")
        lines.append(f"- 策略：**selected_fairprob_switch**")
        lines.append(f"- 仓位：**{best['sizing']}**")
        lines.append(f"- 交易笔数：**{int(best['trades'])}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 总收益率：**{best['total_return']:.2%}**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append(f"- 平均入场分钟：**{best['avg_entry_minute']:.2f}**")
        lines.append(f"- 平均 edge：**{best['avg_edge']:.4f}**")
        lines.append("")
    lines.append("## 查缺补漏说明")
    lines.append("")
    lines.append("这版已经把你 comment 里能用现有数据实现的部分尽量补了：")
    lines.append("")
    lines.append("- fair probability > market price 的价值交易视角")
    lines.append("- regime gate")
    lines.append("- 微观结构近似（盘口 size、spread、流动性、分钟路径）")
    lines.append("")
    lines.append("但以下缺口仍然存在，当前 repo 里还没有原始数据，所以只能在报告里保留为下一步：")
    lines.append("")
    lines.append("- Chainlink 对齐标签 / Chainlink bid-ask / volatility")
    lines.append("- Polymarket 自己的更细 orderbook / trade stream")
    lines.append("- Binance perp OI / taker flow / liquidation")
    lines.append("- Deribit IV / basis / OI")
    lines.append("- 宏观事件日历")
    lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("![精选策略不同仓位的期末本金](selected_strategy_figures/selected_strategy_bankroll_by_sizing.png)")
    lines.append("")
    lines.append("![精选策略本金曲线](selected_strategy_figures/selected_strategy_best_curve.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    raw = read_source_files(Path(args.source_dir))
    quotes = prepare_quotes(raw)
    wide = add_buckets(build_wide(quotes))
    wide["regime"] = wide.apply(regime_for_row, axis=1)

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "selected_strategy_figures"
    report_dir.mkdir(parents=True, exist_ok=True)

    logs_list, sums = [], []
    for frac in FIXED_FRACS:
        lg, sm = simulate_selected(wide, fee=args.fee, frac=frac)
        logs_list.append(lg); sums.append(sm)
    logs = pd.concat([x for x in logs_list if not x.empty], ignore_index=True) if logs_list else pd.DataFrame()
    summary = pd.DataFrame(sums).sort_values("ending_bankroll", ascending=False).reset_index(drop=True)
    breakdown = trade_type_breakdown(logs)
    figs = make_plots(summary, logs, fig_dir)

    logs.to_csv(report_dir / "selected_strategy_trade_logs.csv", index=False)
    summary.to_csv(report_dir / "selected_strategy_summary.csv", index=False)
    breakdown.to_csv(report_dir / "selected_strategy_breakdown.csv", index=False)
    (report_dir / "selected_strategy_report.md").write_text(build_report(summary, breakdown), encoding="utf-8")
    (report_dir / "selected_strategy_summary.json").write_text(json.dumps({"rows_summary": int(len(summary)), "rows_breakdown": int(len(breakdown)), "figure_count": len(figs)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows_summary": int(len(summary)), "rows_breakdown": int(len(breakdown)), "figure_count": len(figs)})


if __name__ == "__main__":
    main()

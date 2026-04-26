from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

MOVE_BINS = [-10000, -100, -50, -30, -10, 10, 30, 50, 100, 10000]
MOVE_LABELS = ["<=-100", "-100~-50", "-50~-30", "-30~-10", "-10~10", "10~30", "30~50", "50~100", ">=100"]
PRICE_BINS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
PRICE_LABELS = ["0.0~0.2", "0.2~0.4", "0.4~0.6", "0.6~0.8", "0.8~1.0"]
SIZE_IMB_BINS = [-10.0, -0.2, 0.2, 10.0]
SIZE_IMB_LABELS = ["neg", "neutral", "pos"]
LIQ_BINS = [-1, 150, 350, 1000000]
LIQ_LABELS = ["low", "mid", "high"]

SIZING_CONFIGS = {
    "fixed_10pct": ("fixed", 0.10),
    "fixed_20pct": ("fixed", 0.20),
    "full_kelly": ("kelly", 1.0),
    "half_kelly": ("kelly", 0.5),
    "quarter_kelly": ("kelly", 0.25),
    "half_kelly_capped20": ("kelly_capped", 0.5),
    "quarter_kelly_capped20": ("kelly_capped", 0.25),
}


def load_features(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["first_quote_ts"] = pd.to_datetime(df["first_quote_ts"], utc=True, errors="coerce")
    df = df.sort_values("first_quote_ts").reset_index(drop=True)
    return df


def add_buckets(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["move_bucket"] = pd.cut(out["btc_move_2m"], bins=MOVE_BINS, labels=MOVE_LABELS, include_lowest=True)
    out["up_price_bucket"] = pd.cut(out["buy_up_price_2m"], bins=PRICE_BINS, labels=PRICE_LABELS, include_lowest=True)
    out["down_price_bucket"] = pd.cut(out["buy_down_price_2m"], bins=PRICE_BINS, labels=PRICE_LABELS, include_lowest=True)
    out["size_imb_bucket"] = pd.cut(out["size_imbalance_updown_2m"], bins=SIZE_IMB_BINS, labels=SIZE_IMB_LABELS, include_lowest=True)
    out["down_liq_bucket"] = pd.cut(out["buy_down_size_2m"], bins=LIQ_BINS, labels=LIQ_LABELS, include_lowest=True)
    out["up_liq_bucket"] = pd.cut(out["buy_up_size_2m"], bins=LIQ_BINS, labels=LIQ_LABELS, include_lowest=True)
    return out


def pnl_per_share(row: pd.Series, side: str, fee: float) -> float:
    if side == "buy_up":
        if pd.isna(row["buy_up_price_2m"]) or pd.isna(row["outcome_up"]):
            return np.nan
        return float(row["outcome_up"] - row["buy_up_price_2m"] - fee)
    if side == "buy_down":
        if pd.isna(row["buy_down_price_2m"]) or pd.isna(row["outcome_up"]):
            return np.nan
        return float((1.0 - row["outcome_up"]) - row["buy_down_price_2m"] - fee)
    return np.nan


def kelly_fraction(prob: float, price: float) -> float:
    if pd.isna(prob) or pd.isna(price) or price <= 0 or price >= 1:
        return 0.0
    denom = 1.0 - price
    if denom <= 0:
        return 0.0
    return float(max((prob - price) / denom, 0.0))


def estimate_conditional_stats(hist: pd.DataFrame, row: pd.Series, side: str, fee: float) -> Dict[str, float]:
    if side == "buy_down":
        price_col = "buy_down_price_2m"
        price_bucket_col = "down_price_bucket"
        liq_bucket_col = "down_liq_bucket"
        q_series = 1.0 - hist["outcome_up"]
    else:
        price_col = "buy_up_price_2m"
        price_bucket_col = "up_price_bucket"
        liq_bucket_col = "up_liq_bucket"
        q_series = hist["outcome_up"]

    fallbacks = [
        (hist["move_bucket"] == row["move_bucket"]) & (hist[price_bucket_col] == row[price_bucket_col]) & (hist["size_imb_bucket"] == row["size_imb_bucket"]),
        (hist["move_bucket"] == row["move_bucket"]) & (hist[price_bucket_col] == row[price_bucket_col]),
        (hist["move_bucket"] == row["move_bucket"]) & (hist[liq_bucket_col] == row[liq_bucket_col]),
        (hist["move_bucket"] == row["move_bucket"]),
        hist[price_bucket_col] == row[price_bucket_col],
        pd.Series([True] * len(hist), index=hist.index),
    ]

    for mask in fallbacks:
        subset = hist[mask & hist["outcome_up"].notna()].copy()
        if len(subset) >= 8:
            q = float(q_series.loc[subset.index].mean())
            avg_pnl = float(subset.apply(lambda r: pnl_per_share(r, side, fee), axis=1).mean())
            return {"support": int(len(subset)), "q_hat": q, "avg_pnl": avg_pnl}

    return {"support": 0, "q_hat": np.nan, "avg_pnl": np.nan}


def base_rule_candidates(row: pd.Series) -> List[Tuple[str, str]]:
    cands: List[Tuple[str, str]] = []
    move = row["btc_move_2m"]
    imb = row.get("size_imbalance_updown_2m", np.nan)
    down_p = row.get("buy_down_price_2m", np.nan)
    up_p = row.get("buy_up_price_2m", np.nan)
    down_sz = row.get("buy_down_size_2m", np.nan)
    up_sz = row.get("buy_up_size_2m", np.nan)

    if pd.notna(move) and move <= -10:
        cands.append(("rule_drop10_down", "buy_down"))
        if pd.notna(imb) and imb <= 0:
            cands.append(("rule_drop10_down_size_neg", "buy_down"))
        if pd.notna(imb) and imb <= -0.2:
            cands.append(("rule_drop10_down_size_strong_neg", "buy_down"))
        if pd.notna(down_p) and down_p <= 0.8:
            cands.append(("rule_drop10_down_price_cap80", "buy_down"))
        if pd.notna(down_sz) and down_sz >= 250:
            cands.append(("rule_drop10_down_good_liq", "buy_down"))
        if pd.notna(down_p) and down_p <= 0.8 and pd.notna(imb) and imb <= 0:
            cands.append(("rule_drop10_down_combo", "buy_down"))
    if pd.notna(move) and move <= -20:
        cands.append(("rule_drop20_down", "buy_down"))
    if pd.notna(move) and move <= -30:
        cands.append(("rule_drop30_down", "buy_down"))
    if pd.notna(move) and 30 < move <= 50:
        cands.append(("rule_rise30to50_up", "buy_up"))
        if pd.notna(imb) and imb >= 0:
            cands.append(("rule_rise30to50_up_size_pos", "buy_up"))
        if pd.notna(up_p) and up_p <= 0.8:
            cands.append(("rule_rise30to50_up_price_cap80", "buy_up"))
        if pd.notna(up_sz) and up_sz >= 250:
            cands.append(("rule_rise30to50_up_good_liq", "buy_up"))
    if pd.notna(move) and -10 < move <= 10:
        if pd.notna(up_p) and 0.4 <= up_p <= 0.6:
            cands.append(("rule_neutral_midprice_up", "buy_up"))
    return cands


def choose_best_candidate(hist: pd.DataFrame, row: pd.Series, fee: float) -> Dict[str, object]:
    candidates = base_rule_candidates(row)
    best = {"strategy": "skip", "side": "skip", "q_hat": np.nan, "support": 0, "avg_pnl": np.nan}
    best_score = -1e18
    for name, side in candidates:
        stats = estimate_conditional_stats(hist, row, side, fee)
        if stats["support"] < 8 or pd.isna(stats["avg_pnl"]) or stats["avg_pnl"] <= 0:
            continue
        score = float(stats["avg_pnl"])
        if score > best_score:
            best_score = score
            best = {"strategy": name, "side": side, **stats}
    return best


def state_policy_choice(hist: pd.DataFrame, row: pd.Series, fee: float) -> Dict[str, object]:
    choices = []
    for side in ["buy_up", "buy_down"]:
        stats = estimate_conditional_stats(hist, row, side, fee)
        if stats["support"] >= 8 and pd.notna(stats["avg_pnl"]):
            choices.append((side, stats))
    if not choices:
        return {"strategy": "state_policy", "side": "skip", "q_hat": np.nan, "support": 0, "avg_pnl": np.nan}
    side, stats = max(choices, key=lambda x: x[1]["avg_pnl"])
    if stats["avg_pnl"] <= 0:
        return {"strategy": "state_policy", "side": "skip", "q_hat": np.nan, "support": stats["support"], "avg_pnl": stats["avg_pnl"]}
    return {"strategy": "state_policy", "side": side, **stats}


def build_walkforward_signals(features: pd.DataFrame, fee: float, min_history: int = 60) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna()].copy().sort_values("first_quote_ts").reset_index(drop=True)
    rows = []
    for i in range(len(usable)):
        row = usable.iloc[i].copy()
        rec = row.to_dict()
        rec.update({
            "opt_strategy": "skip", "opt_side": "skip", "opt_q_hat": np.nan, "opt_support": 0, "opt_avg_pnl": np.nan,
            "state_strategy": "state_policy", "state_side": "skip", "state_q_hat": np.nan, "state_support": 0, "state_avg_pnl": np.nan,
        })
        if i >= min_history:
            hist = usable.iloc[:i].copy()
            best = choose_best_candidate(hist, row, fee)
            rec.update({
                "opt_strategy": best["strategy"], "opt_side": best["side"], "opt_q_hat": best["q_hat"], "opt_support": best["support"], "opt_avg_pnl": best["avg_pnl"],
            })
            state = state_policy_choice(hist, row, fee)
            rec.update({
                "state_strategy": state["strategy"], "state_side": state["side"], "state_q_hat": state["q_hat"], "state_support": state["support"], "state_avg_pnl": state["avg_pnl"],
            })
        rows.append(rec)
    return pd.DataFrame(rows)


def simulate(df: pd.DataFrame, strategy_label_col: str, side_col: str, q_col: str, fee: float, starting_bankroll: float = 100.0) -> Tuple[pd.DataFrame, pd.DataFrame]:
    ordered = df.copy().sort_values("first_quote_ts").reset_index(drop=True)
    all_logs = []
    summaries = []
    for sizing_name, (mode, val) in SIZING_CONFIGS.items():
        bankroll = starting_bankroll
        peak = starting_bankroll
        max_dd = 0.0
        trade_rows = []
        for _, row in ordered.iterrows():
            side = row[side_col]
            if side not in {"buy_up", "buy_down"}:
                continue
            entry_price = row["buy_up_price_2m"] if side == "buy_up" else row["buy_down_price_2m"]
            size_available = row["buy_up_size_2m"] if side == "buy_up" else row["buy_down_size_2m"]
            q = row[q_col]
            if pd.isna(entry_price) or pd.isna(size_available) or pd.isna(q) or entry_price <= 0:
                continue
            if mode == "fixed":
                target_cost = bankroll * float(val)
            elif mode == "kelly":
                target_cost = bankroll * kelly_fraction(float(q), float(entry_price)) * float(val)
            else:
                target_cost = bankroll * min(kelly_fraction(float(q), float(entry_price)) * float(val), 0.20)
            target_cost = max(0.0, min(target_cost, bankroll, float(size_available) * float(entry_price)))
            if target_cost <= 0:
                continue
            shares = target_cost / entry_price
            payout = shares * (1.0 if ((side == "buy_up" and row["outcome_up"] == 1.0) or (side == "buy_down" and row["outcome_up"] == 0.0)) else 0.0)
            pnl = payout - target_cost - shares * fee
            bankroll += pnl
            peak = max(peak, bankroll)
            max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
            trade_rows.append({
                "strategy_group": strategy_label_col,
                "strategy_name": row[strategy_label_col],
                "sizing": sizing_name,
                "first_quote_ts": row["first_quote_ts"],
                "slug": row["slug"],
                "side": side,
                "q_hat": q,
                "support": row.get(q_col.replace("q_hat", "support"), np.nan),
                "entry_price": entry_price,
                "size_available": size_available,
                "target_cost": target_cost,
                "shares": shares,
                "pnl_usd": pnl,
                "bankroll_after": bankroll,
            })
        trade_log = pd.DataFrame(trade_rows)
        if trade_log.empty:
            summaries.append({"strategy_group": strategy_label_col, "strategy_name": "(none)", "sizing": sizing_name, "trades": 0, "ending_bankroll": starting_bankroll, "total_return": 0.0, "max_drawdown": 0.0})
        else:
            grouped = trade_log.groupby("strategy_name")
            for strategy_name, g in grouped:
                summaries.append({
                    "strategy_group": strategy_label_col,
                    "strategy_name": strategy_name,
                    "sizing": sizing_name,
                    "trades": int(len(g)),
                    "ending_bankroll": float(g["bankroll_after"].iloc[-1]),
                    "total_return": float(g["bankroll_after"].iloc[-1] / starting_bankroll - 1.0),
                    "avg_trade_return_on_cost": float((g["pnl_usd"] / g["target_cost"]).mean()),
                    "max_drawdown": float(max_dd),
                })
            all_logs.append(trade_log)
    logs = pd.concat(all_logs, ignore_index=True) if all_logs else pd.DataFrame()
    summary = pd.DataFrame(summaries)
    if not summary.empty:
        summary = summary.sort_values("ending_bankroll", ascending=False).reset_index(drop=True)
    return logs, summary


def build_report(summary: pd.DataFrame, top_rules: pd.DataFrame, fig_dir_name: str) -> str:
    lines: List[str] = []
    lines.append("# 优化策略回测补充报告")
    lines.append("")
    lines.append("## 这版在优化什么")
    lines.append("")
    lines.append("当前最强基线是 `rule_drop10_down + fixed_20pct`。这版补做两类优化：")
    lines.append("")
    lines.append("- 把 size/depth 真正变成开仓过滤器")
    lines.append("- 用 walk-forward 历史条件概率来替代之前 Kelly 里过于保守的固定概率")
    lines.append("")
    lines.append("## 候选策略-仓位结果")
    lines.append("")
    lines.append(top_rules.to_markdown(index=False))
    lines.append("")
    if not summary.empty:
        best = summary.iloc[0]
        lines.append("## 当前最佳补充策略")
        lines.append("")
        lines.append(f"- 策略组：**{best['strategy_group']}**")
        lines.append(f"- 策略名：**{best['strategy_name']}**")
        lines.append(f"- 仓位：**{best['sizing']}**")
        lines.append(f"- 交易笔数：**{int(best['trades'])}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 总收益率：**{best['total_return']:.2%}**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    for title, rel in [
        ("优化策略Top期末本金", f"{fig_dir_name}/optimizer_top_endings.png"),
        ("最佳优化策略本金曲线", f"{fig_dir_name}/optimizer_best_curve.png"),
    ]:
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"![{title}]({rel})")
        lines.append("")
    return "\n".join(lines)


def make_plots(summary: pd.DataFrame, logs: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)
    if not summary.empty:
        top = summary.head(15)
        labels = top["strategy_name"] + "|" + top["sizing"]
        plt.figure(figsize=(12,5))
        plt.bar(labels, top["ending_bankroll"])
        plt.xticks(rotation=60, ha="right")
        plt.title("优化策略Top期末本金")
        plt.tight_layout()
        p = fig_dir / "optimizer_top_endings.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
    if not logs.empty and not summary.empty:
        best = summary.iloc[0]
        s = logs[(logs["strategy_name"] == best["strategy_name"]) & (logs["sizing"] == best["sizing"])].copy().sort_values("first_quote_ts")
        if not s.empty:
            plt.figure(figsize=(10,4))
            plt.plot(s["first_quote_ts"], s["bankroll_after"])
            plt.title(f"最佳优化策略本金曲线: {best['strategy_name']} | {best['sizing']}")
            plt.tight_layout()
            p = fig_dir / "optimizer_best_curve.png"
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-file", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    features = add_buckets(load_features(Path(args.features_file)))
    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "optimization_figures"
    report_dir.mkdir(parents=True, exist_ok=True)

    wf = build_walkforward_signals(features, fee=args.fee, min_history=60)
    opt_logs, opt_summary = simulate(wf, "opt_strategy", "opt_side", "opt_q_hat", fee=args.fee)
    state_logs, state_summary = simulate(wf, "state_strategy", "state_side", "state_q_hat", fee=args.fee)

    summary = pd.concat([opt_summary, state_summary], ignore_index=True) if not opt_summary.empty or not state_summary.empty else pd.DataFrame()
    if not summary.empty:
        summary = summary[summary["strategy_name"] != "(none)"].sort_values("ending_bankroll", ascending=False).reset_index(drop=True)
    logs = pd.concat([opt_logs, state_logs], ignore_index=True) if not opt_logs.empty or not state_logs.empty else pd.DataFrame()

    top_rules = summary.head(25).copy() if not summary.empty else pd.DataFrame()
    fig_paths = make_plots(summary, logs, fig_dir)

    if not logs.empty:
        logs.to_csv(report_dir / "optimizer_trade_logs.csv", index=False)
    if not summary.empty:
        summary.to_csv(report_dir / "optimizer_strategy_summary.csv", index=False)

    report_md = build_report(summary, top_rules, "optimization_figures")
    (report_dir / "optimization_report.md").write_text(report_md, encoding="utf-8")
    summary_json = {
        "rows_features": int(len(features)),
        "rows_walkforward": int(len(wf)),
        "rows_summary": int(len(summary)),
        "figure_count": len(fig_paths),
    }
    (report_dir / "optimizer_summary.json").write_text(json.dumps(summary_json, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary_json, ensure_ascii=False))


if __name__ == "__main__":
    main()

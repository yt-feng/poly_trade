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


def load_features(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["first_quote_ts"] = pd.to_datetime(df["first_quote_ts"], utc=True, errors="coerce")
    df = df[df["outcome_up"].notna()].copy().sort_values("first_quote_ts").reset_index(drop=True)
    df["move_bucket"] = pd.cut(df["btc_move_2m"], bins=MOVE_BINS, labels=MOVE_LABELS, include_lowest=True)
    df["up_price_bucket"] = pd.cut(df["buy_up_price_2m"], bins=PRICE_BINS, labels=PRICE_LABELS, include_lowest=True)
    df["down_price_bucket"] = pd.cut(df["buy_down_price_2m"], bins=PRICE_BINS, labels=PRICE_LABELS, include_lowest=True)
    df["size_sign"] = np.where(df["size_imbalance_updown_2m"] <= -0.1, "neg", np.where(df["size_imbalance_updown_2m"] >= 0.1, "pos", "neu"))
    df["liq_sign_down"] = np.where(df["buy_down_size_2m"] >= 350, "high", np.where(df["buy_down_size_2m"] >= 200, "mid", "low"))
    df["liq_sign_up"] = np.where(df["buy_up_size_2m"] >= 350, "high", np.where(df["buy_up_size_2m"] >= 200, "mid", "low"))
    return df


def payout_per_share(row: pd.Series, side: str, fee: float) -> tuple[float, float, float]:
    if side == "buy_up":
        price = row["buy_up_price_2m"]
        size = row["buy_up_size_2m"]
        pnl_per_share = row["outcome_up"] - price - fee
    elif side == "buy_down":
        price = row["buy_down_price_2m"]
        size = row["buy_down_size_2m"]
        pnl_per_share = (1.0 - row["outcome_up"]) - price - fee
    else:
        return np.nan, np.nan, np.nan
    return float(price), float(size), float(pnl_per_share)


def kelly_fraction(prob: float, price: float) -> float:
    if pd.isna(prob) or pd.isna(price) or price <= 0 or price >= 1:
        return 0.0
    denom = 1.0 - price
    if denom <= 0:
        return 0.0
    return float(max((prob - price) / denom, 0.0))


def add_static_rules(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    move = out["btc_move_2m"]
    down_p = out["buy_down_price_2m"]
    up_p = out["buy_up_price_2m"]
    imb = out["size_imbalance_updown_2m"]
    down_sz = out["buy_down_size_2m"]
    up_sz = out["buy_up_size_2m"]
    depth_imb = out["depth_imbalance_updown_2m"]

    score_down = (
        (move <= -10).astype(int)
        + (down_p <= 0.80).astype(int)
        + (imb <= 0).astype(int)
        + (down_sz >= 250).astype(int)
        + (depth_imb <= 0).astype(int)
    )
    score_up = (
        ((move > 30) & (move <= 50)).astype(int)
        + (up_p <= 0.80).astype(int)
        + (imb >= 0).astype(int)
        + (up_sz >= 250).astype(int)
        + (depth_imb >= 0).astype(int)
    )

    out["classic_trend_down_basic"] = np.where(move <= -10, "buy_down", "skip")
    out["classic_trend_down_score3"] = np.where((move <= -10) & (score_down >= 3), "buy_down", "skip")
    out["classic_trend_down_score4"] = np.where((move <= -10) & (score_down >= 4), "buy_down", "skip")
    out["classic_trend_down_midrange"] = np.where((move <= -10) & (move > -50), "buy_down", "skip")
    out["classic_trend_down_liq"] = np.where((move <= -10) & (down_sz >= 250), "buy_down", "skip")
    out["classic_trend_down_price"] = np.where((move <= -10) & (down_p <= 0.80), "buy_down", "skip")
    out["classic_trend_down_combo"] = np.where((move <= -10) & (down_p <= 0.80) & (imb <= 0) & (down_sz >= 250), "buy_down", "skip")
    out["classic_trend_up_basic"] = np.where((move > 30) & (move <= 50), "buy_up", "skip")
    out["classic_trend_up_score3"] = np.where(((move > 30) & (move <= 50)) & (score_up >= 3), "buy_up", "skip")
    out["classic_neutral_value_up"] = np.where((move > -10) & (move <= 10) & (up_p <= 0.55) & (imb >= 0), "buy_up", "skip")
    return out


def estimate_q(hist: pd.DataFrame, row: pd.Series, side: str) -> float:
    if side == "buy_down":
        outcome = 1.0 - hist["outcome_up"]
        price_bucket_col = "down_price_bucket"
        liq_col = "liq_sign_down"
    else:
        outcome = hist["outcome_up"]
        price_bucket_col = "up_price_bucket"
        liq_col = "liq_sign_up"

    masks = [
        (hist["move_bucket"] == row["move_bucket"]) & (hist[price_bucket_col] == row[price_bucket_col]) & (hist["size_sign"] == row["size_sign"]),
        (hist["move_bucket"] == row["move_bucket"]) & (hist[liq_col] == row[liq_col]),
        (hist["move_bucket"] == row["move_bucket"]),
        (hist[price_bucket_col] == row[price_bucket_col]),
        pd.Series([True] * len(hist), index=hist.index),
    ]
    for mask in masks:
        subset = hist[mask].copy()
        if len(subset) >= 10:
            return float(outcome.loc[subset.index].mean())
    return np.nan


def build_dynamic_strategies(df: pd.DataFrame, fee: float, min_history: int = 60) -> pd.DataFrame:
    out = df.copy().reset_index(drop=True)
    cols = {
        "value_down_margin2": [],
        "value_down_margin5": [],
        "value_down_margin2_book": [],
        "value_down_margin2_q": [],
        "value_down_margin5_q": [],
        "value_down_margin2_book_q": [],
        "value_up_margin2": [],
        "value_up_margin2_q": [],
        "rolling_rule_drop10_down": [],
        "rolling_rule_drop10_down_q": [],
    }
    hist_rule_pnls: List[float] = []
    for i in range(len(out)):
        row = out.iloc[i]
        if i < min_history:
            cols["value_down_margin2"].append("skip")
            cols["value_down_margin5"].append("skip")
            cols["value_down_margin2_book"].append("skip")
            cols["value_down_margin2_q"].append(np.nan)
            cols["value_down_margin5_q"].append(np.nan)
            cols["value_down_margin2_book_q"].append(np.nan)
            cols["value_up_margin2"].append("skip")
            cols["value_up_margin2_q"].append(np.nan)
            cols["rolling_rule_drop10_down"].append("skip")
            cols["rolling_rule_drop10_down_q"].append(np.nan)
        else:
            hist = out.iloc[:i].copy()
            qd = estimate_q(hist, row, "buy_down")
            qu = estimate_q(hist, row, "buy_up")
            cols["value_down_margin2_q"].append(qd)
            cols["value_down_margin5_q"].append(qd)
            cols["value_down_margin2_book_q"].append(qd)
            cols["value_up_margin2_q"].append(qu)
            cols["rolling_rule_drop10_down_q"].append(np.nan if len(hist_rule_pnls) < 8 else float(np.mean(hist_rule_pnls[-20:])))

            if pd.notna(qd) and row["btc_move_2m"] <= -10 and qd > row["buy_down_price_2m"] + 0.02:
                cols["value_down_margin2"].append("buy_down")
            else:
                cols["value_down_margin2"].append("skip")

            if pd.notna(qd) and row["btc_move_2m"] <= -10 and qd > row["buy_down_price_2m"] + 0.05:
                cols["value_down_margin5"].append("buy_down")
            else:
                cols["value_down_margin5"].append("skip")

            if pd.notna(qd) and row["btc_move_2m"] <= -10 and qd > row["buy_down_price_2m"] + 0.02 and row["size_imbalance_updown_2m"] <= 0 and row["buy_down_size_2m"] >= 250:
                cols["value_down_margin2_book"].append("buy_down")
            else:
                cols["value_down_margin2_book"].append("skip")

            if pd.notna(qu) and (30 < row["btc_move_2m"] <= 50) and qu > row["buy_up_price_2m"] + 0.02:
                cols["value_up_margin2"].append("buy_up")
            else:
                cols["value_up_margin2"].append("skip")

            if row["classic_trend_down_basic"] == "buy_down":
                if len(hist_rule_pnls) >= 8 and float(np.mean(hist_rule_pnls[-20:])) > 0.05:
                    cols["rolling_rule_drop10_down"].append("buy_down")
                else:
                    cols["rolling_rule_drop10_down"].append("skip")
            else:
                cols["rolling_rule_drop10_down"].append("skip")
        if row["classic_trend_down_basic"] == "buy_down":
            hist_rule_pnls.append((1.0 - row["outcome_up"]) - row["buy_down_price_2m"] - fee)
    for k, v in cols.items():
        out[k] = v
    return out


def strategy_names(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if c.startswith("classic_") or c.startswith("value_") or c.startswith("rolling_")]


def strategy_q_col(strategy: str) -> str | None:
    q_map = {
        "value_down_margin2": "value_down_margin2_q",
        "value_down_margin5": "value_down_margin5_q",
        "value_down_margin2_book": "value_down_margin2_book_q",
        "value_up_margin2": "value_up_margin2_q",
        "rolling_rule_drop10_down": "rolling_rule_drop10_down_q",
    }
    return q_map.get(strategy)


def simulate_strategy(df: pd.DataFrame, strategy: str, fee: float, sizing_name: str, starting_bankroll: float = 100.0) -> tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = starting_bankroll
    peak = starting_bankroll
    max_dd = 0.0
    rows = []
    q_col = strategy_q_col(strategy)
    for _, row in df.iterrows():
        side = row[strategy]
        if side not in {"buy_up", "buy_down"}:
            continue
        price, size_avail, pnl_per_share = payout_per_share(row, side, fee)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue
        if sizing_name.startswith("fixed_"):
            frac = float(sizing_name.replace("fixed_", "").replace("pct", "")) / 100.0
            target_cost = bankroll * frac
        else:
            if q_col is None or pd.isna(row[q_col]):
                continue
            mult = 1.0 if sizing_name == "full_kelly_capped20" else 0.5 if sizing_name == "half_kelly_capped20" else 0.25
            target_cost = bankroll * min(kelly_fraction(float(row[q_col]), float(price)) * mult, 0.20)
        target_cost = max(0.0, min(target_cost, bankroll, size_avail * price))
        if target_cost <= 0:
            continue
        shares = target_cost / price
        pnl = shares * pnl_per_share
        bankroll += pnl
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        rows.append({
            "strategy": strategy,
            "sizing": sizing_name,
            "first_quote_ts": row["first_quote_ts"],
            "slug": row["slug"],
            "side": side,
            "target_cost": target_cost,
            "shares": shares,
            "entry_price": price,
            "pnl_usd": pnl,
            "bankroll_after": bankroll,
        })
    trade_log = pd.DataFrame(rows)
    summary = {
        "strategy": strategy,
        "sizing": sizing_name,
        "trades": int(len(trade_log)),
        "ending_bankroll": float(trade_log["bankroll_after"].iloc[-1]) if not trade_log.empty else starting_bankroll,
        "total_return": float((trade_log["bankroll_after"].iloc[-1] / starting_bankroll) - 1.0) if not trade_log.empty else 0.0,
        "avg_trade_return_on_cost": float((trade_log["pnl_usd"] / trade_log["target_cost"]).mean()) if not trade_log.empty else np.nan,
        "max_drawdown": float(max_dd),
    }
    return trade_log, summary


def run_all(df: pd.DataFrame, fee: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    logs = []
    summaries = []
    sizings = ["fixed_10pct", "fixed_15pct", "fixed_20pct", "fixed_25pct", "full_kelly_capped20", "half_kelly_capped20", "quarter_kelly_capped20"]
    for strategy in strategy_names(df):
        for sizing in sizings:
            lg, sm = simulate_strategy(df, strategy, fee, sizing)
            logs.append(lg)
            summaries.append(sm)
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
        plt.figure(figsize=(13,5))
        plt.bar(labels, top["ending_bankroll"])
        plt.xticks(rotation=65, ha="right")
        plt.title("经典策略Top期末本金")
        plt.tight_layout()
        p = fig_dir / "classic_top_endings.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
        best = summary.iloc[0]
        s = logs[(logs["strategy"] == best["strategy"]) & (logs["sizing"] == best["sizing"])] .copy().sort_values("first_quote_ts")
        if not s.empty:
            plt.figure(figsize=(10,4))
            plt.plot(s["first_quote_ts"], s["bankroll_after"])
            plt.title(f"最佳经典策略本金曲线: {best['strategy']} | {best['sizing']}")
            plt.tight_layout()
            p = fig_dir / "classic_best_curve.png"
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))
    return paths


def build_report(summary: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# 经典量化策略补充回测")
    lines.append("")
    lines.append("## 这版试了什么")
    lines.append("")
    lines.append("- 趋势确认 + 盘口确认（price cap / size imbalance / liquidity）")
    lines.append("- 历史条件概率 value 策略（fair probability > 市场价格 + margin）")
    lines.append("- 规则近期有效性过滤（rolling performance filter）")
    lines.append("- 固定 10/15/20/25% 仓位 + capped Kelly")
    lines.append("")
    lines.append("## 候选策略-仓位结果")
    lines.append("")
    lines.append(summary.head(30).to_markdown(index=False))
    lines.append("")
    if not summary.empty:
        best = summary.iloc[0]
        lines.append("## 当前最佳经典策略")
        lines.append("")
        lines.append(f"- 策略：**{best['strategy']}**")
        lines.append(f"- 仓位：**{best['sizing']}**")
        lines.append(f"- 交易笔数：**{int(best['trades'])}**")
        lines.append(f"- 期末本金：**{best['ending_bankroll']:.2f} USD**")
        lines.append(f"- 总收益率：**{best['total_return']:.2%}**")
        lines.append(f"- 最大回撤：**{best['max_drawdown']:.2%}**")
        lines.append("")
    lines.append("## 图表")
    lines.append("")
    lines.append("### 经典策略Top期末本金")
    lines.append("")
    lines.append("![经典策略Top期末本金](classic_figures/classic_top_endings.png)")
    lines.append("")
    lines.append("### 最佳经典策略本金曲线")
    lines.append("")
    lines.append("![最佳经典策略本金曲线](classic_figures/classic_best_curve.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-file", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    df = build_dynamic_strategies(add_static_rules(load_features(Path(args.features_file))), fee=args.fee)
    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "classic_figures"
    report_dir.mkdir(parents=True, exist_ok=True)

    logs, summary = run_all(df, fee=args.fee)
    fig_paths = make_plots(summary, logs, fig_dir)

    logs.to_csv(report_dir / "classic_strategy_trade_logs.csv", index=False)
    summary.to_csv(report_dir / "classic_strategy_summary.csv", index=False)
    (report_dir / "classic_strategies_report.md").write_text(build_report(summary), encoding="utf-8")
    meta = {"rows_features": int(len(df)), "rows_summary": int(len(summary)), "figure_count": len(fig_paths)}
    (report_dir / "classic_strategy_summary.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()

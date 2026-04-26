from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


FIXED_SIZINGS = [0.10, 0.15, 0.20, 0.25]


def load_features(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["first_quote_ts"] = pd.to_datetime(df["first_quote_ts"], utc=True, errors="coerce")
    return df[df["outcome_up"].notna()].copy().sort_values("first_quote_ts").reset_index(drop=True)


def load_quotes(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "ts_utc" in df.columns:
        df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
    elif "ts_iso" in df.columns:
        df["ts_utc"] = pd.to_datetime(df["ts_iso"], utc=True, errors="coerce")
    return df.sort_values(["slug", "ts_utc"]).reset_index(drop=True)


def first_non_null(series: pd.Series):
    non_null = series.dropna()
    return np.nan if non_null.empty else non_null.iloc[0]


def derive_path_features(quotes: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, float]] = []
    for slug, g in quotes.groupby("slug", dropna=False):
        g = g.sort_values("ts_utc").copy()
        if g.empty:
            continue
        first_ts = g["ts_utc"].min()
        cutoff = first_ts + pd.Timedelta(minutes=2)
        first2 = g[g["ts_utc"] <= cutoff].copy()
        if first2.empty:
            continue
        px = pd.to_numeric(first2.get("final_price"), errors="coerce").dropna()
        if px.empty:
            continue
        diffs = px.diff().dropna()
        net_move = float(px.iloc[-1] - px.iloc[0])
        range_2m = float(px.max() - px.min())
        total_abs_move = float(diffs.abs().sum()) if not diffs.empty else 0.0
        realized_std = float(diffs.std()) if len(diffs) >= 2 else 0.0
        efficiency = float(abs(net_move) / total_abs_move) if total_abs_move > 0 else 1.0
        choppiness = float(total_abs_move / max(abs(net_move), 1e-9)) if total_abs_move > 0 else 1.0
        rows.append(
            {
                "slug": slug,
                "path_range_2m": range_2m,
                "path_total_abs_move_2m": total_abs_move,
                "path_realized_std_2m": realized_std,
                "path_efficiency_2m": efficiency,
                "path_choppiness_2m": choppiness,
            }
        )
    return pd.DataFrame(rows)


def add_engineered(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["down_pressure_2m"] = out["buy_down_price_2m"] * out["buy_down_size_2m"]
    out["up_pressure_2m"] = out["buy_up_price_2m"] * out["buy_up_size_2m"]
    denom = (out["down_pressure_2m"] + out["up_pressure_2m"]).replace(0, np.nan)
    out["pressure_imbalance_2m"] = (out["down_pressure_2m"] - out["up_pressure_2m"]) / denom

    conditions = [
        (out["btc_move_2m"] <= -10).astype(int),
        (out["buy_down_price_2m"] <= 0.80).astype(int),
        (out["buy_down_size_2m"] >= 250).astype(int),
        (out["size_imbalance_updown_2m"] <= 0).astype(int),
        (out["pressure_imbalance_2m"] >= 0).astype(int),
        (out["path_efficiency_2m"] >= 0.55).astype(int),
        (out["path_choppiness_2m"] <= 2.0).astype(int),
    ]
    out["down_score_ext"] = sum(conditions)

    out["regime"] = np.where(
        (out["btc_move_2m"] <= -10) & (out["path_efficiency_2m"] >= 0.55) & (out["path_choppiness_2m"] <= 2.0),
        "trend_down",
        np.where(
            (out["btc_move_2m"] >= 10) & (out["path_efficiency_2m"] >= 0.55) & (out["path_choppiness_2m"] <= 2.0),
            "trend_up",
            np.where(out["path_choppiness_2m"] >= 2.5, "choppy", "neutral"),
        ),
    )
    return out


def add_static_strategies(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    move = out["btc_move_2m"]
    down_p = out["buy_down_price_2m"]
    up_p = out["buy_up_price_2m"]
    imb = out["size_imbalance_updown_2m"]
    dsz = out["buy_down_size_2m"]
    usz = out["buy_up_size_2m"]
    press = out["pressure_imbalance_2m"]
    eff = out["path_efficiency_2m"]
    chop = out["path_choppiness_2m"]
    regime = out["regime"]

    # Baseline and price-only controls
    out["baseline_rule_drop10_down"] = np.where(move <= -10, "buy_down", "skip")
    out["price_interval_down_60_80"] = np.where((move <= -10) & (down_p >= 0.60) & (down_p <= 0.80), "buy_down", "skip")
    out["price_interval_down_65_80"] = np.where((move <= -10) & (down_p >= 0.65) & (down_p <= 0.80), "buy_down", "skip")
    out["price_interval_down_55_75"] = np.where((move <= -10) & (down_p >= 0.55) & (down_p <= 0.75), "buy_down", "skip")
    out["price_interval_up_45_65"] = np.where((move > 30) & (move <= 50) & (up_p >= 0.45) & (up_p <= 0.65), "buy_up", "skip")

    # 1. Volatility / path filters
    out["vol_filter_down_clean"] = np.where((move <= -10) & (eff >= 0.60) & (chop <= 1.8), "buy_down", "skip")
    out["vol_filter_down_midrange"] = np.where((move <= -10) & (move > -50) & (eff >= 0.50) & (chop <= 2.0), "buy_down", "skip")
    out["vol_filter_up_clean"] = np.where((move > 30) & (move <= 50) & (eff >= 0.60) & (chop <= 1.8), "buy_up", "skip")

    # 2. Price impact / order book pressure
    out["pressure_down_confirm"] = np.where((move <= -10) & (press >= 0.10) & (dsz >= 250), "buy_down", "skip")
    out["pressure_down_strong"] = np.where((move <= -10) & (press >= 0.20) & (down_p <= 0.80), "buy_down", "skip")
    out["pressure_up_confirm"] = np.where((move > 30) & (move <= 50) & (press <= -0.10) & (usz >= 250), "buy_up", "skip")

    # 3. Regime filters
    out["regime_trend_down"] = np.where((move <= -10) & (regime == "trend_down"), "buy_down", "skip")
    out["regime_trend_down_price"] = np.where((move <= -10) & (regime == "trend_down") & (down_p <= 0.80), "buy_down", "skip")
    out["regime_trend_up"] = np.where((move > 30) & (move <= 50) & (regime == "trend_up"), "buy_up", "skip")
    out["regime_neutral_value_up"] = np.where((regime == "neutral") & (move > -10) & (move <= 10) & (up_p <= 0.55) & (imb >= 0), "buy_up", "skip")

    # 5. Tiered position will use this signal with dynamic sizing
    out["tiered_down_score_signal"] = np.where(move <= -10, "buy_down", "skip")

    return out


def add_rolling_filters(df: pd.DataFrame, fee: float) -> pd.DataFrame:
    out = df.copy().reset_index(drop=True)
    mean_hist: List[float] = []
    win_hist: List[float] = []
    sharpe_hist: List[float] = []
    roll_mean = []
    roll_win = []
    roll_sharpe = []
    sig_pnl_filter = []
    sig_win_filter = []
    sig_sharpe_filter = []

    for _, row in out.iterrows():
        recent_mean = float(np.mean(mean_hist[-20:])) if len(mean_hist) >= 8 else np.nan
        recent_win = float(np.mean(win_hist[-20:])) if len(win_hist) >= 8 else np.nan
        recent_sharpe = float(np.mean(sharpe_hist[-20:])) if len(sharpe_hist) >= 8 else np.nan
        roll_mean.append(recent_mean)
        roll_win.append(recent_win)
        roll_sharpe.append(recent_sharpe)

        base_trade = row["baseline_rule_drop10_down"] == "buy_down"
        sig_pnl_filter.append("buy_down" if base_trade and pd.notna(recent_mean) and recent_mean > 0.05 else "skip")
        sig_win_filter.append("buy_down" if base_trade and pd.notna(recent_win) and recent_win > 0.58 else "skip")
        sig_sharpe_filter.append("buy_down" if base_trade and pd.notna(recent_sharpe) and recent_sharpe > 0.20 else "skip")

        if base_trade:
            pnl_per_share = (1.0 - row["outcome_up"]) - row["buy_down_price_2m"] - fee
            mean_hist.append(float(pnl_per_share))
            win_hist.append(float(pnl_per_share > 0))
            sharpe_hist.append(float(pnl_per_share))

    out["rolling_mean_pnl_20"] = roll_mean
    out["rolling_winrate_20"] = roll_win
    out["rolling_sharpe_proxy_20"] = roll_sharpe
    out["rolling_pnl_filter_down"] = sig_pnl_filter
    out["rolling_win_filter_down"] = sig_win_filter
    out["rolling_sharpe_filter_down"] = sig_sharpe_filter
    return out


def add_dynamic_value_strategies(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().reset_index(drop=True)
    q_down_2 = []
    q_down_5 = []
    q_down_book = []
    q_up_2 = []
    sig_down_2 = []
    sig_down_5 = []
    sig_down_book = []
    sig_up_2 = []

    for i in range(len(out)):
        row = out.iloc[i]
        if i < 60:
            q_down_2.append(np.nan); q_down_5.append(np.nan); q_down_book.append(np.nan); q_up_2.append(np.nan)
            sig_down_2.append("skip"); sig_down_5.append("skip"); sig_down_book.append("skip"); sig_up_2.append("skip")
            continue
        hist = out.iloc[:i].copy()

        def est_q(side: str) -> float:
            if side == "buy_down":
                outcome = 1.0 - hist["outcome_up"]
                masks = [
                    (hist["regime"] == row["regime"]) & (hist["down_price_bucket"] == pd.cut(pd.Series([row["buy_down_price_2m"]]), bins=[0,0.2,0.4,0.6,0.8,1.0], include_lowest=True).astype(str).iloc[0] if False else True),
                ]
                # simpler fallback set
                subsets = [
                    hist[(hist["regime"] == row["regime"]) & (hist["move_bucket"] == row["move_bucket"])],
                    hist[(hist["move_bucket"] == row["move_bucket"])],
                    hist[(hist["regime"] == row["regime"])],
                    hist,
                ]
            else:
                outcome = hist["outcome_up"]
                subsets = [
                    hist[(hist["regime"] == row["regime"]) & (hist["move_bucket"] == row["move_bucket"])],
                    hist[(hist["move_bucket"] == row["move_bucket"])],
                    hist[(hist["regime"] == row["regime"])],
                    hist,
                ]
            for sub in subsets:
                if len(sub) >= 10:
                    return float(outcome.loc[sub.index].mean())
            return np.nan

        qd = est_q("buy_down")
        qu = est_q("buy_up")
        q_down_2.append(qd)
        q_down_5.append(qd)
        q_down_book.append(qd)
        q_up_2.append(qu)

        sig_down_2.append("buy_down" if row["btc_move_2m"] <= -10 and pd.notna(qd) and qd > row["buy_down_price_2m"] + 0.02 else "skip")
        sig_down_5.append("buy_down" if row["btc_move_2m"] <= -10 and pd.notna(qd) and qd > row["buy_down_price_2m"] + 0.05 else "skip")
        sig_down_book.append("buy_down" if row["btc_move_2m"] <= -10 and pd.notna(qd) and qd > row["buy_down_price_2m"] + 0.02 and row["size_imbalance_updown_2m"] <= 0 and row["buy_down_size_2m"] >= 250 else "skip")
        sig_up_2.append("buy_up" if 30 < row["btc_move_2m"] <= 50 and pd.notna(qu) and qu > row["buy_up_price_2m"] + 0.02 else "skip")

    out["value_down_margin2_q"] = q_down_2
    out["value_down_margin5_q"] = q_down_5
    out["value_down_margin2_book_q"] = q_down_book
    out["value_up_margin2_q"] = q_up_2
    out["value_down_margin2"] = sig_down_2
    out["value_down_margin5"] = sig_down_5
    out["value_down_margin2_book"] = sig_down_book
    out["value_up_margin2"] = sig_up_2
    return out


def strategy_names(df: pd.DataFrame) -> List[str]:
    return [
        c
        for c in df.columns
        if c.startswith("baseline_")
        or c.startswith("price_interval_")
        or c.startswith("vol_filter_")
        or c.startswith("pressure_")
        or c.startswith("regime_")
        or c.startswith("rolling_")
        or c.startswith("value_")
        or c == "tiered_down_score_signal"
    ]


def q_col_for_strategy(strategy: str) -> str | None:
    mapping = {
        "value_down_margin2": "value_down_margin2_q",
        "value_down_margin5": "value_down_margin5_q",
        "value_down_margin2_book": "value_down_margin2_book_q",
        "value_up_margin2": "value_up_margin2_q",
    }
    return mapping.get(strategy)


def payout(row: pd.Series, side: str, fee: float) -> Tuple[float, float, float]:
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


def simulate_strategy(df: pd.DataFrame, strategy: str, fee: float, sizing_name: str, starting_bankroll: float = 100.0) -> Tuple[pd.DataFrame, Dict[str, float]]:
    bankroll = starting_bankroll
    peak = starting_bankroll
    max_dd = 0.0
    rows = []
    q_col = q_col_for_strategy(strategy)

    for _, row in df.iterrows():
        side = row[strategy]
        if side not in {"buy_up", "buy_down"}:
            continue
        price, size_avail, pnl_per_share = payout(row, side, fee)
        if pd.isna(price) or pd.isna(size_avail) or price <= 0:
            continue

        if sizing_name.startswith("fixed_"):
            frac = float(sizing_name.replace("fixed_", "").replace("pct", "")) / 100.0
            target_cost = bankroll * frac
        elif sizing_name == "tiered_score":
            if strategy != "tiered_down_score_signal":
                continue
            score = row["down_score_ext"]
            frac = 0.10 if score <= 3 else 0.15 if score == 4 else 0.20 if score == 5 else 0.25
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
        rows.append(
            {
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
            }
        )

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


def run_all(df: pd.DataFrame, fee: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    logs = []
    summaries = []
    for strategy in strategy_names(df):
        sizings = [f"fixed_{int(x*100)}pct" for x in FIXED_SIZINGS]
        if strategy == "tiered_down_score_signal":
            sizings.append("tiered_score")
        if q_col_for_strategy(strategy) is not None:
            sizings.extend(["full_kelly_capped20", "half_kelly_capped20", "quarter_kelly_capped20"])
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
        plt.figure(figsize=(13, 5))
        plt.bar(labels, top["ending_bankroll"])
        plt.xticks(rotation=65, ha="right")
        plt.title("扩展经典策略Top期末本金")
        plt.tight_layout()
        p = fig_dir / "extended_top_endings.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))
        best = summary.iloc[0]
        s = logs[(logs["strategy"] == best["strategy"]) & (logs["sizing"] == best["sizing"])].copy().sort_values("first_quote_ts")
        if not s.empty:
            plt.figure(figsize=(10, 4))
            plt.plot(s["first_quote_ts"], s["bankroll_after"])
            plt.title(f"最佳扩展经典策略本金曲线: {best['strategy']} | {best['sizing']}")
            plt.tight_layout()
            p = fig_dir / "extended_best_curve.png"
            plt.savefig(p, dpi=150)
            plt.close()
            paths.append(str(p))
    return paths


def build_report(summary: pd.DataFrame) -> str:
    lines: List[str] = []
    lines.append("# 扩展经典策略回测")
    lines.append("")
    lines.append("## 这版覆盖的 6 类策略")
    lines.append("")
    lines.append("1. 波动率 / 路径过滤")
    lines.append("2. 盘口压力 / 价格冲击")
    lines.append("3. Regime filter")
    lines.append("4. Rolling 健康度过滤（均值 / 胜率 / Sharpe 代理）")
    lines.append("5. 分层仓位（score-based tiered sizing）")
    lines.append("6. 价格区间搜索")
    lines.append("")
    lines.append("## 候选策略-仓位结果")
    lines.append("")
    lines.append(summary.head(40).to_markdown(index=False))
    lines.append("")
    if not summary.empty:
        best = summary.iloc[0]
        lines.append("## 当前最佳扩展经典策略")
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
    lines.append("### 扩展经典策略Top期末本金")
    lines.append("")
    lines.append("![扩展经典策略Top期末本金](extended_classic_figures/extended_top_endings.png)")
    lines.append("")
    lines.append("### 最佳扩展经典策略本金曲线")
    lines.append("")
    lines.append("![最佳扩展经典策略本金曲线](extended_classic_figures/extended_best_curve.png)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features-file", type=str, required=True)
    parser.add_argument("--quotes-file", type=str, required=True)
    parser.add_argument("--report-dir", type=str, required=True)
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    features = load_features(Path(args.features_file))
    quotes = load_quotes(Path(args.quotes_file))
    path_features = derive_path_features(quotes)
    df = features.merge(path_features, on="slug", how="left")
    df = add_dynamic_value_strategies(add_rolling_filters(add_static_strategies(add_engineered(df)), fee=args.fee))

    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "extended_classic_figures"
    report_dir.mkdir(parents=True, exist_ok=True)

    logs, summary = run_all(df, fee=args.fee)
    fig_paths = make_plots(summary, logs, fig_dir)

    logs.to_csv(report_dir / "extended_classic_trade_logs.csv", index=False)
    summary.to_csv(report_dir / "extended_classic_summary.csv", index=False)
    (report_dir / "extended_classic_report.md").write_text(build_report(summary), encoding="utf-8")
    meta = {"rows_features": int(len(df)), "rows_summary": int(len(summary)), "figure_count": len(fig_paths)}
    (report_dir / "extended_classic_summary.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()

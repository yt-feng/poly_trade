from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Dict, Iterable, List

import numpy as np
import pandas as pd

RUN_ID = "24869603988_attempt1"
FILE_GLOB = "*btc-updown-5m_quotes.csv"

NUMERIC_COLUMNS = [
    "buy_up_cents",
    "buy_down_cents",
    "sell_up_cents",
    "sell_down_cents",
    "buy_up_size",
    "buy_down_size",
    "sell_up_size",
    "sell_down_size",
    "mid_up_cents",
    "mid_down_cents",
    "spread_up_cents",
    "spread_down_cents",
    "bid_depth_up_5",
    "ask_depth_up_5",
    "bid_depth_down_5",
    "ask_depth_down_5",
    "level_count_bid_up",
    "level_count_ask_up",
    "level_count_bid_down",
    "level_count_ask_down",
    "target_price",
    "final_price",
    "trade_count_1s",
    "trade_volume_1s",
]


def first_non_null(series: pd.Series):
    non_null = series.dropna()
    if non_null.empty:
        return np.nan
    return non_null.iloc[0]


def last_non_null(series: pd.Series):
    non_null = series.dropna()
    if non_null.empty:
        return np.nan
    return non_null.iloc[-1]


def read_source_files(source_dir: Path) -> tuple[pd.DataFrame, List[Path]]:
    files = sorted(source_dir.glob(FILE_GLOB))
    if not files:
        raise FileNotFoundError(f"No files matching {FILE_GLOB} under {source_dir}")

    frames: List[pd.DataFrame] = []
    for path in files:
        frame = pd.read_csv(path)
        frame["source_file"] = path.name
        frames.append(frame)

    combined = pd.concat(frames, ignore_index=True)
    combined.columns = [str(c).strip() for c in combined.columns]
    return combined, files


def to_numeric_inplace(df: pd.DataFrame, columns: Iterable[str]) -> None:
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")


def parse_close_ts_from_slug(slug: str) -> pd.Timestamp:
    m = re.search(r"(\d{10})$", str(slug))
    if not m:
        return pd.NaT
    return pd.to_datetime(int(m.group(1)), unit="s", utc=True)


def bucket_seconds_to_close(seconds: float) -> str:
    if pd.isna(seconds):
        return "unknown"
    value = float(seconds)
    if value < 0:
        return "after_close"
    if value <= 30:
        return "00_30s"
    if value <= 60:
        return "30_60s"
    if value <= 120:
        return "01_02m"
    if value <= 180:
        return "02_03m"
    if value <= 240:
        return "03_04m"
    if value <= 300:
        return "04_05m"
    return "gt_05m"


def prepare_quotes(raw: pd.DataFrame) -> pd.DataFrame:
    quotes = raw.copy()
    quotes["ts_utc"] = pd.to_datetime(quotes["ts_iso"], errors="coerce", utc=True)
    to_numeric_inplace(quotes, NUMERIC_COLUMNS)

    if "mid_up_cents" in quotes.columns and "buy_up_cents" in quotes.columns and "sell_up_cents" in quotes.columns:
        quotes["mid_up_cents"] = quotes["mid_up_cents"].fillna((quotes["buy_up_cents"] + quotes["sell_up_cents"]) / 2.0)
    if "mid_down_cents" in quotes.columns and "buy_down_cents" in quotes.columns and "sell_down_cents" in quotes.columns:
        quotes["mid_down_cents"] = quotes["mid_down_cents"].fillna((quotes["buy_down_cents"] + quotes["sell_down_cents"]) / 2.0)

    quotes["close_ts_utc"] = quotes["slug"].map(parse_close_ts_from_slug)
    quotes["seconds_to_close"] = (quotes["close_ts_utc"] - quotes["ts_utc"]).dt.total_seconds()
    quotes["time_to_close_bucket"] = quotes["seconds_to_close"].map(bucket_seconds_to_close)
    quotes["mid_sum_cents"] = quotes[["mid_up_cents", "mid_down_cents"]].sum(axis=1, min_count=2)
    quotes["mid_overround_cents"] = quotes["mid_sum_cents"] - 100.0
    quotes["mid_up_prob"] = quotes["mid_up_cents"] / 100.0
    quotes["mid_down_prob"] = quotes["mid_down_cents"] / 100.0
    quotes["book_complete"] = (
        quotes["buy_up_cents"].notna()
        & quotes["buy_down_cents"].notna()
        & quotes["sell_up_cents"].notna()
        & quotes["sell_down_cents"].notna()
    )
    quotes["btc_move_from_target"] = quotes["final_price"] - quotes["target_price"]

    quotes = quotes.sort_values(["slug", "ts_utc", "source_file"]).drop_duplicates(subset=["ts_iso", "slug"], keep="last")
    quotes = quotes.reset_index(drop=True)
    return quotes


def build_markets(quotes: pd.DataFrame) -> pd.DataFrame:
    grouped = quotes.groupby("slug", dropna=False, as_index=False)
    markets = grouped.agg(
        market_url=("market_url", first_non_null),
        window_text=("window_text", first_non_null),
        first_quote_ts=("ts_utc", "min"),
        last_quote_ts=("ts_utc", "max"),
        close_ts_utc=("close_ts_utc", first_non_null),
        quote_count=("ts_utc", "size"),
        source_files=("source_file", lambda s: ", ".join(sorted(set(map(str, s.dropna()))))),
        target_price=("target_price", first_non_null),
        final_price_first=("final_price", first_non_null),
        final_price_last=("final_price", last_non_null),
        mid_up_open=("mid_up_cents", first_non_null),
        mid_up_last=("mid_up_cents", last_non_null),
        mid_up_low=("mid_up_cents", "min"),
        mid_up_high=("mid_up_cents", "max"),
        spread_up_median=("spread_up_cents", "median"),
        spread_down_median=("spread_down_cents", "median"),
        overround_median=("mid_overround_cents", "median"),
    )
    markets["can_resolve_outcome"] = markets["target_price"].notna() & markets["final_price_last"].notna()
    markets["outcome_up"] = np.where(
        markets["can_resolve_outcome"],
        (markets["final_price_last"] > markets["target_price"]).astype(float),
        np.nan,
    )
    markets["price_move_usd"] = markets["final_price_last"] - markets["target_price"]
    markets["mid_up_range"] = markets["mid_up_high"] - markets["mid_up_low"]
    return markets.sort_values("first_quote_ts").reset_index(drop=True)


def build_first2m_features(quotes: pd.DataFrame, fee: float) -> pd.DataFrame:
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
        last_row = first2.iloc[-1]
        first_row = first2.iloc[0]
        target_price = first_non_null(g["target_price"])
        final_price = last_non_null(g["final_price"])
        outcome_up = np.nan
        if pd.notna(target_price) and pd.notna(final_price):
            outcome_up = float(final_price > target_price)
        btc_move_2m = np.nan
        if pd.notna(last_row.get("final_price")) and pd.notna(target_price):
            btc_move_2m = float(last_row["final_price"] - target_price)
        mid_up_prob_2m = pd.to_numeric(last_row.get("mid_up_prob"), errors="coerce")
        if pd.isna(mid_up_prob_2m) and pd.notna(last_row.get("mid_up_cents")):
            mid_up_prob_2m = float(last_row["mid_up_cents"]) / 100.0
        entry_prob = float(mid_up_prob_2m) if pd.notna(mid_up_prob_2m) else np.nan
        realized_pnl_buy_up = np.nan
        if pd.notna(outcome_up) and pd.notna(entry_prob):
            realized_pnl_buy_up = float(outcome_up - entry_prob - fee)
        rows.append(
            {
                "slug": slug,
                "window_text": first_non_null(g["window_text"]),
                "first_quote_ts": first_ts,
                "close_ts_utc": first_non_null(g["close_ts_utc"]),
                "quote_count_total": int(len(g)),
                "quote_count_first2m": int(len(first2)),
                "target_price": target_price,
                "price_after_2m": last_non_null(first2["final_price"]),
                "final_price_last": final_price,
                "btc_move_2m": btc_move_2m,
                "btc_return_bps_2m": np.nan if pd.isna(target_price) or target_price == 0 or pd.isna(btc_move_2m) else float(btc_move_2m / target_price * 10000.0),
                "mid_up_prob_open": pd.to_numeric(first_row.get("mid_up_prob"), errors="coerce"),
                "mid_up_prob_2m": entry_prob,
                "mid_up_prob_change_2m": np.nan if pd.isna(entry_prob) or pd.isna(pd.to_numeric(first_row.get("mid_up_prob"), errors="coerce")) else float(entry_prob - pd.to_numeric(first_row.get("mid_up_prob"), errors="coerce")),
                "spread_up_median_first2m": pd.to_numeric(first2["spread_up_cents"], errors="coerce").median(),
                "spread_down_median_first2m": pd.to_numeric(first2["spread_down_cents"], errors="coerce").median(),
                "overround_median_first2m": pd.to_numeric(first2["mid_overround_cents"], errors="coerce").median(),
                "trade_count_sum_first2m": pd.to_numeric(first2["trade_count_1s"], errors="coerce").sum(min_count=1),
                "trade_volume_sum_first2m": pd.to_numeric(first2["trade_volume_1s"], errors="coerce").sum(min_count=1),
                "outcome_up": outcome_up,
                "realized_pnl_buy_up_from_2m": realized_pnl_buy_up,
            }
        )
    features = pd.DataFrame(rows)
    if not features.empty:
        features = features.sort_values("first_quote_ts").reset_index(drop=True)
    return features


def threshold_rule_table(features: pd.DataFrame, fee: float) -> pd.DataFrame:
    usable = features[features["outcome_up"].notna() & features["mid_up_prob_2m"].notna() & features["btc_move_2m"].notna()].copy()
    rows = []
    for th in [10, 20, 30, 40, 50, 75, 100]:
        for mode in ["momentum", "mean_reversion"]:
            if mode == "momentum":
                take = usable["btc_move_2m"] >= th
            else:
                take = usable["btc_move_2m"] >= th
            subset = usable[take].copy()
            if subset.empty:
                continue
            if mode == "momentum":
                pnl = subset["outcome_up"] - subset["mid_up_prob_2m"] - fee
                win = subset["outcome_up"]
            else:
                pnl = (1.0 - subset["outcome_up"]) - (1.0 - subset["mid_up_prob_2m"]) - fee
                win = 1.0 - subset["outcome_up"]
            rows.append({
                "strategy": mode,
                "threshold_usd": th,
                "trades": int(len(subset)),
                "win_rate": float(win.mean()),
                "avg_entry_prob": float(subset["mid_up_prob_2m"].mean()),
                "avg_pnl": float(pnl.mean()),
                "cum_pnl": float(pnl.sum()),
            })
    return pd.DataFrame(rows)


def missingness_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "column": df.columns,
        "missing_ratio": [df[c].isna().mean() for c in df.columns],
        "non_null": [int(df[c].notna().sum()) for c in df.columns],
    }).sort_values("missing_ratio", ascending=False).reset_index(drop=True)


def markdown_table(df: pd.DataFrame, rows: int = 20) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(rows).copy()
    numeric_cols = show.select_dtypes(include=[np.number]).columns
    show[numeric_cols] = show[numeric_cols].round(4)
    return show.to_markdown(index=False)


def write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def maybe_make_plots(features: pd.DataFrame, threshold_table: pd.DataFrame, fig_dir: Path) -> List[str]:
    paths: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return paths
    fig_dir.mkdir(parents=True, exist_ok=True)

    if not features.empty and features["btc_move_2m"].notna().any():
        plt.figure(figsize=(8, 4))
        plt.hist(features["btc_move_2m"].dropna(), bins=30)
        plt.title("BTC move in first 2 minutes (USD)")
        plt.tight_layout()
        p = fig_dir / "btc_move_2m_hist.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    usable = features[features["btc_move_2m"].notna() & features["outcome_up"].notna()].copy()
    if not usable.empty:
        usable["move_bin"] = pd.cut(usable["btc_move_2m"], bins=[-1000, -100, -50, -30, -10, 10, 30, 50, 100, 1000], include_lowest=True)
        agg = usable.groupby("move_bin", observed=False)["outcome_up"].mean()
        plt.figure(figsize=(10, 4))
        agg.plot(kind="bar")
        plt.title("Up finish rate by first-2-minute BTC move bucket")
        plt.tight_layout()
        p = fig_dir / "up_rate_by_btc_move_bucket.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    if not threshold_table.empty:
        pivot = threshold_table.pivot(index="threshold_usd", columns="strategy", values="avg_pnl")
        plt.figure(figsize=(8, 4))
        pivot.plot(ax=plt.gca())
        plt.title("Average PnL by threshold rule")
        plt.tight_layout()
        p = fig_dir / "avg_pnl_by_threshold_strategy.png"
        plt.savefig(p, dpi=150)
        plt.close()
        paths.append(str(p))

    return paths


def build_report(files: List[Path], quotes: pd.DataFrame, features: pd.DataFrame, thresholds: pd.DataFrame, plots: List[str], fee: float) -> str:
    lines: List[str] = []
    lines.append(f"# First-2-minute predictive research for {RUN_ID}")
    lines.append("")
    lines.append("## Research question")
    lines.append("")
    lines.append("Given the **first 2 minutes** of BTC move and quote information, can we predict the final 5-minute Up/Down result or choose a better trading rule?")
    lines.append("")
    lines.append("## Data summary")
    lines.append("")
    lines.append(f"- Source files: **{len(files)}**")
    lines.append(f"- Clean quotes: **{len(quotes)}**")
    lines.append(f"- Market feature rows: **{len(features)}**")
    lines.append(f"- Fee used in rule evaluation: **{fee:.4f}**")
    if not features.empty and features["outcome_up"].notna().any():
        lines.append(f"- Up rate on resolved markets: **{features.loc[features['outcome_up'].notna(), 'outcome_up'].mean():.4f}**")
    lines.append("")
    lines.append("## First-2-minute feature sample")
    lines.append("")
    sample_cols = ["slug", "window_text", "btc_move_2m", "mid_up_prob_2m", "outcome_up", "realized_pnl_buy_up_from_2m"]
    lines.append(markdown_table(features[sample_cols] if not features.empty else pd.DataFrame(), rows=20))
    lines.append("")
    lines.append("## Threshold-rule comparison")
    lines.append("")
    lines.append(markdown_table(thresholds, rows=30))
    lines.append("")
    lines.append("## Missingness")
    lines.append("")
    lines.append(markdown_table(missingness_table(quotes), rows=15))
    lines.append("")
    if plots:
        lines.append("## Generated figures")
        lines.append("")
        for p in plots:
            lines.append(f"- `{p}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cleaned dataset and first-2-minute predictive research for source run 24869603988_attempt1")
    parser.add_argument("--source-dir", type=str, required=True)
    parser.add_argument("--cleaned-dir", type=str, default="data/cleaned/run_24869603988_attempt1")
    parser.add_argument("--features-dir", type=str, default="data/features/run_24869603988_attempt1")
    parser.add_argument("--report-dir", type=str, default="reports/run_24869603988_attempt1_predictive")
    parser.add_argument("--fee", type=float, default=0.01)
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    cleaned_dir = Path(args.cleaned_dir)
    features_dir = Path(args.features_dir)
    report_dir = Path(args.report_dir)
    fig_dir = report_dir / "figures"
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    features_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    raw, files = read_source_files(source_dir)
    quotes = prepare_quotes(raw)
    markets = build_markets(quotes)
    features = build_first2m_features(quotes, fee=args.fee)
    thresholds = threshold_rule_table(features, fee=args.fee)
    plots = maybe_make_plots(features, thresholds, fig_dir)

    quotes.to_csv(cleaned_dir / "btc_updown_5m_quotes_clean.csv", index=False)
    markets.to_csv(cleaned_dir / "btc_updown_5m_markets_clean.csv", index=False)
    features.to_csv(features_dir / "market_features_first2m.csv", index=False)
    thresholds.to_csv(report_dir / "threshold_rule_comparison.csv", index=False)

    report_md = build_report(files, quotes, features, thresholds, plots, fee=args.fee)
    (report_dir / "report.md").write_text(report_md, encoding="utf-8")

    summary = {
        "run_id": RUN_ID,
        "source_dir": str(source_dir),
        "source_file_count": len(files),
        "quote_rows": int(len(quotes)),
        "market_rows": int(len(markets)),
        "feature_rows": int(len(features)),
        "threshold_rows": int(len(thresholds)),
    }
    write_json(report_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

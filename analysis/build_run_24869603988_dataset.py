from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

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

    quotes = quotes.sort_values(["ts_utc", "slug", "source_file"]).drop_duplicates(subset=["ts_iso", "slug"], keep="last")
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
        mid_down_open=("mid_down_cents", first_non_null),
        mid_down_last=("mid_down_cents", last_non_null),
        spread_up_median=("spread_up_cents", "median"),
        spread_down_median=("spread_down_cents", "median"),
        overround_median=("mid_overround_cents", "median"),
    )

    markets["window_seconds"] = (markets["close_ts_utc"] - markets["first_quote_ts"]).dt.total_seconds()
    markets["has_target_price"] = markets["target_price"].notna()
    markets["has_final_price_last"] = markets["final_price_last"].notna()
    markets["can_resolve_outcome"] = markets["has_target_price"] & markets["has_final_price_last"]
    markets["outcome_up"] = np.where(
        markets["can_resolve_outcome"],
        (markets["final_price_last"] > markets["target_price"]).astype(float),
        np.nan,
    )
    markets["price_move_bps"] = np.where(
        markets["can_resolve_outcome"] & markets["target_price"].ne(0),
        (markets["final_price_last"] / markets["target_price"] - 1.0) * 10000.0,
        np.nan,
    )
    markets["mid_up_range"] = markets["mid_up_high"] - markets["mid_up_low"]
    return markets.sort_values("first_quote_ts").reset_index(drop=True)


def build_snapshot_calibration(quotes: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    outcome_map = markets.set_index("slug")["outcome_up"]
    usable = quotes.copy()
    usable["outcome_up"] = usable["slug"].map(outcome_map)
    usable = usable[usable["outcome_up"].notna() & usable["mid_up_prob"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()

    usable = usable[usable["time_to_close_bucket"].isin(["04_05m", "03_04m", "02_03m", "01_02m", "30_60s", "00_30s"])]
    if usable.empty:
        return pd.DataFrame()

    usable = usable.sort_values(["slug", "seconds_to_close", "ts_utc"], ascending=[True, False, True])
    snapshots = usable.groupby(["slug", "time_to_close_bucket"], as_index=False).tail(1)

    bins = pd.cut(snapshots["mid_up_prob"], bins=np.linspace(0, 1, 11), include_lowest=True, duplicates="drop")
    snapshots["prob_bin"] = bins.astype(str)

    calibration = snapshots.groupby(["time_to_close_bucket", "prob_bin"], as_index=False).agg(
        count=("outcome_up", "size"),
        avg_mid_up_prob=("mid_up_prob", "mean"),
        realized_up_rate=("outcome_up", "mean"),
    )
    calibration["edge"] = calibration["realized_up_rate"] - calibration["avg_mid_up_prob"]
    return calibration.sort_values(["time_to_close_bucket", "avg_mid_up_prob"]).reset_index(drop=True)


def build_last_quote_summary(quotes: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    outcome_map = markets.set_index("slug")["outcome_up"]
    usable = quotes.copy()
    usable["outcome_up"] = usable["slug"].map(outcome_map)
    usable = usable[usable["outcome_up"].notna() & usable["mid_up_prob"].notna()].copy()
    if usable.empty:
        return pd.DataFrame()

    usable["abs_seconds_to_close"] = usable["seconds_to_close"].abs()
    last_quotes = usable.sort_values(["slug", "abs_seconds_to_close", "ts_utc"]).groupby("slug", as_index=False).head(1)
    bins = pd.cut(last_quotes["mid_up_prob"], bins=np.linspace(0, 1, 11), include_lowest=True, duplicates="drop")
    last_quotes["prob_bin"] = bins.astype(str)

    summary = last_quotes.groupby("prob_bin", as_index=False).agg(
        count=("outcome_up", "size"),
        avg_mid_up_prob=("mid_up_prob", "mean"),
        realized_up_rate=("outcome_up", "mean"),
        avg_seconds_to_close=("seconds_to_close", "mean"),
    )
    summary["edge"] = summary["realized_up_rate"] - summary["avg_mid_up_prob"]
    return summary.sort_values("avg_mid_up_prob").reset_index(drop=True)


def missingness_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "column": df.columns,
            "missing_ratio": [df[c].isna().mean() for c in df.columns],
            "non_null": [int(df[c].notna().sum()) for c in df.columns],
        }
    ).sort_values("missing_ratio", ascending=False).reset_index(drop=True)


def describe_series(series: pd.Series) -> Dict[str, float]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return {}
    return {
        "count": float(clean.size),
        "mean": float(clean.mean()),
        "median": float(clean.median()),
        "p10": float(clean.quantile(0.10)),
        "p90": float(clean.quantile(0.90)),
        "min": float(clean.min()),
        "max": float(clean.max()),
    }


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


def build_report(
    files: List[Path],
    quotes: pd.DataFrame,
    markets: pd.DataFrame,
    calibration: pd.DataFrame,
    last_quote_summary: pd.DataFrame,
    missing: pd.DataFrame,
) -> str:
    summary: Dict[str, object] = {
        "run_id": RUN_ID,
        "source_file_count": len(files),
        "quote_rows": int(len(quotes)),
        "market_count": int(markets["slug"].nunique()),
        "quote_time_start": None if quotes["ts_utc"].dropna().empty else str(quotes["ts_utc"].min()),
        "quote_time_end": None if quotes["ts_utc"].dropna().empty else str(quotes["ts_utc"].max()),
        "resolvable_market_count": int(markets["can_resolve_outcome"].sum()),
        "resolvable_market_ratio": float(markets["can_resolve_outcome"].mean()) if len(markets) else math.nan,
        "up_rate_on_resolved_markets": float(markets.loc[markets["outcome_up"].notna(), "outcome_up"].mean()) if markets["outcome_up"].notna().any() else math.nan,
        "quote_count_stats": describe_series(markets["quote_count"]),
        "spread_up_stats": describe_series(quotes.get("spread_up_cents", pd.Series(dtype=float))),
        "spread_down_stats": describe_series(quotes.get("spread_down_cents", pd.Series(dtype=float))),
        "overround_stats": describe_series(quotes.get("mid_overround_cents", pd.Series(dtype=float))),
        "seconds_to_close_stats": describe_series(quotes.get("seconds_to_close", pd.Series(dtype=float))),
    }

    lines: List[str] = []
    lines.append(f"# Analysis for source run {RUN_ID}")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(f"- Source quote files: **{len(files)}**")
    lines.append(f"- Clean quote rows: **{len(quotes)}**")
    lines.append(f"- Markets: **{markets['slug'].nunique()}**")
    if quotes["ts_utc"].notna().any():
        lines.append(f"- Time range: **{quotes['ts_utc'].min()}** to **{quotes['ts_utc'].max()}**")
    if markets["outcome_up"].notna().any():
        lines.append(f"- Resolved markets: **{int(markets['outcome_up'].notna().sum())}**")
        lines.append(f"- Up rate on resolved markets: **{markets.loc[markets['outcome_up'].notna(), 'outcome_up'].mean():.4f}**")
    lines.append("")
    lines.append("## Summary JSON")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(summary, indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("## Quote count per market")
    lines.append("")
    lines.append(markdown_table(markets[["slug", "window_text", "quote_count", "target_price", "final_price_last", "outcome_up"]]))
    lines.append("")
    lines.append("## Missingness (top 15)")
    lines.append("")
    lines.append(markdown_table(missing, rows=15))
    lines.append("")
    if not calibration.empty:
        lines.append("## Calibration by time-to-close bucket")
        lines.append("")
        lines.append(markdown_table(calibration, rows=30))
        lines.append("")
    if not last_quote_summary.empty:
        lines.append("## Last-quote calibration summary")
        lines.append("")
        lines.append(markdown_table(last_quote_summary, rows=20))
        lines.append("")
    lines.append("## Market summary sample")
    lines.append("")
    sample_cols = [
        "slug",
        "window_text",
        "quote_count",
        "target_price",
        "final_price_last",
        "mid_up_open",
        "mid_up_last",
        "mid_up_range",
        "price_move_bps",
        "outcome_up",
    ]
    lines.append(markdown_table(markets[sample_cols], rows=20))
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cleaned dataset and report for source run 24869603988_attempt1")
    parser.add_argument("--source-dir", type=str, required=True, help="Directory containing raw quote csv chunks")
    parser.add_argument("--cleaned-dir", type=str, default="data/cleaned/run_24869603988_attempt1")
    parser.add_argument("--report-dir", type=str, default="reports/run_24869603988_attempt1")
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    cleaned_dir = Path(args.cleaned_dir)
    report_dir = Path(args.report_dir)
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    raw, files = read_source_files(source_dir)
    quotes = prepare_quotes(raw)
    markets = build_markets(quotes)
    calibration = build_snapshot_calibration(quotes, markets)
    last_quote_summary = build_last_quote_summary(quotes, markets)
    missing = missingness_table(quotes)

    quotes.to_csv(cleaned_dir / "btc_updown_5m_quotes_clean.csv", index=False)
    markets.to_csv(cleaned_dir / "btc_updown_5m_markets_clean.csv", index=False)
    calibration.to_csv(report_dir / "calibration_by_time_bucket.csv", index=False)
    last_quote_summary.to_csv(report_dir / "last_quote_calibration.csv", index=False)
    missing.to_csv(report_dir / "missingness.csv", index=False)

    report_md = build_report(files, quotes, markets, calibration, last_quote_summary, missing)
    (report_dir / "report.md").write_text(report_md, encoding="utf-8")

    summary = {
        "run_id": RUN_ID,
        "source_dir": str(source_dir),
        "source_files": [p.name for p in files],
        "quote_rows": int(len(quotes)),
        "market_rows": int(len(markets)),
        "resolved_markets": int(markets["outcome_up"].notna().sum()),
    }
    write_json(report_dir / "summary.json", summary)

    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

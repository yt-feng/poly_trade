from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


def find_input_file(explicit_path: Optional[str]) -> Path:
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            raise FileNotFoundError(f"Input file not found: {p}")
        return p

    candidates: List[Path] = []
    for pattern in [
        "data/*.parquet",
        "data/*.csv",
        "data/**/*.parquet",
        "data/**/*.csv",
        "*.parquet",
        "*.csv",
    ]:
        candidates.extend(Path(".").glob(pattern))

    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        raise FileNotFoundError(
            "No input file found. Put a csv/parquet file under data/ or pass --input explicitly."
        )

    def rank(p: Path) -> Tuple[int, int]:
        name = p.name.lower()
        score = 0
        for token in ["btc", "updown", "5m", "poly", "market"]:
            if token in name:
                score += 1
        return (score, -len(str(p)))

    return sorted(candidates, key=rank, reverse=True)[0]


def load_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported file type: {path}")


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out


def choose_first(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    lowered = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def choose_contains(df: pd.DataFrame, includes_all: Iterable[str]) -> Optional[str]:
    tokens = [t.lower() for t in includes_all]
    for c in df.columns:
        lc = c.lower()
        if all(t in lc for t in tokens):
            return c
    return None


def infer_timestamp_col(df: pd.DataFrame) -> Optional[str]:
    exact = choose_first(
        df,
        [
            "timestamp",
            "ts",
            "time",
            "datetime",
            "created_at",
            "start_time",
            "market_start",
            "bucket_start",
            "window_start",
        ],
    )
    if exact:
        return exact
    for groups in [
        ["time"],
        ["date"],
        ["start", "time"],
        ["event", "time"],
    ]:
        col = choose_contains(df, groups)
        if col:
            return col
    return None


def infer_outcome_col(df: pd.DataFrame) -> Optional[str]:
    exact = choose_first(
        df,
        [
            "outcome",
            "label",
            "target",
            "winner",
            "result",
            "resolved_outcome",
            "direction",
            "updown",
            "is_up",
        ],
    )
    if exact:
        return exact
    for groups in [
        ["outcome"],
        ["winner"],
        ["result"],
        ["direction"],
    ]:
        col = choose_contains(df, groups)
        if col:
            return col
    return None


def infer_open_close_cols(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    open_candidates = [
        "btc_open",
        "open_px",
        "open_price",
        "price_open",
        "start_price",
        "start_px",
        "underlying_open",
    ]
    close_candidates = [
        "btc_close",
        "close_px",
        "close_price",
        "price_close",
        "end_price",
        "end_px",
        "underlying_close",
    ]
    open_col = choose_first(df, open_candidates)
    close_col = choose_first(df, close_candidates)
    if open_col and close_col:
        return open_col, close_col

    alt_open = None
    alt_close = None
    for c in df.columns:
        lc = c.lower()
        if alt_open is None and "open" in lc and ("price" in lc or "px" in lc or "btc" in lc):
            alt_open = c
        if alt_close is None and "close" in lc and ("price" in lc or "px" in lc or "btc" in lc):
            alt_close = c
    return open_col or alt_open, close_col or alt_close


def infer_probability_cols(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    mapping = {
        "up": [
            "p_up",
            "prob_up",
            "up_prob",
            "up_yes_mid",
            "up_yes_price",
            "yes_up_price",
            "price_up_yes",
            "mid_up_yes",
            "up_mid",
        ],
        "down": [
            "p_down",
            "prob_down",
            "down_prob",
            "down_yes_mid",
            "down_yes_price",
            "yes_down_price",
            "price_down_yes",
            "mid_down_yes",
            "down_mid",
        ],
    }
    up_col = choose_first(df, mapping["up"])
    down_col = choose_first(df, mapping["down"])
    if up_col or down_col:
        return up_col, down_col

    for c in df.columns:
        lc = c.lower()
        if up_col is None and (
            ("up" in lc and ("prob" in lc or "price" in lc or "mid" in lc))
            or lc in {"yes_price", "yes_mid"}
        ):
            up_col = c
        if down_col is None and "down" in lc and ("prob" in lc or "price" in lc or "mid" in lc):
            down_col = c
    return up_col, down_col


def to_datetime_safe(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True)


def derive_binary_outcome(df: pd.DataFrame, outcome_col: Optional[str], open_col: Optional[str], close_col: Optional[str]) -> pd.Series:
    if outcome_col:
        s = df[outcome_col]
        if pd.api.types.is_bool_dtype(s):
            return s.astype(int)
        if pd.api.types.is_numeric_dtype(s):
            vals = pd.to_numeric(s, errors="coerce")
            uniq = sorted(set(vals.dropna().astype(float).unique().tolist()))
            if set(uniq).issubset({0.0, 1.0}):
                return vals.fillna(0).astype(int)
        mapped = (
            s.astype(str)
            .str.strip()
            .str.lower()
            .replace(
                {
                    "up": 1,
                    "yes": 1,
                    "true": 1,
                    "1": 1,
                    "bull": 1,
                    "higher": 1,
                    "green": 1,
                    "down": 0,
                    "no": 0,
                    "false": 0,
                    "0": 0,
                    "bear": 0,
                    "lower": 0,
                    "red": 0,
                }
            )
        )
        numeric = pd.to_numeric(mapped, errors="coerce")
        if numeric.notna().any():
            return numeric.fillna(0).astype(int)

    if open_col and close_col:
        o = pd.to_numeric(df[open_col], errors="coerce")
        c = pd.to_numeric(df[close_col], errors="coerce")
        return (c > o).astype(int)

    raise ValueError(
        "Could not infer realized outcome. Need an outcome/label column or both open and close price columns."
    )


def summarize_missing(df: pd.DataFrame) -> pd.DataFrame:
    miss = df.isna().mean().sort_values(ascending=False)
    return pd.DataFrame({"missing_ratio": miss, "non_null": df.notna().sum()})


def longest_streak(binary: pd.Series, value: int) -> int:
    best = cur = 0
    for x in binary.fillna(-1).astype(int).tolist():
        if x == value:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def compute_transition_table(y: pd.Series) -> pd.DataFrame:
    prev = y.shift(1)
    valid = prev.notna() & y.notna()
    tab = pd.crosstab(prev[valid], y[valid], normalize="index")
    tab.index = [f"prev_{int(i)}" for i in tab.index]
    tab.columns = [f"curr_{int(i)}" for i in tab.columns]
    return tab


def calibration_table(prob: pd.Series, y: pd.Series, bins: int = 10) -> pd.DataFrame:
    valid = prob.notna() & y.notna()
    p = prob[valid].clip(0, 1)
    o = y[valid].astype(float)
    if len(p) == 0:
        return pd.DataFrame()

    cuts = pd.cut(p, bins=np.linspace(0, 1, bins + 1), include_lowest=True, duplicates="drop")
    grouped = pd.DataFrame({"p": p, "y": o, "bin": cuts}).groupby("bin", observed=False)
    out = grouped.agg(avg_prob=("p", "mean"), realized_up_rate=("y", "mean"), count=("y", "size"))
    out = out.reset_index()
    return out


def brier_score(prob: pd.Series, y: pd.Series) -> Optional[float]:
    valid = prob.notna() & y.notna()
    if not valid.any():
        return None
    p = prob[valid].clip(0, 1).astype(float)
    o = y[valid].astype(float)
    return float(np.mean((p - o) ** 2))


def log_loss(prob: pd.Series, y: pd.Series) -> Optional[float]:
    valid = prob.notna() & y.notna()
    if not valid.any():
        return None
    p = prob[valid].clip(1e-6, 1 - 1e-6).astype(float)
    o = y[valid].astype(float)
    return float(-np.mean(o * np.log(p) + (1 - o) * np.log(1 - p)))


def threshold_backtest(prob: pd.Series, y: pd.Series, side_name: str, fee: float) -> pd.DataFrame:
    valid = prob.notna() & y.notna()
    p = prob[valid].clip(0, 1).astype(float)
    o = y[valid].astype(int)
    if len(p) == 0:
        return pd.DataFrame()

    rows = []
    for th in np.round(np.arange(0.35, 0.71, 0.05), 2):
        take = p >= th
        n = int(take.sum())
        if n == 0:
            continue
        payoff = o[take] - p[take] - fee
        rows.append(
            {
                "side": side_name,
                "threshold": float(th),
                "trades": n,
                "avg_entry_prob": float(p[take].mean()),
                "win_rate": float(o[take].mean()),
                "avg_pnl_per_trade": float(payoff.mean()),
                "cum_pnl": float(payoff.sum()),
            }
        )
    return pd.DataFrame(rows)


def safe_json_dump(obj: Dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def maybe_make_plots(df: pd.DataFrame, ts_col: Optional[str], y: pd.Series, up_prob: Optional[pd.Series], out_dir: Path) -> List[str]:
    created: List[str] = []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return created

    out_dir.mkdir(parents=True, exist_ok=True)

    plt.figure(figsize=(8, 4))
    y.value_counts().sort_index().plot(kind="bar")
    plt.title("Outcome Count (0=Down, 1=Up)")
    plt.tight_layout()
    path = out_dir / "outcome_count.png"
    plt.savefig(path, dpi=150)
    plt.close()
    created.append(str(path))

    if ts_col and ts_col in df.columns:
        ts = to_datetime_safe(df[ts_col])
        valid = ts.notna()
        if valid.any():
            hour_rate = pd.DataFrame({"hour": ts[valid].dt.hour, "up": y[valid].astype(float)}).groupby("hour")["up"].mean()
            plt.figure(figsize=(8, 4))
            hour_rate.plot()
            plt.title("Up Rate by UTC Hour")
            plt.tight_layout()
            path = out_dir / "up_rate_by_hour.png"
            plt.savefig(path, dpi=150)
            plt.close()
            created.append(str(path))

    if up_prob is not None and up_prob.notna().any():
        plt.figure(figsize=(8, 4))
        up_prob.dropna().clip(0, 1).hist(bins=30)
        plt.title("Predicted Up Probability Distribution")
        plt.tight_layout()
        path = out_dir / "up_probability_distribution.png"
        plt.savefig(path, dpi=150)
        plt.close()
        created.append(str(path))

    return created


def markdown_table(df: pd.DataFrame, max_rows: int = 20) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(max_rows).copy()
    numeric_cols = show.select_dtypes(include=[np.number]).columns
    show[numeric_cols] = show[numeric_cols].round(4)
    return show.to_markdown(index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="EDA for Polymarket BTC Up/Down 5m data")
    parser.add_argument("--input", type=str, default=None, help="CSV or parquet file")
    parser.add_argument("--output-dir", type=str, default="reports/eda", help="Output directory")
    parser.add_argument(
        "--fee",
        type=float,
        default=0.0,
        help="Per-trade fixed fee/cost expressed in probability points, e.g. 0.01 means 1 cent on a $1 payoff.",
    )
    args = parser.parse_args()

    input_path = find_input_file(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = load_table(input_path)
    df = normalize_columns(raw)

    ts_col = infer_timestamp_col(df)
    outcome_col = infer_outcome_col(df)
    open_col, close_col = infer_open_close_cols(df)
    up_prob_col, down_prob_col = infer_probability_cols(df)

    if ts_col:
        df = df.sort_values(ts_col).reset_index(drop=True)

    y = derive_binary_outcome(df, outcome_col, open_col, close_col)
    up_prob = pd.to_numeric(df[up_prob_col], errors="coerce") if up_prob_col else None
    down_prob = pd.to_numeric(df[down_prob_col], errors="coerce") if down_prob_col else None

    summary = {
        "input_path": str(input_path),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "inferred": {
            "timestamp_col": ts_col,
            "outcome_col": outcome_col,
            "open_col": open_col,
            "close_col": close_col,
            "up_prob_col": up_prob_col,
            "down_prob_col": down_prob_col,
        },
        "outcome": {
            "up_rate": float(y.mean()),
            "down_rate": float(1 - y.mean()),
            "longest_up_streak": longest_streak(y, 1),
            "longest_down_streak": longest_streak(y, 0),
        },
    }

    if ts_col:
        ts = to_datetime_safe(df[ts_col])
        summary["time_range"] = {
            "start": None if ts.dropna().empty else str(ts.dropna().min()),
            "end": None if ts.dropna().empty else str(ts.dropna().max()),
            "rows_with_valid_timestamp": int(ts.notna().sum()),
        }

    missing = summarize_missing(df)
    transition = compute_transition_table(y)

    hourly = pd.DataFrame()
    weekday = pd.DataFrame()
    if ts_col:
        ts = to_datetime_safe(df[ts_col])
        valid = ts.notna()
        if valid.any():
            hourly = (
                pd.DataFrame({"hour": ts[valid].dt.hour, "up": y[valid].astype(float)})
                .groupby("hour")
                .agg(up_rate=("up", "mean"), count=("up", "size"))
                .reset_index()
            )
            weekday = (
                pd.DataFrame({"weekday": ts[valid].dt.day_name(), "up": y[valid].astype(float)})
                .groupby("weekday")
                .agg(up_rate=("up", "mean"), count=("up", "size"))
                .reset_index()
            )

    auto = {}
    for lag in [1, 2, 3, 6, 12]:
        try:
            auto[f"lag_{lag}"] = float(pd.Series(y).autocorr(lag=lag))
        except Exception:
            auto[f"lag_{lag}"] = math.nan
    summary["autocorr"] = auto

    metrics = {}
    cal_up = pd.DataFrame()
    cal_down = pd.DataFrame()
    bt_frames: List[pd.DataFrame] = []
    if up_prob is not None:
        metrics["up_brier_score"] = brier_score(up_prob, y)
        metrics["up_log_loss"] = log_loss(up_prob, y)
        cal_up = calibration_table(up_prob, y)
        bt_frames.append(threshold_backtest(up_prob, y, "buy_up_yes", args.fee))
    if down_prob is not None:
        y_down = 1 - y
        metrics["down_brier_score"] = brier_score(down_prob, y_down)
        metrics["down_log_loss"] = log_loss(down_prob, y_down)
        cal_down = calibration_table(down_prob, y_down)
        bt_frames.append(threshold_backtest(down_prob, y_down, "buy_down_yes", args.fee))
    summary["probability_metrics"] = metrics

    backtest = pd.concat([x for x in bt_frames if not x.empty], ignore_index=True) if bt_frames else pd.DataFrame()
    plots = maybe_make_plots(df, ts_col, y, up_prob, out_dir / "figures")
    summary["plots"] = plots

    safe_json_dump(summary, out_dir / "summary.json")
    missing.to_csv(out_dir / "missingness.csv")
    transition.to_csv(out_dir / "transition_matrix.csv")
    if not hourly.empty:
        hourly.to_csv(out_dir / "hourly_up_rate.csv", index=False)
    if not weekday.empty:
        weekday.to_csv(out_dir / "weekday_up_rate.csv", index=False)
    if not cal_up.empty:
        cal_up.to_csv(out_dir / "calibration_up.csv", index=False)
    if not cal_down.empty:
        cal_down.to_csv(out_dir / "calibration_down.csv", index=False)
    if not backtest.empty:
        backtest.to_csv(out_dir / "threshold_backtest.csv", index=False)

    md = []
    md.append("# BTC Up/Down 5m Data Analysis")
    md.append("")
    md.append(f"- Input: `{input_path}`")
    md.append(f"- Rows: **{len(df)}**")
    md.append(f"- Up rate: **{y.mean():.4f}**")
    md.append(f"- Down rate: **{1 - y.mean():.4f}**")
    md.append("")
    md.append("## Inferred Columns")
    md.append("")
    md.append("```json")
    md.append(json.dumps(summary["inferred"], indent=2, ensure_ascii=False))
    md.append("```")
    md.append("")
    md.append("## Transition Matrix")
    md.append("")
    md.append(markdown_table(transition.reset_index().rename(columns={"index": "state"})))
    md.append("")
    if not hourly.empty:
        md.append("## Up Rate by UTC Hour")
        md.append("")
        md.append(markdown_table(hourly))
        md.append("")
    if not weekday.empty:
        md.append("## Up Rate by Weekday")
        md.append("")
        md.append(markdown_table(weekday))
        md.append("")
    if metrics:
        md.append("## Probability Metrics")
        md.append("")
        md.append("```json")
        md.append(json.dumps(metrics, indent=2, ensure_ascii=False))
        md.append("```")
        md.append("")
    if not cal_up.empty:
        md.append("## Calibration (Up)")
        md.append("")
        md.append(markdown_table(cal_up))
        md.append("")
    if not cal_down.empty:
        md.append("## Calibration (Down)")
        md.append("")
        md.append(markdown_table(cal_down))
        md.append("")
    if not backtest.empty:
        md.append("## Threshold Backtest")
        md.append("")
        md.append(markdown_table(backtest))
        md.append("")
    md.append("## Missingness")
    md.append("")
    md.append(markdown_table(missing.reset_index().rename(columns={"index": "column"})))
    md.append("")
    if plots:
        md.append("## Generated Figures")
        md.append("")
        for p in plots:
            md.append(f"- `{p}`")
    (out_dir / "report.md").write_text("\n".join(md), encoding="utf-8")

    print(f"Loaded: {input_path}")
    print(f"Rows: {len(df)}")
    print(f"Output dir: {out_dir}")


if __name__ == "__main__":
    main()

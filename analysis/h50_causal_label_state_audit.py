"""Audit old-clean outcome-label clocks and coverage without strategy tuning.

The old replay derives ``outcome_up`` from the first target price and the last
captured final price.  H50 measures whether that last final observation is at
or after the five-minute window close, and whether coverage quality changes by
chronological run.  It does not select a strategy or use a future label to
make a trade decision.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"


def slug_start_seconds(value: object) -> float:
    match = re.search(r"-(\d{9,})$", str(value))
    if not match:
        return float("nan")
    return float(match.group(1))


def classify(lag: float, target_missing: bool, final_missing: bool) -> str:
    if target_missing:
        return "target_missing"
    if final_missing:
        return "final_missing"
    if not np.isfinite(lag):
        return "clock_unresolved"
    if lag < -30.0:
        return "final_before_window_end"
    if lag <= 30.0:
        return "final_near_window_end"
    return "final_after_window_end"


def build_market_quality(quotes: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for market_id, group in quotes.groupby("market_id", dropna=False):
        g = group.sort_values("ts_utc", kind="stable")
        first_ts = pd.Timestamp(g["ts_utc"].min())
        slug = str(g["slug"].dropna().iloc[0]) if g["slug"].notna().any() else ""
        start_seconds = slug_start_seconds(slug)
        window_end = pd.Timestamp(start_seconds, unit="s", tz="UTC") + pd.Timedelta(seconds=300) if np.isfinite(start_seconds) else pd.NaT
        target_mask = pd.to_numeric(g["target_price"], errors="coerce").notna()
        final_mask = pd.to_numeric(g["final_price"], errors="coerce").notna()
        target_ts = pd.Timestamp(g.loc[target_mask, "ts_utc"].iloc[0]) if target_mask.any() else pd.NaT
        final_ts = pd.Timestamp(g.loc[final_mask, "ts_utc"].iloc[-1]) if final_mask.any() else pd.NaT
        target = pd.to_numeric(g.loc[target_mask, "target_price"], errors="coerce").iloc[0] if target_mask.any() else np.nan
        final = pd.to_numeric(g.loc[final_mask, "final_price"], errors="coerce").iloc[-1] if final_mask.any() else np.nan
        lag = (final_ts - window_end).total_seconds() if pd.notna(final_ts) and pd.notna(window_end) else np.nan
        outcome = float(final > target) if pd.notna(final) and pd.notna(target) else np.nan
        rows.append({
            "market_id": str(market_id), "run_name": str(g["run_name"].iloc[0]), "slug": slug,
            "first_quote_ts": first_ts.isoformat(), "window_end_ts": window_end.isoformat() if pd.notna(window_end) else None,
            "target_ts": target_ts.isoformat() if pd.notna(target_ts) else None, "final_ts": final_ts.isoformat() if pd.notna(final_ts) else None,
            "quote_rows": int(len(g)), "target_rows": int(target_mask.sum()), "final_rows": int(final_mask.sum()),
            "label_lag_seconds": float(lag) if np.isfinite(lag) else None, "outcome_up": outcome,
            "quality_class": classify(lag, not target_mask.any(), not final_mask.any()),
        })
    return pd.DataFrame(rows)


def evaluate(source_root: Path, output: Path) -> dict[str, object]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name", kind="stable").reset_index(drop=True)
    complete = coverage.loc[coverage["file_count"].eq(coverage["file_count"].max()), "run_name"].astype(str).tolist()
    raw = raw[raw["run_name"].astype(str).isin(complete)].copy()
    quotes = base.prepare_quotes(raw)
    market = build_market_quality(quotes)
    market = market.sort_values(["run_name", "first_quote_ts"], kind="stable").reset_index(drop=True)
    run_order = {name: index for index, name in enumerate(complete)}
    market["run_index"] = market["run_name"].map(run_order)
    market["test_split"] = np.where(market["run_index"] < 4, "train4", np.where(market["run_index"] < 24, "selection20", "holdout59"))
    by_quality = market.groupby(["test_split", "quality_class"], dropna=False).agg(markets=("market_id", "size"), outcome_rate=("outcome_up", "mean"), median_lag_seconds=("label_lag_seconds", "median"), missing_outcome=("outcome_up", lambda s: int(s.isna().sum()))).reset_index()
    by_run = market.groupby(["run_index", "run_name", "test_split"], dropna=False).agg(markets=("market_id", "size"), outcome_rate=("outcome_up", "mean"), quality_aligned=("quality_class", lambda s: float((s == "final_near_window_end").mean())), quality_before_end=("quality_class", lambda s: float((s == "final_before_window_end").mean())), median_lag_seconds=("label_lag_seconds", "median")).reset_index()
    output.mkdir(parents=True, exist_ok=True)
    market.to_csv(output / "market_quality.csv", index=False)
    by_quality.to_csv(output / "quality_by_split.csv", index=False)
    by_run.to_csv(output / "quality_by_run.csv", index=False)
    counts = market["quality_class"].value_counts(dropna=False).to_dict()
    meta = {
        "format": "old-clean-h50-causal-label-state-audit-v1", "source_root": str(source_root), "source_commit": SOURCE_COMMIT,
        "complete_runs": len(complete), "market_rows": len(market), "quality_counts": {str(k): int(v) for k, v in counts.items()},
        "split_counts": {str(k): int(v) for k, v in market["test_split"].value_counts().items()},
        "definition": "outcome_up is derived from first non-null target_price and last non-null final_price; final clock is compared with slug start plus 300 seconds",
        "strategy_selection": False, "new_data_collected": False, "live_orders_enabled": False, "actual_fills": 0, "verified_canary_fills": 0,
        "next_use": "Use quality_by_run and quality_by_split to predeclare one label-clock or coverage-conditioned causal audit; do not select on holdout outcomes.",
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False))
    print(by_quality.to_string(index=False))
    print(by_run.to_string(index=False))
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--source-root", required=True, type=Path); parser.add_argument("--output", required=True, type=Path); args = parser.parse_args(); evaluate(args.source_root, args.output)


if __name__ == "__main__": main()

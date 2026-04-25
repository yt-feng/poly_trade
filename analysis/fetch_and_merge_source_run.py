from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

import pandas as pd


def find_chunk_files(source_root: Path, source_run_dir: str) -> List[Path]:
    run_dir = source_root / source_run_dir
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    files = sorted(run_dir.glob("part*_chunk*_btc-updown-5m_quotes.csv"))
    if not files:
        raise FileNotFoundError(f"No chunk csv files found under: {run_dir}")
    return files


def load_and_clean(files: List[Path]) -> pd.DataFrame:
    frames = []
    for idx, file_path in enumerate(files, start=1):
        df = pd.read_csv(file_path)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(axis=0, how="all").copy()
        df["source_file"] = file_path.name
        df["source_order"] = idx
        frames.append(df)

    merged = pd.concat(frames, ignore_index=True)

    if "ts_iso" in merged.columns:
        merged["ts_iso"] = pd.to_datetime(merged["ts_iso"], errors="coerce")

    numeric_candidates = [
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
    for col in numeric_candidates:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")

    dedupe_keys = [c for c in ["ts_iso", "slug"] if c in merged.columns]
    merged = merged.sort_values([c for c in ["ts_iso", "slug", "source_order"] if c in merged.columns]).reset_index(drop=True)
    if dedupe_keys:
        merged = merged.drop_duplicates(subset=dedupe_keys, keep="last").reset_index(drop=True)
    else:
        merged = merged.drop_duplicates().reset_index(drop=True)

    if "ts_iso" in merged.columns:
        merged = merged[merged["ts_iso"].notna()].copy()
        merged = merged.sort_values([c for c in ["ts_iso", "slug"] if c in merged.columns]).reset_index(drop=True)
        merged["ts_iso"] = merged["ts_iso"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        merged["ts_iso"] = merged["ts_iso"].str.replace(r"(\+\d{2})(\d{2})$", r"\1:\2", regex=True)

    return merged


def build_summary(files: List[Path], merged: pd.DataFrame, source_run_dir: str) -> dict:
    summary = {
        "source_run_dir": source_run_dir,
        "source_file_count": len(files),
        "source_files": [p.name for p in files],
        "merged_rows": int(len(merged)),
        "columns": list(merged.columns),
    }

    if "ts_iso" in merged.columns and not merged.empty:
        summary["time_range"] = {
            "start": str(merged["ts_iso"].iloc[0]),
            "end": str(merged["ts_iso"].iloc[-1]),
        }
    if "slug" in merged.columns:
        summary["unique_markets"] = int(merged["slug"].nunique())
    if "window_text" in merged.columns:
        summary["unique_windows"] = int(merged["window_text"].nunique())
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge one source run from the yt-feng/poly repo")
    parser.add_argument("--source-root", required=True, help="Local checkout path of the source repo")
    parser.add_argument("--source-run-dir", required=True, help="Run directory inside the source repo")
    parser.add_argument("--output", required=True, help="Merged clean csv output path")
    parser.add_argument("--summary-path", required=True, help="Summary json output path")
    args = parser.parse_args()

    source_root = Path(args.source_root)
    output_path = Path(args.output)
    summary_path = Path(args.summary_path)

    files = find_chunk_files(source_root, args.source_run_dir)
    merged = load_and_clean(files)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_path, index=False)

    summary = build_summary(files, merged, args.source_run_dir)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Merged csv written to: {output_path}")
    print(f"Summary json written to: {summary_path}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd


def read_base_features(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "first_quote_ts" in df.columns:
        df["first_quote_ts"] = pd.to_datetime(df["first_quote_ts"], utc=True, errors="coerce")
    return df.sort_values("first_quote_ts").reset_index(drop=True)


def _read_external_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "ts_utc" in df.columns:
        df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
    elif "timestamp" in df.columns:
        df["ts_utc"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    else:
        raise ValueError(f"External file must contain ts_utc or timestamp: {path}")
    return df.sort_values("ts_utc").reset_index(drop=True)


def merge_provider(base: pd.DataFrame, external: pd.DataFrame, provider_name: str, tolerance: str = "5min") -> pd.DataFrame:
    keep_cols = [c for c in external.columns if c not in {"timestamp"}]
    ext = external[keep_cols].copy()
    rename_map = {c: f"{provider_name}__{c}" for c in ext.columns if c != "ts_utc"}
    ext = ext.rename(columns=rename_map)
    merged = pd.merge_asof(
        base.sort_values("first_quote_ts"),
        ext.sort_values("ts_utc"),
        left_on="first_quote_ts",
        right_on="ts_utc",
        direction="backward",
        tolerance=pd.Timedelta(tolerance),
    )
    return merged


def discover_provider_files(external_root: Path) -> Dict[str, List[Path]]:
    providers: Dict[str, List[Path]] = {}
    if not external_root.exists():
        return providers
    for provider_dir in sorted([p for p in external_root.iterdir() if p.is_dir()]):
        csvs = sorted(provider_dir.glob("*.csv"))
        if csvs:
            providers[provider_dir.name] = csvs
    return providers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-features-file", type=str, required=True)
    parser.add_argument("--external-root", type=str, required=True)
    parser.add_argument("--output-file", type=str, required=True)
    parser.add_argument("--tolerance", type=str, default="5min")
    args = parser.parse_args()

    base = read_base_features(Path(args.base_features_file))
    providers = discover_provider_files(Path(args.external_root))
    merged = base.copy()
    merge_log = []

    for provider_name, files in providers.items():
        for p in files:
            try:
                ext = _read_external_csv(p)
                merged = merge_provider(merged, ext, provider_name, tolerance=args.tolerance)
                merge_log.append({"provider": provider_name, "file": p.name, "rows": int(len(ext)), "status": "merged"})
            except Exception as e:
                merge_log.append({"provider": provider_name, "file": p.name, "rows": 0, "status": f"error: {e}"})

    out_path = Path(args.output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_path, index=False)
    log_path = out_path.with_suffix(".merge_log.json")
    log_path.write_text(json.dumps(merge_log, indent=2, ensure_ascii=False), encoding="utf-8")
    print({"rows": int(len(merged)), "providers": len(providers), "merge_log": str(log_path)})


if __name__ == "__main__":
    main()

"""H370: audit a synchronized public BTC 5m source for H369 activation.

This is a bounded source-contract audit, not a strategy replay.  The sample
contains one hour of Polymarket BTC 5m, Binance, and Chainlink records on a
common collector clock.  The report only answers whether the pre-registered
lead-lag fields are present and internally coherent.  It cannot establish an
owner-scoped fill, settlement, reusable cash, or Canary row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


FORMAT = "h370-rabbitgo-synchronized-5m-source-audit-v1"
SAMPLE_ID = "RabbitGo777/polymarket.btc.5m_20260807T1100.v4.parquet"
REQUIRED_COMMON = {"seq_local", "ts_local"}
PREDECLARED_FIELDS = {
    "binance_event_ts": ("ts_local", "T", "timestamp"),
    "polymarket_book_event_ts": ("ts_local", "timestamp"),
    "chainlink_event_ts": ("ts_local", "timestamp", "payload_timestamp"),
    "transaction_hash": ("transaction_hash", "tx_hash"),
    "market_identity": ("market", "market_id", "condition_id", "slug"),
    "token_identity": ("asset_id", "token_id", "clob_token_id"),
    "winning_outcome": ("winning_outcome", "winner"),
    "best_bid": ("best_bid", "bid"),
    "best_ask": ("best_ask", "ask"),
    "binance_price": ("p", "price"),
    "chainlink_value": ("value", "full_accuracy_value"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def _parquet_file(path: Path) -> pq.ParquetFile:
    """Open metadata without materialising the complete public sample."""
    return pq.ParquetFile(path, memory_map=True)


def _batches(path: Path, columns: list[str]):
    parquet = _parquet_file(path)
    selected = [column for column in columns if column in parquet.schema_arrow.names]
    if not selected:
        return
    yield from parquet.iter_batches(columns=selected, batch_size=250_000, use_threads=True)


def _numeric(values: list[Any]) -> np.ndarray:
    return pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)


def _clock_contract(path: Path, names: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"rows": int(_parquet_file(path).metadata.num_rows)}
    missing = sorted(REQUIRED_COMMON - set(names))
    result["missing_common_fields"] = missing
    if missing or result["rows"] == 0:
        result.update({"monotonic_seq": False, "monotonic_ts": False})
        return result
    null_seq = 0
    null_ts = 0
    monotonic_seq = True
    monotonic_ts = True
    previous_seq: float | None = None
    previous_ts: float | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    for batch in _batches(path, ["seq_local", "ts_local"]):
        seq = _numeric(batch.column("seq_local").to_pylist())
        ts = _numeric(batch.column("ts_local").to_pylist())
        null_seq += int(np.isnan(seq).sum())
        null_ts += int(np.isnan(ts).sum())
        valid_seq = seq[~np.isnan(seq)]
        valid_ts = ts[~np.isnan(ts)]
        if len(valid_seq):
            monotonic_seq = monotonic_seq and bool(np.all(np.diff(valid_seq) >= 0))
            if previous_seq is not None:
                monotonic_seq = monotonic_seq and bool(valid_seq[0] >= previous_seq)
            previous_seq = float(valid_seq[-1])
        if len(valid_ts):
            monotonic_ts = monotonic_ts and bool(np.all(np.diff(valid_ts) >= 0))
            if previous_ts is not None:
                monotonic_ts = monotonic_ts and bool(valid_ts[0] >= previous_ts)
            previous_ts = float(valid_ts[-1])
            if first_ts is None:
                first_ts = float(valid_ts[0])
            last_ts = float(valid_ts[-1])
    result["null_seq_rows"] = null_seq
    result["null_ts_rows"] = null_ts
    result["monotonic_seq"] = bool(null_seq == 0 and monotonic_seq)
    result["monotonic_ts"] = bool(null_ts == 0 and monotonic_ts)
    if first_ts is not None and last_ts is not None:
        result["first_ts_local"] = int(first_ts)
        result["last_ts_local"] = int(last_ts)
        result["span_seconds"] = float((last_ts - first_ts) / 1_000_000.0)
    return result


def _coverage(path: Path, names: list[str], candidates: tuple[str, ...]) -> dict[str, Any]:
    column = _first_column(pd.DataFrame(columns=names), candidates)
    rows = int(_parquet_file(path).metadata.num_rows)
    if column is None:
        return {"column": None, "rows": rows, "non_null_rows": 0, "coverage": 0.0}
    null_rows = 0
    for batch in _batches(path, [column]):
        null_rows += int(batch.column(column).null_count)
    non_null = rows - null_rows
    return {"column": column, "rows": rows, "non_null_rows": non_null,
            "coverage": float(non_null / rows) if rows else 0.0}


def _market_count(path: Path, names: list[str]) -> dict[str, Any]:
    column = _first_column(pd.DataFrame(columns=names), ("market", "market_id", "condition_id", "slug"))
    if column is None:
        return {"identity_column": None, "unique_markets": 0, "btc_5m_markets": 0}
    unique: set[str] = set()
    for batch in _batches(path, [column]):
        for value in batch.column(column).to_pylist():
            if value is not None:
                unique.add(str(value))
    five_minute = sum("btc" in value.lower() and "5m" in value.lower() for value in unique)
    return {"identity_column": column, "unique_markets": len(unique), "btc_5m_markets": int(five_minute)}


def _read_numeric(path: Path, column: str) -> np.ndarray:
    arrays = []
    for batch in _batches(path, [column]):
        arrays.append(_numeric(batch.column(column).to_pylist()))
    return np.concatenate(arrays) if arrays else np.array([], dtype=float)


def _lead_lag_probe(polymarket: Path, polymarket_names: list[str], binance: Path, binance_names: list[str]) -> dict[str, Any]:
    """Count only causal timestamp candidates; do not infer a trade signal."""
    pm_ts_col = _first_column(pd.DataFrame(columns=polymarket_names), ("ts_local",))
    bn_ts_col = _first_column(pd.DataFrame(columns=binance_names), ("ts_local",))
    price_col = _first_column(pd.DataFrame(columns=binance_names), ("p", "price"))
    if pm_ts_col is None or bn_ts_col is None or price_col is None:
        return {"large_move_events": 0, "candidate_pairs": 0, "threshold_bps": 5.0,
                "window_ms": 500.0, "available": False}
    pm_ts = _read_numeric(polymarket, pm_ts_col)
    bn_ts = _read_numeric(binance, bn_ts_col)
    price = _read_numeric(binance, price_col)
    pm_ts = np.sort(pm_ts[~np.isnan(pm_ts)])
    valid = ~np.isnan(bn_ts) & ~np.isnan(price)
    bn_ts = bn_ts[valid]
    price = price[valid]
    order = np.argsort(bn_ts, kind="stable")
    bn_ts = bn_ts[order]
    price = price[order]
    if len(price) < 2 or len(pm_ts) == 0:
        return {"large_move_events": 0, "candidate_pairs": 0, "threshold_bps": 5.0,
                "window_ms": 500.0, "available": True}
    previous = np.roll(price, 1)
    previous[0] = np.nan
    move_bps = (price / previous - 1.0) * 10_000.0
    event_ts = bn_ts
    large = np.abs(move_bps) >= 5.0
    large &= np.isfinite(event_ts)
    large_ts = event_ts[large]
    if len(large_ts) == 0:
        return {"large_move_events": 0, "candidate_pairs": 0, "threshold_bps": 5.0,
                "window_ms": 500.0, "available": True}
    positions = np.searchsorted(pm_ts, large_ts, side="left")
    valid = positions < len(pm_ts)
    positions = positions[valid]
    source = large_ts[valid]
    paired = ((pm_ts[positions] - source) >= 0) & ((pm_ts[positions] - source) <= 500_000)
    return {"large_move_events": int(len(large_ts)), "candidate_pairs": int(paired.sum()),
            "threshold_bps": 5.0, "window_ms": 500.0, "available": True}


def audit(input_dir: Path, output: Path) -> dict[str, Any]:
    files = {
        "polymarket": input_dir / "polymarket.btc.5m_20260807T1100.v4.parquet",
        "binance": input_dir / "binance.btc_20260807T1100.v4.parquet",
        "chainlink": input_dir / "chainlink.btc_20260807T1100.v4.parquet",
    }
    missing_files = [name for name, path in files.items() if not path.is_file()]
    if missing_files:
        raise ValueError("MISSING_INPUT_FILES:" + ",".join(missing_files))
    parquet = {name: _parquet_file(path) for name, path in files.items()}
    names = {name: list(parquet[name].schema_arrow.names) for name in files}
    fields = {}
    for name, path in files.items():
        fields[name] = {
            "columns": sorted(str(column) for column in names[name]),
            "clock": _clock_contract(path, names[name]),
        }
    field_coverage = {
        name: {field: _coverage(path, names[name], candidates) for field, candidates in PREDECLARED_FIELDS.items()}
        for name, path in files.items()
    }
    report = {
        "format": FORMAT,
        "sample_id": SAMPLE_ID,
        "source_scope": "one-hour public synchronized sample; source-contract audit only",
        "files": {name: {"rows": int(parquet[name].metadata.num_rows), "sha256": _sha256(path)}
                  for name, path in files.items()},
        "field_contract": fields,
        "field_coverage": field_coverage,
        "market_identity": _market_count(files["polymarket"], names["polymarket"]),
        "lead_lag_probe": _lead_lag_probe(files["polymarket"], names["polymarket"], files["binance"], names["binance"]),
        "h369_activation": {
            "synchronized_public_fields_available": bool(
                fields["polymarket"]["clock"]["monotonic_seq"]
                and fields["binance"]["clock"]["monotonic_seq"]
                and fields["chainlink"]["clock"]["monotonic_seq"]
            ),
            "owner_scoped_execution_bundle_available": False,
            "settlement_cash_continuity_available": False,
            "sufficient_chronological_markets_for_selection_holdout": False,
            "decision": "source_fields_are_suitable_for_a_bounded_lead_lag_schema_check_only",
        },
        "protocol": {
            "initial_cash_usd": 10.0,
            "required_consecutive_fills": 10,
            "same_evolving_cash_ledger": True,
            "no_concatenation": True,
            "paper_only": True,
        },
        "new_data_collected": True,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.input_dir, args.output)
    print(json.dumps({"format": report["format"], "decision": report["h369_activation"]["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()

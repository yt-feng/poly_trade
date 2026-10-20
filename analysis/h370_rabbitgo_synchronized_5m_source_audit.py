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
import math
from pathlib import Path
from typing import Any

import pandas as pd


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


def _clock_contract(frame: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {"rows": int(len(frame))}
    missing = sorted(REQUIRED_COMMON - set(frame.columns))
    result["missing_common_fields"] = missing
    if missing or frame.empty:
        result.update({"monotonic_seq": False, "monotonic_ts": False})
        return result
    seq = pd.to_numeric(frame["seq_local"], errors="coerce")
    ts = pd.to_numeric(frame["ts_local"], errors="coerce")
    result["null_seq_rows"] = int(seq.isna().sum())
    result["null_ts_rows"] = int(ts.isna().sum())
    result["monotonic_seq"] = bool(seq.notna().all() and seq.is_monotonic_increasing)
    result["monotonic_ts"] = bool(ts.notna().all() and ts.is_monotonic_increasing)
    if ts.notna().any():
        result["first_ts_local"] = int(ts.min())
        result["last_ts_local"] = int(ts.max())
        result["span_seconds"] = float((ts.max() - ts.min()) / 1_000_000.0)
    return result


def _coverage(frame: pd.DataFrame, candidates: tuple[str, ...]) -> dict[str, Any]:
    column = _first_column(frame, candidates)
    if column is None:
        return {"column": None, "rows": int(len(frame)), "non_null_rows": 0, "coverage": 0.0}
    values = frame[column]
    non_null = int(values.notna().sum())
    return {"column": column, "rows": int(len(frame)), "non_null_rows": non_null,
            "coverage": float(non_null / len(frame)) if len(frame) else 0.0}


def _market_count(frame: pd.DataFrame) -> dict[str, Any]:
    column = _first_column(frame, ("market", "market_id", "condition_id", "slug"))
    if column is None:
        return {"identity_column": None, "unique_markets": 0, "btc_5m_markets": 0}
    values = frame[column].astype(str)
    unique = set(values[values.ne("nan")])
    five_minute = sum("btc" in value.lower() and "5m" in value.lower() for value in unique)
    return {"identity_column": column, "unique_markets": len(unique), "btc_5m_markets": int(five_minute)}


def _lead_lag_probe(polymarket: pd.DataFrame, binance: pd.DataFrame) -> dict[str, Any]:
    """Count only causal timestamp candidates; do not infer a trade signal."""
    pm_ts_col = _first_column(polymarket, ("ts_local",))
    bn_ts_col = _first_column(binance, ("ts_local",))
    price_col = _first_column(binance, ("p", "price"))
    if pm_ts_col is None or bn_ts_col is None or price_col is None:
        return {"large_move_events": 0, "candidate_pairs": 0, "threshold_bps": 5.0,
                "window_ms": 500.0, "available": False}
    pm_ts = pd.to_numeric(polymarket[pm_ts_col], errors="coerce").dropna().sort_values().to_numpy()
    bn = binance[[bn_ts_col, price_col]].copy()
    bn[bn_ts_col] = pd.to_numeric(bn[bn_ts_col], errors="coerce")
    bn[price_col] = pd.to_numeric(bn[price_col], errors="coerce")
    bn = bn.dropna().sort_values(bn_ts_col, kind="stable")
    if len(bn) < 2 or len(pm_ts) == 0:
        return {"large_move_events": 0, "candidate_pairs": 0, "threshold_bps": 5.0,
                "window_ms": 500.0, "available": True}
    price = bn[price_col].to_numpy(dtype=float)
    previous = pd.Series(price).shift(1).to_numpy(dtype=float)
    move_bps = (price / previous - 1.0) * 10_000.0
    event_ts = bn[bn_ts_col].to_numpy(dtype=float)
    large = (pd.Series(move_bps).abs() >= 5.0).to_numpy()
    large_ts = event_ts[large & math.isfinite(float(event_ts[-1]))]
    if len(large_ts) == 0:
        return {"large_move_events": 0, "candidate_pairs": 0, "threshold_bps": 5.0,
                "window_ms": 500.0, "available": True}
    positions = pd.Series(pm_ts).searchsorted(large_ts, side="left")
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
    frames = {name: pd.read_parquet(path) for name, path in files.items()}
    pm, bn, cl = frames["polymarket"], frames["binance"], frames["chainlink"]
    fields = {}
    for name, frame in frames.items():
        fields[name] = {
            "columns": sorted(str(column) for column in frame.columns),
            "clock": _clock_contract(frame),
        }
    field_coverage = {
        name: {field: _coverage(frame, candidates) for field, candidates in PREDECLARED_FIELDS.items()}
        for name, frame in frames.items()
    }
    report = {
        "format": FORMAT,
        "sample_id": SAMPLE_ID,
        "source_scope": "one-hour public synchronized sample; source-contract audit only",
        "files": {name: {"rows": int(len(frame)), "sha256": _sha256(path)} for name, (frame, path) in
                  ((name, (frames[name], path)) for name, path in files.items())},
        "field_contract": fields,
        "field_coverage": field_coverage,
        "market_identity": _market_count(pm),
        "lead_lag_probe": _lead_lag_probe(pm, bn),
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

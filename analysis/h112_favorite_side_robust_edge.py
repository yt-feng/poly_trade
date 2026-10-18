"""Fail-closed schema gate for the H112 favorite-side robust-edge study.

This module does not collect data or fit a model.  It only inspects CSV headers
before any expensive replay.  Outcome/strike columns are deliberately not
accepted as a proxy for a causal spot or Chainlink process price.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable


QUOTE_FIELDS = frozenset({"ts_iso", "buy_up_cents", "buy_down_cents"})
DEPTH_FIELDS = frozenset({"buy_up_size", "buy_down_size", "ask_depth_up_5", "ask_depth_down_5"})
OUTCOME_ONLY_FIELDS = frozenset({"target_price", "final_price", "outcome_up"})
INDEPENDENT_PRICE_ALIASES = (
    frozenset({"spot_price"}),
    frozenset({"chainlink_price"}),
    frozenset({"btc_price"}),
)
TIME_ALIGNMENT_ALIASES = (
    frozenset({"window_start_ts", "window_end_ts"}),
    frozenset({"seconds_to_close"}),
    frozenset({"time_to_close"}),
)


def _headers_from_csv(path: Path) -> frozenset[str]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        row = next(csv.reader(handle), [])
    return frozenset(str(value).strip() for value in row if str(value).strip())


def discover_headers(source_root: Path) -> tuple[list[Path], list[frozenset[str]]]:
    paths = sorted(source_root.rglob("*_btc-updown-5m_quotes.csv"))
    if not paths:
        raise ValueError("NO_QUOTE_CSV_FILES")
    return paths, [_headers_from_csv(path) for path in paths]


def validate_headers(headers: Iterable[str]) -> dict[str, object]:
    fields = frozenset(str(value).strip() for value in headers if str(value).strip())
    independent_price = next((sorted(alias & fields) for alias in INDEPENDENT_PRICE_ALIASES if alias <= fields), [])
    time_alignment = next((sorted(alias & fields) for alias in TIME_ALIGNMENT_ALIASES if alias <= fields), [])
    missing = []
    if not QUOTE_FIELDS <= fields:
        missing.append("favorite_side_quote_fields")
    if not DEPTH_FIELDS & fields:
        missing.append("execution_depth_fields")
    if not independent_price:
        missing.append("independent_spot_or_chainlink_price")
    if not time_alignment:
        missing.append("causal_time_alignment")
    # These columns are labels/strikes, never independent fair-value inputs.
    outcome_only_present = sorted(OUTCOME_ONLY_FIELDS & fields)
    passed = not missing
    return {
        "passed": passed,
        "fields": sorted(fields),
        "missing": missing,
        "independent_price_fields": independent_price,
        "time_alignment_fields": time_alignment,
        "outcome_only_fields_present": outcome_only_present,
        "outcome_fields_rejected_as_fair_value": bool(outcome_only_present),
    }


def inspect_source(source_root: Path) -> dict[str, object]:
    paths, headers = discover_headers(source_root)
    signatures = {tuple(sorted(header)) for header in headers}
    # All files must be compatible before an H112 replay can be considered.
    merged = set().union(*headers)
    result = validate_headers(merged)
    result.update({
        "source_root": str(source_root),
        "file_count": len(paths),
        "header_signature_count": len(signatures),
        "schema_gate": "pass" if result["passed"] and len(signatures) == 1 else "reject",
    })
    if len(signatures) != 1:
        result["missing"].append("single_consistent_header_signature")
        result["passed"] = False
        result["schema_gate"] = "reject"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inspect_source(args.source_root)
    except Exception:
        print(json.dumps({"schema_gate": "reject", "reason": "SCHEMA_INSPECTION_FAILED"}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["schema_gate"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())

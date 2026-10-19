"""Fail-closed input contract for the pre-registered H319A experiment.

H319A studies calibration conditional on a fixed five-minute horizon and
time-of-day.  This module deliberately stops before model fitting or replay:
it verifies that every candidate row carries a causally available reference,
an explicit decision clock, and a label whose availability is after the
scheduled resolution.  It is safe to run on a public archive and does not
access accounts, submit orders, or infer missing settlement fields.
"""
from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Any, Iterable


_REQUIRED = (
    "market_id",
    "decision_ts_ms",
    "scheduled_end_ts_ms",
    "time_to_expiry_seconds",
    "ptb_received_ts_ms",
    "reference_event_ts_ms",
    "reference_received_ts_ms",
    "reference_source",
    "source_version",
    "market_probability",
    "outcome",
    "outcome_available_ts_ms",
    "block_id",
)


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and isfinite(float(value))


def validate_h319a_rows(
    rows: Iterable[dict[str, Any]],
    *,
    min_blocks: int = 5,
    min_rows_per_block: int = 10,
) -> dict[str, Any]:
    """Validate one pre-registered H319A input slice.

    The result is diagnostic only.  ``passed`` means the slice is eligible
    for a separate calibration run; it does not imply a profitable strategy,
    a fill, settlement, cash release, or Canary admission.
    """
    failures: list[str] = []
    materialized = list(rows)
    if not materialized:
        return {
            "passed": False,
            "row_count": 0,
            "block_counts": {},
            "failure_reasons": ["no_rows"],
        }
    if type(min_blocks) is not int or min_blocks <= 0:
        raise ValueError("min_blocks must be a positive integer")
    if type(min_rows_per_block) is not int or min_rows_per_block <= 0:
        raise ValueError("min_rows_per_block must be a positive integer")

    seen_markets: set[str] = set()
    previous_decision: int | None = None
    block_counts: Counter[str] = Counter()
    source_versions: set[str] = set()
    sources: set[str] = set()

    for index, row in enumerate(materialized):
        if not isinstance(row, dict):
            failures.append(f"row_{index}:not_an_object")
            continue
        missing = [field for field in _REQUIRED if field not in row]
        if missing:
            failures.append(f"row_{index}:missing:" + ",".join(missing))
            continue
        market_id = str(row["market_id"]).strip()
        if not market_id:
            failures.append(f"row_{index}:market_id_missing")
        elif market_id in seen_markets:
            failures.append(f"row_{index}:duplicate_market_id")
        seen_markets.add(market_id)

        clocks = [
            "decision_ts_ms",
            "scheduled_end_ts_ms",
            "ptb_received_ts_ms",
            "reference_event_ts_ms",
            "reference_received_ts_ms",
            "outcome_available_ts_ms",
        ]
        if not all(_integer(row[field]) for field in clocks):
            failures.append(f"row_{index}:invalid_causal_clock")
            continue
        decision = row["decision_ts_ms"]
        end = row["scheduled_end_ts_ms"]
        if decision >= end:
            failures.append(f"row_{index}:decision_not_before_end")
        expected_tte = (end - decision) / 1000.0
        if not _finite_number(row["time_to_expiry_seconds"]):
            failures.append(f"row_{index}:invalid_time_to_expiry")
        elif abs(float(row["time_to_expiry_seconds"]) - expected_tte) > 0.001:
            failures.append(f"row_{index}:time_to_expiry_mismatch")
        if row["ptb_received_ts_ms"] > decision:
            failures.append(f"row_{index}:ptb_received_after_decision")
        if row["reference_event_ts_ms"] > decision:
            failures.append(f"row_{index}:reference_event_after_decision")
        if row["reference_received_ts_ms"] > decision:
            failures.append(f"row_{index}:reference_received_after_decision")
        if row["outcome_available_ts_ms"] < end:
            failures.append(f"row_{index}:outcome_available_before_end")
        if previous_decision is not None and decision <= previous_decision:
            failures.append(f"row_{index}:decision_order_not_strict")
        previous_decision = decision

        if not isinstance(row["reference_source"], str) or not row["reference_source"].strip():
            failures.append(f"row_{index}:reference_source_missing")
        else:
            sources.add(row["reference_source"].strip())
        if not isinstance(row["source_version"], str) or not row["source_version"].strip():
            failures.append(f"row_{index}:source_version_missing")
        else:
            source_versions.add(row["source_version"].strip())
        if not _finite_number(row["market_probability"]) or not 0.0 <= float(row["market_probability"]) <= 1.0:
            failures.append(f"row_{index}:market_probability_out_of_range")
        outcome = row["outcome"]
        if outcome not in (0, 1, 0.0, 1.0):
            failures.append(f"row_{index}:outcome_not_binary")

        block = str(row["block_id"]).strip()
        if not block:
            failures.append(f"row_{index}:block_id_missing")
        else:
            block_counts[block] += 1

    if len(block_counts) < min_blocks:
        failures.append("insufficient_chronological_blocks")
    if any(count < min_rows_per_block for count in block_counts.values()):
        failures.append("block_sample_floor_not_met")
    if len(source_versions) > 1:
        failures.append("mixed_source_versions")
    if len(sources) > 1:
        failures.append("mixed_reference_sources")

    unique_failures = sorted(set(failures))
    return {
        "passed": not unique_failures,
        "row_count": len(materialized),
        "unique_market_count": len(seen_markets),
        "block_counts": dict(sorted(block_counts.items())),
        "source_versions": sorted(source_versions),
        "reference_sources": sorted(sources),
        "causal_clock_contract": not any(
            reason.endswith(("invalid_causal_clock", "decision_not_before_end", "time_to_expiry_mismatch"))
            or "_after_" in reason
            or reason.endswith("decision_order_not_strict")
            for reason in unique_failures
        ),
        "failure_reasons": unique_failures,
    }


"""Fail-closed intake contract for canonical v3 observations and labels.

The intake step is deliberately separate from model evaluation.  It verifies
file hashes, field-level v3/label contracts, source/receive ordering and label
availability before a replay is allowed to inspect the data.  It emits counts
and hashes only: no observations, outcomes or private paths are copied into a
public report, and it never computes OOS metrics or enables canary promotion.

Example::

    python analysis/walk_forward_intake.py \
      --observations /private/observations-v3.jsonl \
      --labels /private/resolution-labels.jsonl \
      --expected-observations-sha256 '<64 lowercase hex>' \
      --expected-labels-sha256 '<64 lowercase hex>' \
      --output /private/intake-report.json

Synthetic fixtures may pass ``--synthetic`` for regression tests.  The report
then records ``evidence_qualifies=false`` even when the fixture contract is
valid.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

try:
    from canary_evidence_ledger import REQUIREMENTS
    from v3_data_contract import load_jsonl, validate_records
    from walk_forward_execution import load_labels
except ImportError:  # pragma: no cover - package/import-mode convenience
    from analysis.canary_evidence_ledger import REQUIREMENTS
    from analysis.v3_data_contract import load_jsonl, validate_records
    from analysis.walk_forward_execution import load_labels


INTAKE_SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_report(path: Path, expected: str | None) -> dict[str, Any]:
    present = path.is_file()
    actual = sha256_file(path) if present else None
    expected_valid = expected is None or bool(SHA256_RE.fullmatch(expected))
    if expected is not None and not expected_valid:
        expected = None
    return {
        "provided_name": path.name,
        "present": present,
        "bytes": path.stat().st_size if present else None,
        "sha256": actual,
        "expected_sha256": expected,
        "sha256_match": (actual == expected) if expected is not None else None,
        "expected_sha256_valid": expected_valid,
    }


def _utc_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).date().isoformat()


def _time_order_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_market[record["market_id"]].append(record)
    regressions = []
    for market_id, rows in by_market.items():
        previous = None
        for index, row in enumerate(rows):
            current = (row["source_event_time_ms"], row["received_time_ms"])
            if previous is not None and current < previous:
                regressions.append({"market_id": market_id, "row_index": index})
            previous = current
    return {
        "market_count": len(by_market),
        "nondecreasing_by_market": not regressions,
        "regression_count": len(regressions),
        "regressions": regressions[:20],
    }


def _alignment_report(
    observations: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    observation_markets = {record["market_id"] for record in observations}
    label_markets = set(labels)
    missing = sorted(observation_markets - label_markets)
    orphan = sorted(label_markets - observation_markets)
    condition_mismatches = []
    label_known_before_observation = []
    observation_after_resolution = []
    for record in observations:
        label = labels.get(record["market_id"])
        if label is None:
            continue
        if label["condition_id"] != record["condition_id"]:
            condition_mismatches.append(record["market_id"])
        if record["received_time_ms"] >= label["label_available_time_ms"]:
            label_known_before_observation.append(record["observation_id"])
        if record["source_event_time_ms"] >= label["resolved_time_ms"]:
            observation_after_resolution.append(record["observation_id"])
    return {
        "observation_markets": len(observation_markets),
        "label_markets": len(label_markets),
        "missing_label_market_count": len(missing),
        "orphan_label_market_count": len(orphan),
        "condition_mismatch_count": len(set(condition_mismatches)),
        "label_known_before_or_at_observation_count": len(label_known_before_observation),
        "observation_at_or_after_resolution_count": len(observation_after_resolution),
        "missing_label_market_ids": missing[:20],
        "orphan_label_market_ids": orphan[:20],
        "condition_mismatch_market_ids": sorted(set(condition_mismatches))[:20],
        "label_known_before_or_at_observation_ids": label_known_before_observation[:20],
        "observation_at_or_after_resolution_ids": observation_after_resolution[:20],
    }


def _coverage_report(observations: list[dict[str, Any]]) -> dict[str, Any]:
    markets = {record["market_id"] for record in observations}
    dates = {_utc_date(record["source_event_time_ms"]) for record in observations}
    return {
        "accepted_observation_records": len(observations),
        "independent_windows": len(markets),
        "independent_utc_dates": len(dates),
        "utc_dates": sorted(dates),
        "source_event_time_ms": {
            "min": min((row["source_event_time_ms"] for row in observations), default=None),
            "max": max((row["source_event_time_ms"] for row in observations), default=None),
        },
    }


def build_report(
    observations_path: Path,
    labels_path: Path,
    *,
    expected_observations_sha256: str | None = None,
    expected_labels_sha256: str | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    """Build a public-safe intake report without evaluating a strategy."""
    observation_file = _file_report(observations_path, expected_observations_sha256)
    label_file = _file_report(labels_path, expected_labels_sha256)
    blockers: list[str] = []
    if not observation_file["present"]:
        blockers.append("missing_observation_file")
    if not label_file["present"]:
        blockers.append("missing_label_file")
    for name, file_report in (("observation", observation_file), ("label", label_file)):
        if not file_report["expected_sha256_valid"]:
            blockers.append(f"invalid_{name}_expected_sha256")
        elif file_report["sha256_match"] is False:
            blockers.append(f"{name}_file_sha256_mismatch")

    raw_observations = load_jsonl(observations_path) if observation_file["present"] else []
    validation = validate_records(raw_observations)
    accepted_records = list(validation["accepted"])
    observations = [record for record in accepted_records if record.get("observation_type") == "book_snapshot"]
    labels, label_validation = load_labels(labels_path) if label_file["present"] else ({}, {
        "input_records": 0,
        "accepted_records": 0,
        "quarantined_records": 0,
        "reason_counts": {"missing_label_file": 1},
    })
    if validation["quarantined_records"]:
        blockers.append("quarantined_observation_records")
    if label_validation["quarantined_records"]:
        blockers.append("quarantined_label_records")
    if not observations:
        blockers.append("no_validated_book_observations")
    if not labels:
        blockers.append("no_valid_resolution_labels")

    alignment = _alignment_report(observations, labels)
    if alignment["missing_label_market_count"]:
        blockers.append("missing_resolution_label_for_observation_market")
    if alignment["orphan_label_market_count"]:
        blockers.append("orphan_resolution_label_market")
    if alignment["condition_mismatch_count"]:
        blockers.append("label_condition_mismatch")
    if alignment["label_known_before_or_at_observation_count"]:
        blockers.append("label_available_before_or_at_observation")
    if alignment["observation_at_or_after_resolution_count"]:
        blockers.append("observation_at_or_after_resolution")

    time_order = _time_order_report(observations)
    if not time_order["nondecreasing_by_market"]:
        blockers.append("observation_time_not_monotonic_by_market")
    coverage = _coverage_report(observations)
    if coverage["independent_windows"] < REQUIREMENTS["independent_windows"]:
        blockers.append("fewer_than_300_independent_windows")
    if coverage["independent_utc_dates"] < REQUIREMENTS["independent_utc_dates"]:
        blockers.append("fewer_than_7_independent_utc_dates")
    if synthetic:
        blockers.append("synthetic_input_not_evidence")

    source_hashes = sorted({record["provenance"]["source_sha256"] for record in observations})
    report = {
        "schema_version": INTAKE_SCHEMA_VERSION,
        "intake": "walk_forward_canonical_input_v1",
        "status": "blocked" if blockers else "valid_public_replay_input",
        "canary_blocked": True,
        "canary_allowed": False,
        "evidence_qualifies": False,
        "synthetic_input": synthetic,
        "blocked_reasons": sorted(set(blockers)),
        "files": {"observations": observation_file, "labels": label_file},
        "observations": {
            "contract": {
                "schema": "research/strategy/schema/v3_observation.schema.json",
                "schema_version": 3,
                "observation_types": ["book_snapshot", "trade"],
                "forbidden_future_fields": [
                    "label", "outcome", "final_price", "target_price", "settlement", "pnl", "payoff",
                ],
            },
            "input_records": validation["input_records"],
            "accepted_records": validation["accepted_records"],
            "accepted_book_records": len(observations),
            "quarantined_records": validation["quarantined_records"],
            "reason_counts": validation["reason_counts"],
            "leakage_check": validation["leakage_check"],
            "source_sha256_unique_count": len(source_hashes),
            "source_sha256_values": source_hashes,
            "source_sha256_verification": "format_only_without_source_bytes",
        },
        "labels": {
            "contract": {
                "schema": "research/strategy/schema/walk_forward_label.schema.json",
                "schema_version": 1,
                "required_fields": [
                    "label_version", "market_id", "condition_id", "outcome",
                    "resolved_time_ms", "label_available_time_ms", "source", "source_sha256",
                ],
                "additional_properties": False,
            },
            **label_validation,
        },
        "alignment": alignment,
        "time_order": time_order,
        "coverage": coverage,
        "canary_requirements": {
            "independent_windows_min": REQUIREMENTS["independent_windows"],
            "independent_utc_dates_min": REQUIREMENTS["independent_utc_dates"],
            "execution_evidence_min": REQUIREMENTS["execution_evidence"],
            "private_execution_receipts_required": True,
            "public_replay_does_not_satisfy_canary": True,
        },
        "metrics": {"oos": None, "brier": None, "ece": None, "net_pnl_usdc": None},
    }
    return report


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--expected-observations-sha256")
    parser.add_argument("--expected-labels-sha256")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.observations,
            args.labels,
            expected_observations_sha256=args.expected_observations_sha256,
            expected_labels_sha256=args.expected_labels_sha256,
            synthetic=args.synthetic,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        report = {
            "schema_version": INTAKE_SCHEMA_VERSION,
            "intake": "walk_forward_canonical_input_v1",
            "status": "blocked",
            "canary_blocked": True,
            "canary_allowed": False,
            "evidence_qualifies": False,
            "synthetic_input": args.synthetic,
            "blocked_reasons": [f"input_error:{type(exc).__name__}"],
            "metrics": {"oos": None, "brier": None, "ece": None, "net_pnl_usdc": None},
        }
    write_report(report, args.output)
    print(json.dumps({"status": report["status"], "canary_blocked": True,
                      "blocked_reasons": report["blocked_reasons"]}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

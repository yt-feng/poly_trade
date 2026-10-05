"""Market-level walk-forward split, purge and embargo audit.

This is a structural audit that runs before model fitting.  It treats
``(market_id, condition_id)`` as the independent event cluster, keeps clusters
whole, and records train/validation/test boundaries together with label
availability cutoffs.  It produces no predictions, PnL or canary evidence.
Missing files, hash mismatches, identity conflicts, split overlap, labels known
before a feature, and clusters crossing purge/embargo boundaries fail closed.

Example::

    python analysis/walk_forward_split_audit.py \
      --observations /private/observations-v3.jsonl \
      --labels /private/resolution-labels.jsonl \
      --expected-observations-sha256 '<64 lowercase hex>' \
      --expected-labels-sha256 '<64 lowercase hex>' \
      --output /private/split-audit.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from canary_evidence_ledger import REQUIREMENTS
    from v3_data_contract import load_jsonl, validate_records
    from walk_forward_execution import load_labels
    from walk_forward_intake import _coverage_report, _file_report, _stable_digest
except ImportError:  # pragma: no cover - package/import-mode convenience
    from analysis.canary_evidence_ledger import REQUIREMENTS
    from analysis.v3_data_contract import load_jsonl, validate_records
    from analysis.walk_forward_execution import load_labels
    from analysis.walk_forward_intake import _coverage_report, _file_report, _stable_digest


AUDIT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SplitConfig:
    train_duration_ms: int = 3_600_000
    validation_duration_ms: int = 900_000
    test_duration_ms: int = 900_000
    purge_ms: int = 300_000
    embargo_ms: int = 1_000

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "SplitConfig":
        raw = raw or {}
        fields = (
            "train_duration_ms", "validation_duration_ms", "test_duration_ms",
            "purge_ms", "embargo_ms",
        )
        values = {}
        for field in fields:
            value = raw.get(field, getattr(cls, field))
            if type(value) is not int or value < 0:
                raise ValueError(f"invalid_config:{field}")
            values[field] = value
        if any(values[field] <= 0 for field in ("train_duration_ms", "validation_duration_ms", "test_duration_ms")):
            raise ValueError("invalid_config:nonpositive_split_duration")
        return cls(**values)


def _digest_ids(values: list[str]) -> str:
    return _stable_digest(values)


def _cluster_records(observations: list[dict[str, Any]], labels: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in observations:
        grouped[(record["market_id"], record["condition_id"])].append(record)
    clusters = []
    blockers: list[str] = []
    for (market_id, condition_id), rows in grouped.items():
        identities = {(row["market_id"], row["condition_id"], row["token_ids"]["up"], row["token_ids"]["down"]) for row in rows}
        if len(identities) != 1:
            blockers.append("inconsistent_cluster_identity")
        label = labels.get(market_id)
        if label is None:
            blockers.append("missing_label_for_cluster")
        elif label["condition_id"] != condition_id:
            blockers.append("label_condition_mismatch")
        clusters.append({
            "market_id": market_id,
            "condition_id": condition_id,
            "cluster_key": f"{market_id}\x00{condition_id}",
            "observation_count": len(rows),
            "start_event_ms": min(row["source_event_time_ms"] for row in rows),
            "end_event_ms": max(row["source_event_time_ms"] for row in rows),
            "first_received_ms": min(row["received_time_ms"] for row in rows),
            "last_received_ms": max(row["received_time_ms"] for row in rows),
            "label_available_time_ms": label.get("label_available_time_ms") if label else None,
            "resolved_time_ms": label.get("resolved_time_ms") if label else None,
        })
    clusters.sort(key=lambda item: (item["start_event_ms"], item["cluster_key"]))
    return clusters, sorted(set(blockers))


def _ids_digest(clusters: list[dict[str, Any]]) -> str:
    return _digest_ids([cluster["cluster_key"] for cluster in clusters])


def _interval_overlap(cluster: dict[str, Any], start_ms: int, end_ms: int) -> bool:
    return cluster["start_event_ms"] < end_ms and cluster["end_event_ms"] >= start_ms


def _cluster_counts(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "clusters": len(clusters),
        "observations": sum(cluster["observation_count"] for cluster in clusters),
        "cluster_ids_sha256": _ids_digest(clusters),
    }


def _split_one(
    clusters: list[dict[str, Any]],
    *,
    fold_number: int,
    anchor_start_ms: int,
    train_end_ms: int,
    config: SplitConfig,
) -> dict[str, Any]:
    purge_one_end = train_end_ms + config.purge_ms
    validation_start = purge_one_end
    validation_end = validation_start + config.validation_duration_ms
    purge_two_end = validation_end + config.purge_ms
    embargo_start = purge_two_end
    test_start = embargo_start + config.embargo_ms
    test_end = test_start + config.test_duration_ms
    train = [cluster for cluster in clusters if anchor_start_ms <= cluster["start_event_ms"] < train_end_ms]
    validation = [cluster for cluster in clusters if validation_start <= cluster["start_event_ms"] < validation_end]
    test = [cluster for cluster in clusters if test_start <= cluster["start_event_ms"] < test_end]
    purge_one = [cluster for cluster in clusters if _interval_overlap(cluster, train_end_ms, purge_one_end)]
    purge_two = [cluster for cluster in clusters if _interval_overlap(cluster, validation_end, purge_two_end)]
    embargo = [cluster for cluster in clusters if _interval_overlap(cluster, embargo_start, test_start)]
    all_sets = {"train": {c["cluster_key"] for c in train},
                "validation": {c["cluster_key"] for c in validation},
                "test": {c["cluster_key"] for c in test}}
    overlap_pairs = {
        f"{left}_{right}": sorted(all_sets[left] & all_sets[right])
        for left, right in (("train", "validation"), ("train", "test"), ("validation", "test"))
    }
    train_after_label_cutoff = [cluster for cluster in train
                                if cluster["label_available_time_ms"] is None
                                or cluster["label_available_time_ms"] > train_end_ms]
    validation_label_leak = [cluster for cluster in validation
                             if cluster["label_available_time_ms"] is None
                             or cluster["label_available_time_ms"] <= cluster["first_received_ms"]]
    test_label_leak = [cluster for cluster in test
                       if cluster["label_available_time_ms"] is None
                       or cluster["label_available_time_ms"] <= cluster["first_received_ms"]]
    clusters_crossing_windows = [cluster for cluster in clusters
                                 if any(
                                     cluster["start_event_ms"] < end and cluster["end_event_ms"] >= end
                                     for end in (train_end_ms, validation_end, test_start, test_end)
                                 )]
    blockers: list[str] = []
    if not train:
        blockers.append("empty_train_split")
    if not validation:
        blockers.append("empty_validation_split")
    if not test:
        blockers.append("empty_test_split")
    if any(overlap_pairs.values()):
        blockers.append("cluster_overlap_between_splits")
    if purge_one or purge_two or embargo:
        blockers.append("cluster_in_purge_or_embargo_interval")
    if clusters_crossing_windows:
        blockers.append("cluster_crosses_split_boundary")
    if train_after_label_cutoff:
        blockers.append("train_label_after_availability_cutoff")
    if validation_label_leak:
        blockers.append("validation_label_known_before_feature")
    if test_label_leak:
        blockers.append("test_label_known_before_feature")
    return {
        "fold": fold_number,
        "boundaries_ms": {
            "anchor_start": anchor_start_ms,
            "train_end": train_end_ms,
            "purge_1_start": train_end_ms,
            "purge_1_end": purge_one_end,
            "validation_start": validation_start,
            "validation_end": validation_end,
            "purge_2_start": validation_end,
            "purge_2_end": purge_two_end,
            "embargo_start": embargo_start,
            "embargo_end": test_start,
            "test_start": test_start,
            "test_end": test_end,
        },
        "label_availability_cutoffs_ms": {
            "train_must_be_at_or_before": train_end_ms,
            "validation_must_be_after_feature_receive": True,
            "test_must_be_after_feature_receive": True,
        },
        "counts": {
            "train": _cluster_counts(train),
            "validation": _cluster_counts(validation),
            "test": _cluster_counts(test),
            "purge_1": _cluster_counts(purge_one),
            "purge_2": _cluster_counts(purge_two),
            "embargo": _cluster_counts(embargo),
            "train_label_after_cutoff": len(train_after_label_cutoff),
            "validation_label_known_before_feature": len(validation_label_leak),
            "test_label_known_before_feature": len(test_label_leak),
            "cluster_crossing_split_boundary": len(clusters_crossing_windows),
        },
        "overlap": overlap_pairs,
        "blockers": sorted(set(blockers)),
    }


def audit_splits(observations: list[dict[str, Any]], labels: dict[str, dict[str, Any]], config: SplitConfig) -> dict[str, Any]:
    clusters, cluster_blockers = _cluster_records(observations, labels)
    if not clusters:
        return {"folds": [], "blockers": sorted(set(cluster_blockers + ["no_clusters"])), "cluster_count": 0}
    anchor_start = min(cluster["start_event_ms"] for cluster in clusters)
    latest = max(cluster["start_event_ms"] for cluster in clusters)
    folds = []
    train_end = anchor_start + config.train_duration_ms
    while train_end <= latest:
        fold = _split_one(clusters, fold_number=len(folds) + 1, anchor_start_ms=anchor_start,
                          train_end_ms=train_end, config=config)
        # Keep blocked folds in the audit so the missing boundary is reviewable.
        folds.append(fold)
        train_end = fold["boundaries_ms"]["test_end"]
    blockers = list(cluster_blockers)
    if not folds:
        blockers.append("no_complete_chronological_folds")
    for fold in folds:
        blockers.extend(fold["blockers"])
    return {
        "cluster_count": len(clusters),
        "cluster_ids_sha256": _ids_digest(clusters),
        "folds": folds,
        "blockers": sorted(set(blockers)),
    }


def _manifest_digest(report: dict[str, Any]) -> str:
    unsigned = dict(report)
    unsigned.pop("manifest_sha256", None)
    payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_report(
    observations_path: Path,
    labels_path: Path,
    *,
    config: SplitConfig | None = None,
    expected_observations_sha256: str | None = None,
    expected_labels_sha256: str | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    config = config or SplitConfig()
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
    raw = load_jsonl(observations_path) if observation_file["present"] else []
    validation = validate_records(raw)
    observations = [record for record in validation["accepted"] if record.get("observation_type") == "book_snapshot"]
    labels, label_report = load_labels(labels_path) if label_file["present"] else ({}, {
        "input_records": 0, "accepted_records": 0, "quarantined_records": 0,
        "reason_counts": {"missing_label_file": 1},
    })
    if validation["quarantined_records"]:
        blockers.append("quarantined_observation_records")
    if label_report["quarantined_records"]:
        blockers.append("quarantined_label_records")
    if not observations:
        blockers.append("no_validated_book_observations")
    if not labels:
        blockers.append("no_valid_resolution_labels")
    coverage = _coverage_report(observations)
    if coverage["inconsistent_market_identity_count"]:
        blockers.append("inconsistent_market_condition_or_token_identity")
    split = audit_splits(observations, labels, config) if observations and labels else {
        "cluster_count": 0, "cluster_ids_sha256": _stable_digest([]), "folds": [], "blockers": [],
    }
    blockers.extend(split["blockers"])
    if coverage["independent_windows"] < REQUIREMENTS["independent_windows"]:
        blockers.append("fewer_than_300_independent_windows")
    if coverage["independent_utc_dates"] < REQUIREMENTS["independent_utc_dates"]:
        blockers.append("fewer_than_7_independent_utc_dates")
    if synthetic:
        blockers.append("synthetic_input_not_evidence")
    report: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit": "market_level_walk_forward_split_v1",
        "status": "blocked" if blockers else "valid_public_split_audit",
        "canary_blocked": True,
        "canary_allowed": False,
        "evidence_qualifies": False,
        "synthetic_input": synthetic,
        "blocked_reasons": sorted(set(blockers)),
        "files": {"observations": observation_file, "labels": label_file},
        "contracts": {
            "observations_schema_version": 3,
            "labels_schema_version": 1,
            "cluster_key": ["market_id", "condition_id"],
        },
        "coverage": coverage,
        "labels": label_report,
        "observation_validation": {key: value for key, value in validation.items() if key != "accepted"},
        "config": {
            "train_duration_ms": config.train_duration_ms,
            "validation_duration_ms": config.validation_duration_ms,
            "test_duration_ms": config.test_duration_ms,
            "purge_ms": config.purge_ms,
            "embargo_ms": config.embargo_ms,
        },
        "split_audit": split,
        "metrics": {"oos": None, "brier": None, "ece": None, "net_pnl_usdc": None},
        "canary_requirements": {
            "independent_windows_min": REQUIREMENTS["independent_windows"],
            "independent_utc_dates_min": REQUIREMENTS["independent_utc_dates"],
            "execution_evidence_min": REQUIREMENTS["execution_evidence"],
            "private_execution_receipts_required": True,
        },
    }
    report["manifest_sha256"] = _manifest_digest(report)
    return report


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(report)
    report["manifest_sha256"] = _manifest_digest(report)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--expected-observations-sha256")
    parser.add_argument("--expected-labels-sha256")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw_config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
        report = build_report(
            args.observations,
            args.labels,
            config=SplitConfig.from_mapping(raw_config),
            expected_observations_sha256=args.expected_observations_sha256,
            expected_labels_sha256=args.expected_labels_sha256,
            synthetic=args.synthetic,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        report = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "audit": "market_level_walk_forward_split_v1",
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

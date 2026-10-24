#!/usr/bin/env python3
"""Aggregate-only coverage audit for an already-produced score bundle.

This module deliberately does not fit, predict, replay, or select orders.  It is
intended for a cloud runner to consume a saved ``scores.csv`` and the saved
candidate-gate JSONL from a prior frozen experiment.  It emits counts and
digests only; individual market identities never enter the report.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


SCORE_FIELDS = {
    "market_id",
    "run_name",
    "slug",
    "side",
    "decision_ts",
    "market_close_ts",
    "fit_run",
    "model_status",
    "predicted_net_margin_per_share",
    "quality_label_observed",
    "cash_domain_label_available_ts",
    "net_cash_difference_per_share",
}
CANDIDATE_FIELDS = {"market_id", "decision_expected_net_margin"}
THRESHOLDS = (0.0, 0.01, 0.02, 0.03, 0.05)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _finite(value: str, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric field: {field}") from exc
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric field: {field}")
    return result


def _timestamp(value: str, field: str) -> None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp field: {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone: {field}")


def _read_scores(path: Path) -> tuple[list[dict[str, str]], str]:
    digest = _sha256(path)
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or ())
        missing = sorted(SCORE_FIELDS - fields)
        if missing:
            raise ValueError(f"scores missing fields: {missing}")
        rows: list[dict[str, str]] = []
        for row in reader:
            for field in ("market_id", "fit_run", "model_status"):
                if not row.get(field):
                    raise ValueError(f"scores blank field: {field}")
            _finite(row["predicted_net_margin_per_share"], "predicted_net_margin_per_share")
            _timestamp(row["decision_ts"], "decision_ts")
            _timestamp(row["market_close_ts"], "market_close_ts")
            rows.append(row)
    if not rows:
        raise ValueError("scores are empty")
    return rows, digest


def _read_candidates(path: Path) -> tuple[list[dict[str, Any]], str]:
    digest = _sha256(path)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict) or not CANDIDATE_FIELDS.issubset(value):
                raise ValueError(f"candidate row {line_number} missing fields")
            value["decision_expected_net_margin"] = _finite(
                value["decision_expected_net_margin"], "decision_expected_net_margin"
            )
            rows.append(value)
    if not rows:
        raise ValueError("candidate rows are empty")
    return rows, digest


def build_report(
    scores: Iterable[dict[str, str]],
    candidates: Iterable[dict[str, Any]],
    *,
    score_sha256: str,
    candidate_sha256: str,
    edge_floor: float,
) -> dict[str, Any]:
    score_rows = list(scores)
    candidate_rows = list(candidates)
    margins = [_finite(row["predicted_net_margin_per_share"], "predicted_net_margin_per_share") for row in score_rows]
    score_market_ids = {row["market_id"] for row in score_rows}
    candidate_market_ids = {str(row["market_id"]) for row in candidate_rows}
    overlap = score_market_ids.intersection(candidate_market_ids)
    candidate_margins = [float(row["decision_expected_net_margin"]) for row in candidate_rows]
    threshold_counts = {
        f"ge_{threshold:g}": sum(value >= threshold for value in margins)
        for threshold in THRESHOLDS
    }
    return {
        "format": "offline-score-coverage-diagnostic-v1",
        "scope": "saved scores only; no fit, predict, replay or order admission",
        "score_file_sha256": score_sha256,
        "candidate_file_sha256": candidate_sha256,
        "score_rows": len(score_rows),
        "score_unique_market_ids": len(score_market_ids),
        "candidate_rows": len(candidate_rows),
        "candidate_unique_market_ids": len(candidate_market_ids),
        "candidate_score_market_overlap": len(overlap),
        "candidate_score_market_overlap_fraction": len(overlap) / len(candidate_market_ids),
        "score_threshold_counts": threshold_counts,
        "score_rows_at_or_above_frozen_edge_floor": sum(value >= edge_floor for value in margins),
        "score_unique_markets_at_or_above_frozen_edge_floor": len(
            {row["market_id"] for row in score_rows if _finite(row["predicted_net_margin_per_share"], "predicted_net_margin_per_share") >= edge_floor}
        ),
        "candidate_rows_at_or_above_frozen_edge_floor": sum(value >= edge_floor for value in candidate_margins),
        "candidate_max_expected_net_margin": max(candidate_margins),
        "candidate_min_expected_net_margin": min(candidate_margins),
        "edge_floor": edge_floor,
        "model_fit_calls": 0,
        "model_predict_calls": 0,
        "actual_fills": 0,
        "live_orders_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--edge-floor", type=float, default=0.02)
    args = parser.parse_args(argv)
    if not math.isfinite(args.edge_floor) or args.edge_floor < 0:
        raise SystemExit("edge floor must be finite and non-negative")
    scores, score_sha = _read_scores(args.scores)
    candidates, candidate_sha = _read_candidates(args.candidates)
    report = build_report(
        scores,
        candidates,
        score_sha256=score_sha,
        candidate_sha256=candidate_sha,
        edge_floor=args.edge_floor,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

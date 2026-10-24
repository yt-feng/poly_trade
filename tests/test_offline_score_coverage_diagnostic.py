from __future__ import annotations

import csv
import json
from pathlib import Path

from analysis.offline_score_coverage_diagnostic import main


SCORE_FIELDS = [
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
]


def _write_fixture(root: Path) -> tuple[Path, Path, Path]:
    scores = root / "scores.csv"
    candidates = root / "candidates.jsonl"
    output = root / "coverage.json"
    rows = [
        ["m1", "r1", "s1", "up", "2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00", "r1", "ok", "0.03", "1", "2026-01-01T00:01:00+00:00", "0.03"],
        ["m2", "r2", "s2", "down", "2026-01-01T00:05:00+00:00", "2026-01-01T00:10:00+00:00", "r2", "ok", "0.01", "1", "2026-01-01T00:06:00+00:00", "0.01"],
        ["m3", "r3", "s3", "up", "2026-01-01T00:10:00+00:00", "2026-01-01T00:15:00+00:00", "r3", "ok", "-0.01", "1", "2026-01-01T00:11:00+00:00", "-0.01"],
    ]
    with scores.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(SCORE_FIELDS)
        writer.writerows(rows)
    candidates.write_text(
        json.dumps({"market_id": "m1", "decision_expected_net_margin": 0.03}) + "\n",
        encoding="utf-8",
    )
    return scores, candidates, output


def test_aggregate_only_report_has_no_row_id_list(tmp_path: Path) -> None:
    scores, candidates, output = _write_fixture(tmp_path)
    assert main(["--scores", str(scores), "--candidates", str(candidates), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["score_rows"] == 3
    assert report["score_rows_at_or_above_frozen_edge_floor"] == 1
    assert report["score_unique_markets_at_or_above_frozen_edge_floor"] == 1
    assert report["candidate_rows_at_or_above_frozen_edge_floor"] == 1
    assert report["candidate_score_market_overlap"] == 1
    assert "market_ids" not in report
    assert report["model_fit_calls"] == 0
    assert report["model_predict_calls"] == 0


def test_invalid_timestamp_is_rejected(tmp_path: Path) -> None:
    scores, candidates, output = _write_fixture(tmp_path)
    text = scores.read_text(encoding="utf-8").replace("2026-01-01T00:00:00+00:00", "not-a-time", 1)
    scores.write_text(text, encoding="utf-8")
    try:
        main(["--scores", str(scores), "--candidates", str(candidates), "--output", str(output)])
    except ValueError as exc:
        assert "invalid timestamp" in str(exc)
    else:
        raise AssertionError("invalid timestamp was accepted")


def test_actual_candidate_gate_schema_and_nullable_predictions(tmp_path: Path) -> None:
    scores, candidates, output = _write_fixture(tmp_path)
    candidates.write_text(
        "\n".join(
            [
                json.dumps({
                    "candidate_index": 0,
                    "market_id": "m1",
                    "prediction_status": "available",
                    "predicted_net_margin_per_share": 0.03,
                    "passes_margin_veto": True,
                }),
                json.dumps({
                    "candidate_index": 1,
                    "market_id": "m2",
                    "prediction_status": "invalid_prediction",
                    "predicted_net_margin_per_share": None,
                    "passes_margin_veto": False,
                }),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert main(["--scores", str(scores), "--candidates", str(candidates), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["candidate_rows"] == 2
    assert report["candidate_rows_with_finite_margin"] == 1
    assert report["candidate_rows_at_or_above_frozen_edge_floor"] == 1
    assert report["candidate_max_expected_net_margin"] == 0.03

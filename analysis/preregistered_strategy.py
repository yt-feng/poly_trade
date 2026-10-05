"""Fail-closed pre-registration and multiple-testing audit.

The walk-forward evaluator can replay public data, but it must not silently
turn a parameter search into a post-hoc strategy selection.  This module is a
small, dependency-free gate around a pre-registered strategy manifest.  It
checks the immutable data/code identity, feature and label cutoffs, the
declared parameter grid, and the validation-only selection rule.  It does not
fit a model or calculate an OOS score.

The post-evaluation phase also requires an explicit PBO/CSCV result.  Until
that result exists, the report carries machine-readable cautions and remains
blocked.  A synthetic invocation is always non-evidence and contains no OOS
metrics.

Example (pre-evaluation):

    python analysis/preregistered_strategy.py \
      --manifest research/strategy/experiments/EXP-0002-btc5m-reference.json \
      --observations /private/observations-v3.jsonl \
      --labels /private/resolution-labels.jsonl \
      --code-commit '<40 lowercase hex>' \
      --evaluation-dates 2026-10-03,2026-10-04 \
      --phase pre_evaluation --output /private/preregistration.json
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import itertools
import json
from pathlib import Path
import re
from typing import Any, Iterable


SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
PHASES = {"pre_evaluation", "post_evaluation"}
EXPECTED_FEATURE_RULE = "received_time_ms < train_end_ms"
EXPECTED_LABEL_RULE = "label_available_time_ms <= train_end_ms"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def manifest_digest(manifest: dict[str, Any]) -> str:
    """Return the SHA-256 over the manifest body, excluding its self-digest."""
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _error(errors: list[str], reason: str) -> None:
    if reason not in errors:
        errors.append(reason)


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _grid_values(grid: dict[str, Any]) -> list[dict[str, Any]]:
    keys = sorted(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[key] for key in keys))]


def parameter_grid_digest(grid: dict[str, Any]) -> str:
    """Digest the canonical, sorted parameter grid rather than its formatting."""
    return hashlib.sha256(_canonical({key: grid[key] for key in sorted(grid)})).hexdigest()


def _parameter_registered(grid: dict[str, list[Any]], parameters: dict[str, Any]) -> bool:
    if not isinstance(grid, dict) or any(not isinstance(values, list) or not values for values in grid.values()):
        return False
    if set(parameters) != set(grid):
        return False
    return any(_canonical(candidate) == _canonical(parameters) for candidate in _grid_values(grid))


def validate_manifest(manifest: Any) -> list[str]:
    """Return deterministic structural errors; an empty list means valid."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest_not_object"]
    if manifest.get("schema_version") != SCHEMA_VERSION:
        _error(errors, "unsupported_manifest_schema")
    if manifest.get("manifest_type") != "pre_registered_strategy_hypothesis":
        _error(errors, "invalid_manifest_type")
    strategy_id = manifest.get("strategy_id")
    if not isinstance(strategy_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}", strategy_id):
        _error(errors, "invalid_strategy_id")
    if not isinstance(manifest.get("hypothesis"), str) or not manifest["hypothesis"].strip():
        _error(errors, "missing_hypothesis")
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        _error(errors, "missing_protocol")
    else:
        if protocol.get("pre_registered") is not True:
            _error(errors, "protocol_not_pre_registered")
        if protocol.get("public_quotes_are_not_fills") is not True:
            _error(errors, "protocol_public_quote_boundary_missing")
        if protocol.get("real_orders") is not False:
            _error(errors, "protocol_real_orders_must_be_false")

    cutoff = manifest.get("feature_cutoff")
    if not isinstance(cutoff, dict):
        _error(errors, "missing_feature_cutoff")
    else:
        if cutoff.get("feature_rule") != EXPECTED_FEATURE_RULE:
            _error(errors, "feature_cutoff_rule_mismatch")
        if cutoff.get("label_rule") != EXPECTED_LABEL_RULE:
            _error(errors, "label_cutoff_rule_mismatch")
        if cutoff.get("timezone") != "UTC":
            _error(errors, "feature_cutoff_timezone_mismatch")

    grid = manifest.get("parameter_grid")
    if not isinstance(grid, dict) or not grid:
        _error(errors, "missing_parameter_grid")
        grid = {}
    else:
        for key, values in grid.items():
            if not isinstance(key, str) or not key or not isinstance(values, list) or not values:
                _error(errors, "invalid_parameter_grid")
                continue
            canonical_values = [_canonical(value) for value in values]
            if len(set(canonical_values)) != len(canonical_values):
                _error(errors, "duplicate_parameter_grid_value")
        expected_count = 1
        for values in grid.values():
            if isinstance(values, list):
                expected_count *= len(values)
        if manifest.get("candidate_count") != expected_count:
            _error(errors, "parameter_candidate_count_mismatch")
        if manifest.get("parameter_grid_sha256") != parameter_grid_digest(grid):
            _error(errors, "parameter_grid_digest_mismatch")

    selection = manifest.get("selection_rule")
    if not isinstance(selection, dict):
        _error(errors, "missing_selection_rule")
    else:
        if selection.get("stage") != "validation_only":
            _error(errors, "selection_must_be_validation_only")
        if selection.get("uses_test_data") is not False:
            _error(errors, "selection_test_data_forbidden")
        if selection.get("fixed_before_evaluation") is not True:
            _error(errors, "selection_must_be_fixed_before_evaluation")
        if not isinstance(selection.get("metric"), str) or not selection["metric"].strip():
            _error(errors, "missing_selection_metric")
        if selection.get("direction") not in {"minimize", "maximize"}:
            _error(errors, "invalid_selection_direction")
        if not isinstance(selection.get("tie_break"), list) or not selection["tie_break"]:
            _error(errors, "missing_selection_tie_break")

    data = manifest.get("data")
    if not isinstance(data, dict):
        _error(errors, "missing_data_identity")
    else:
        for key in ("observations_sha256", "labels_sha256"):
            if not isinstance(data.get(key), str) or not SHA256_RE.fullmatch(data[key]):
                _error(errors, f"invalid_data_{key}")
        dates = data.get("evaluation_dates_utc")
        if not isinstance(dates, list) or not dates or any(not _valid_date(item) for item in dates):
            _error(errors, "invalid_evaluation_dates")
        elif dates != sorted(set(dates)):
            _error(errors, "evaluation_dates_not_sorted_unique")
        if data.get("timezone") != "UTC":
            _error(errors, "evaluation_date_timezone_mismatch")

    code = manifest.get("code")
    if not isinstance(code, dict):
        _error(errors, "missing_code_identity")
    else:
        if not isinstance(code.get("commit"), str) or not COMMIT_RE.fullmatch(code["commit"]):
            _error(errors, "invalid_code_commit")
        if not isinstance(code.get("entrypoint"), str) or not code["entrypoint"].strip():
            _error(errors, "missing_code_entrypoint")

    multiple = manifest.get("multiple_testing")
    if not isinstance(multiple, dict):
        _error(errors, "missing_multiple_testing_policy")
    else:
        if multiple.get("candidate_count") != manifest.get("candidate_count"):
            _error(errors, "multiple_testing_candidate_count_mismatch")
        if multiple.get("pbo_method") != "CSCV":
            _error(errors, "pbo_method_must_be_cscv")
        if multiple.get("pbo_required") is not True or multiple.get("cscv_required") is not True:
            _error(errors, "pbo_cscv_must_be_required")
        if multiple.get("status") != "not_computed_at_registration":
            _error(errors, "invalid_multiple_testing_registration_status")

    digest = manifest.get("manifest_sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        _error(errors, "missing_manifest_digest")
    elif digest != manifest_digest(manifest):
        _error(errors, "manifest_digest_mismatch")
    return sorted(errors)


def audit_manifest(
    manifest: dict[str, Any] | None,
    *,
    phase: str,
    observations_sha256: str | None = None,
    labels_sha256: str | None = None,
    code_commit: str | None = None,
    evaluation_dates_utc: Iterable[str] | None = None,
    parameters: dict[str, Any] | None = None,
    selection_stage: str | None = None,
    selection_metric: str | None = None,
    selection_metric_source: str | None = None,
    pbo_probability: float | None = None,
    cscv_result: dict[str, Any] | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    """Audit one evaluation context without evaluating a strategy."""
    blockers: list[str] = []
    cautions: list[str] = []
    if phase not in PHASES:
        _error(blockers, "invalid_phase")
    if manifest is None:
        _error(blockers, "missing_preregistration_manifest")
        errors = ["manifest_not_available"]
    else:
        errors = validate_manifest(manifest)
        blockers.extend(f"manifest:{error}" for error in errors)
    expected = manifest if isinstance(manifest, dict) else {}
    data = expected.get("data", {})
    code = expected.get("code", {})
    registered_dates = set(data.get("evaluation_dates_utc", []))
    supplied_dates = sorted(set(evaluation_dates_utc or []))
    if observations_sha256 is None:
        _error(blockers, "observation_data_hash_not_supplied")
    elif observations_sha256 != data.get("observations_sha256"):
        _error(blockers, "observation_data_hash_mismatch")
    if labels_sha256 is None:
        _error(blockers, "label_data_hash_not_supplied")
    elif labels_sha256 != data.get("labels_sha256"):
        _error(blockers, "label_data_hash_mismatch")
    if code_commit is None:
        _error(blockers, "code_commit_not_supplied")
    elif code_commit != code.get("commit"):
        _error(blockers, "code_commit_mismatch")
    if not supplied_dates:
        _error(blockers, "evaluation_dates_not_supplied")
    elif not set(supplied_dates).issubset(registered_dates):
        _error(blockers, "evaluation_date_outside_preregistration")
    if parameters is not None:
        grid = expected.get("parameter_grid", {})
        if not isinstance(parameters, dict) or not _parameter_registered(grid, parameters):
            _error(blockers, "parameter_set_not_registered")
    if phase == "post_evaluation":
        if parameters is None:
            _error(blockers, "post_evaluation_parameters_missing")
        if selection_stage != "validation_only_pre_registered":
            _error(blockers, "post_hoc_model_selection")
        registered_metric = expected.get("selection_rule", {}).get("metric")
        if selection_metric != registered_metric:
            _error(blockers, "selection_metric_mismatch")
        if selection_metric_source in {"test", "test_and_validation", "oos"}:
            _error(blockers, "test_data_used_for_selection")
        if pbo_probability is None:
            _error(blockers, "pbo_not_computed")
            _error(cautions, "pbo_not_computed")
        elif not isinstance(pbo_probability, (int, float)) or not 0 <= pbo_probability <= 1:
            _error(blockers, "invalid_pbo_probability")
        if cscv_result is None:
            _error(blockers, "cscv_not_computed")
            _error(cautions, "cscv_not_computed")
        elif not isinstance(cscv_result, dict) or cscv_result.get("method") != "CSCV":
            _error(blockers, "invalid_cscv_result")
    else:
        _error(cautions, "pbo_cscv_pending_until_post_evaluation")
    if synthetic:
        _error(blockers, "synthetic_input_not_evidence")
    return {
        "schema_version": SCHEMA_VERSION,
        "audit": "pre_registered_strategy_v1",
        "phase": phase,
        "status": "blocked" if blockers else "valid_preregistration_audit",
        "canary_blocked": True,
        "canary_allowed": False,
        "evidence_qualifies": False,
        "synthetic_input": synthetic,
        "strategy_id": expected.get("strategy_id"),
        "manifest_sha256": expected.get("manifest_sha256"),
        "manifest_errors": errors,
        "blockers": sorted(set(blockers)),
        "cautions": sorted(set(cautions)),
        "registered": {
            "feature_rule": expected.get("feature_cutoff", {}).get("feature_rule") if isinstance(expected.get("feature_cutoff"), dict) else None,
            "label_rule": expected.get("feature_cutoff", {}).get("label_rule") if isinstance(expected.get("feature_cutoff"), dict) else None,
            "candidate_count": expected.get("candidate_count"),
            "selection_rule": expected.get("selection_rule"),
            "evaluation_dates_utc": data.get("evaluation_dates_utc"),
            "code_commit": code.get("commit"),
        },
        "multiple_testing": {
            "pbo_probability": pbo_probability,
            "cscv_result": cscv_result,
            "selection_bias_control": "PBO/CSCV required before post-evaluation selection",
        },
        "metrics": {"oos": None, "brier": None, "ece": None, "net_pnl_usdc": None},
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--code-commit")
    parser.add_argument("--evaluation-dates", help="UTC dates separated by commas")
    parser.add_argument("--parameters", help="JSON object for one registered candidate")
    parser.add_argument("--selection-stage")
    parser.add_argument("--selection-metric")
    parser.add_argument("--selection-metric-source")
    parser.add_argument("--pbo-probability", type=float)
    parser.add_argument("--cscv-result", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = _load_json(args.manifest)
        observation_hash = file_sha256(args.observations) if args.observations else None
        label_hash = file_sha256(args.labels) if args.labels else None
        parameters = json.loads(args.parameters) if args.parameters else None
        cscv = _load_json(args.cscv_result) if args.cscv_result else None
        dates = [item for item in (args.evaluation_dates or "").split(",") if item]
        report = audit_manifest(
            manifest,
            phase=args.phase,
            observations_sha256=observation_hash,
            labels_sha256=label_hash,
            code_commit=args.code_commit,
            evaluation_dates_utc=dates,
            parameters=parameters,
            selection_stage=args.selection_stage,
            selection_metric=args.selection_metric,
            selection_metric_source=args.selection_metric_source,
            pbo_probability=args.pbo_probability,
            cscv_result=cscv,
            synthetic=args.synthetic,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report = audit_manifest(None, phase=args.phase, synthetic=args.synthetic)
        report["blockers"] = [f"input_error:{type(exc).__name__}"]
        report["status"] = "blocked"
    write_report(report, args.output)
    print(json.dumps({"status": report["status"], "blockers": report["blockers"], "metrics": report["metrics"]}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

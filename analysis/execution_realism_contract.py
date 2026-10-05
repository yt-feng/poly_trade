"""Pre-registered execution-realism stress contract.

This module defines the *shape* of a cost-aware replay.  It deliberately does
not simulate a strategy or publish performance.  The contract requires
displayed ask/bid depth, rejects midpoint fills, fixes a minimum-edge rule,
and enumerates every fee/slippage/latency/depth/partial-fill/TTL cell before
an evaluation can be considered complete.

Synthetic audits expand the cells with null metrics only.  A real run must
provide one output record per immutable cell and every required output field;
missing cells, costs, edge thresholds or output fields fail closed.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from typing import Any, Iterable


CONTRACT_SCHEMA_VERSION = 1
REQUIRED_DIMENSIONS = (
    "fee_rate_multiplier",
    "slippage_ticks",
    "latency_ms",
    "depth_fraction",
    "partial_fill_policy",
    "ttl_ms",
)
OUTPUT_FIELDS = (
    "gross_pnl_usdc",
    "fee_usdc",
    "net_pnl_usdc",
    "fill_rate",
    "brier",
    "ece",
)
PARTIAL_FILL_POLICIES = {"allow_partial_cancel", "require_full_or_cancel"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def matrix_digest(matrix: dict[str, list[Any]]) -> str:
    """Digest the sorted stress dimensions and their ordered values."""
    normalized = {key: matrix[key] for key in sorted(matrix)}
    return hashlib.sha256(_canonical(normalized)).hexdigest()


def _nonempty_unique(values: Any) -> bool:
    if not isinstance(values, list) or not values:
        return False
    try:
        encoded = [_canonical(value) for value in values]
    except (TypeError, ValueError):
        return False
    return len(set(encoded)) == len(encoded)


def validate_execution_contract(contract: Any) -> list[str]:
    """Return deterministic contract errors; an empty list means complete."""
    errors: list[str] = []
    if not isinstance(contract, dict):
        return ["contract_not_object"]
    if contract.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        errors.append("unsupported_contract_schema")
    if contract.get("price_source") != "displayed_ask_bid":
        errors.append("price_source_must_be_displayed_ask_bid")
    if contract.get("midpoint_allowed") is not False:
        errors.append("midpoint_execution_forbidden")
    required_fields = contract.get("required_execution_fields")
    required_field_set = set(required_fields) if isinstance(required_fields, list) else set()
    for field in ("books.<token>.asks", "books.<token>.bids", "rules.tick_size", "rules.min_order_size", "fees.rate", "fees.exponent"):
        if field not in required_field_set:
            errors.append("missing_execution_field:" + field)

    edge = contract.get("minimum_edge")
    if not isinstance(edge, dict):
        errors.append("missing_minimum_edge_gate")
    else:
        values = edge.get("values")
        if not _nonempty_unique(values):
            errors.append("missing_minimum_edge_values")
        else:
            for value in values:
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    errors.append("invalid_minimum_edge_value")
                    continue
                if numeric < 0 or numeric > 1 or not math.isfinite(numeric):
                    errors.append("invalid_minimum_edge_value")
        if edge.get("field") != "edge_buffer":
            errors.append("minimum_edge_field_mismatch")
        if edge.get("rule") != "predicted_edge > minimum_edge":
            errors.append("minimum_edge_rule_mismatch")
        if edge.get("unit") != "probability":
            errors.append("minimum_edge_unit_mismatch")

    matrix = contract.get("stress_matrix")
    if not isinstance(matrix, dict):
        errors.append("incomplete_stress_sensitivity")
        matrix = {}
    if set(matrix) != set(REQUIRED_DIMENSIONS):
        errors.append("incomplete_stress_sensitivity")
    for dimension in REQUIRED_DIMENSIONS:
        values = matrix.get(dimension)
        if not _nonempty_unique(values):
            errors.append("missing_stress_dimension:" + dimension)
            continue
        if dimension in {"fee_rate_multiplier", "depth_fraction"}:
            for value in values:
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    errors.append("invalid_stress_value:" + dimension)
                    continue
                if not math.isfinite(numeric) or numeric <= 0 or (dimension == "depth_fraction" and numeric > 1):
                    errors.append("invalid_stress_value:" + dimension)
        elif dimension in {"latency_ms", "slippage_ticks"}:
            if any(type(value) is not int or value < 0 for value in values):
                errors.append("invalid_stress_value:" + dimension)
        elif dimension == "ttl_ms":
            if any(type(value) is not int or value <= 0 for value in values):
                errors.append("invalid_stress_value:" + dimension)
        elif dimension == "partial_fill_policy":
            if any(value not in PARTIAL_FILL_POLICIES for value in values):
                errors.append("invalid_stress_value:" + dimension)
    if isinstance(matrix, dict) and set(matrix) == set(REQUIRED_DIMENSIONS) and all(_nonempty_unique(matrix.get(key)) for key in REQUIRED_DIMENSIONS):
        expected_cells = 1
        for dimension in REQUIRED_DIMENSIONS:
            expected_cells *= len(matrix[dimension])
        if contract.get("cell_count") != expected_cells:
            errors.append("stress_cell_count_mismatch")
        if contract.get("matrix_sha256") != matrix_digest(matrix):
            errors.append("stress_matrix_digest_mismatch")

    outputs = contract.get("outputs")
    if not isinstance(outputs, dict):
        errors.append("missing_stress_output_schema")
    else:
        fields = outputs.get("per_cell")
        if fields != list(OUTPUT_FIELDS):
            errors.append("stress_output_schema_mismatch")
        if outputs.get("null_on_synthetic") is not True:
            errors.append("synthetic_metrics_must_be_null")
    return sorted(set(errors))


def expand_stress_cells(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a valid matrix into stable cell IDs and null output slots."""
    matrix = contract.get("stress_matrix", {})
    if validate_execution_contract(contract):
        return []
    cells = []
    for values in itertools.product(*(matrix[dimension] for dimension in REQUIRED_DIMENSIONS)):
        stress = dict(zip(REQUIRED_DIMENSIONS, values))
        cell_id = hashlib.sha256(_canonical(stress)).hexdigest()
        cells.append({
            "cell_id": cell_id,
            "stress": stress,
            "metrics": {field: None for field in OUTPUT_FIELDS},
        })
    return cells


def _cell_matches(contract: dict[str, Any], selected: dict[str, Any]) -> bool:
    if not isinstance(selected, dict):
        return False
    cells = expand_stress_cells(contract)
    for cell in cells:
        if selected.get("cell_id") == cell["cell_id"] and selected.get("stress") == cell["stress"]:
            return True
    return False


def audit_execution_contract(
    contract: dict[str, Any] | None,
    *,
    selected_cell: dict[str, Any] | None = None,
    covered_cell_ids: Iterable[str] | None = None,
    observed_outputs: list[dict[str, Any]] | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    """Build a public-safe structural report; no PnL is computed here."""
    blockers: list[str] = []
    cautions: list[str] = []
    if contract is None:
        blockers.append("missing_execution_realism_contract")
        errors = ["contract_not_available"]
        cells: list[dict[str, Any]] = []
    else:
        errors = validate_execution_contract(contract)
        blockers.extend("contract:" + error for error in errors)
        cells = expand_stress_cells(contract) if not errors else []
    expected_ids = {cell["cell_id"] for cell in cells}
    if selected_cell is not None and not _cell_matches(contract or {}, selected_cell):
        blockers.append("stress_cell_not_registered")
    if covered_cell_ids is not None:
        covered_values = list(covered_cell_ids)
        covered = set(covered_values)
        if len(covered_values) != len(covered):
            blockers.append("duplicate_stress_cell")
        unknown = covered - expected_ids
        missing = expected_ids - covered
        if unknown:
            blockers.append("unknown_stress_cell")
        if missing:
            blockers.append("incomplete_stress_sensitivity")
    if observed_outputs is not None:
        observed_ids = set()
        for output in observed_outputs:
            if not isinstance(output, dict) or not isinstance(output.get("cell_id"), str):
                blockers.append("invalid_stress_output_record")
                continue
            observed_ids.add(output["cell_id"])
            if output["cell_id"] not in expected_ids:
                blockers.append("unknown_stress_cell")
            for field in OUTPUT_FIELDS:
                if field not in output.get("metrics", {}):
                    blockers.append("missing_stress_output_field:" + field)
                    continue
                value = output["metrics"][field]
                if value is None:
                    if not synthetic:
                        blockers.append("null_stress_metric:" + field)
                    continue
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                    blockers.append("invalid_stress_metric:" + field)
                    continue
                if field == "fill_rate" and not 0 <= float(value) <= 1:
                    blockers.append("invalid_stress_metric:fill_rate")
                if field in {"fee_usdc", "brier", "ece"} and float(value) < 0:
                    blockers.append("invalid_stress_metric:" + field)
        if len(observed_ids) != len(observed_outputs):
            blockers.append("duplicate_stress_cell")
        if expected_ids - observed_ids:
            blockers.append("incomplete_stress_sensitivity")
    else:
        cautions.append("stress_outputs_not_observed")
    if synthetic:
        blockers.append("synthetic_input_not_evidence")
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "audit": "execution_realism_stress_v1",
        "status": "blocked" if blockers else "valid_execution_realism_contract",
        "canary_blocked": True,
        "canary_allowed": False,
        "evidence_qualifies": False,
        "synthetic_input": synthetic,
        "blockers": sorted(set(blockers)),
        "cautions": sorted(set(cautions)),
        "contract_errors": errors,
        "price_execution": {
            "source": contract.get("price_source") if isinstance(contract, dict) else None,
            "midpoint_allowed": contract.get("midpoint_allowed") if isinstance(contract, dict) else False,
            "required_depth": ["asks", "bids"],
        },
        "minimum_edge_gate": contract.get("minimum_edge") if isinstance(contract, dict) else None,
        "stress_matrix": {
            "dimensions": contract.get("stress_matrix") if isinstance(contract, dict) else None,
            "cell_count": len(cells),
            "matrix_sha256": contract.get("matrix_sha256") if isinstance(contract, dict) else None,
        },
        "output_schema": {
            "per_cell": list(OUTPUT_FIELDS),
            "null_on_synthetic": True,
        },
        "cells": cells,
        "metrics": {"oos": None, "brier": None, "ece": None, "gross_pnl_usdc": None, "fee_usdc": None, "net_pnl_usdc": None, "fill_rate": None},
    }

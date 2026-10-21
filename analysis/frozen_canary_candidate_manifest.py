"""Freeze the best old-clean paper candidate and its Canary admission contract.

This module is deliberately a static contract.  It does not read market data,
fit a model, call a provider, place an order, or count a paper fill as a
Canary.  Future owner-scoped receipts may be checked against this manifest,
but the manifest itself can never open the live gate.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

try:
    from .h432_old_clean_extratrees_compound import (
        EDGE_THRESHOLD,
        FEATURES,
        INITIAL_CASH,
        MAX_OVERROUND,
        MAX_SPREAD,
        MIN_DEPTH,
        MINIMUM_ORDER_SHARES,
        MINIMUM_ORDER_USD,
        MODEL_PARAMS,
        PRICE_MAX,
        PRICE_MIN,
        REQUIRED_FILLS,
        SLIPPAGES,
        TAKER_FEE_RATE,
        CASH_RELEASE_SECONDS,
        ENTRY_SECONDS_AFTER_START,
        SOURCE_COMMIT,
    )
except ImportError:  # pragma: no cover - direct script execution
    from h432_old_clean_extratrees_compound import (
        EDGE_THRESHOLD,
        FEATURES,
        INITIAL_CASH,
        MAX_OVERROUND,
        MAX_SPREAD,
        MIN_DEPTH,
        MINIMUM_ORDER_SHARES,
        MINIMUM_ORDER_USD,
        MODEL_PARAMS,
        PRICE_MAX,
        PRICE_MIN,
        REQUIRED_FILLS,
        SLIPPAGES,
        TAKER_FEE_RATE,
        CASH_RELEASE_SECONDS,
        ENTRY_SECONDS_AFTER_START,
        SOURCE_COMMIT,
    )


MANIFEST_FORMAT = "frozen-canary-candidate-manifest-v1"
HYPOTHESIS_ID = "H498_FROZEN_H447_H434_CANARY_CANDIDATE"
EXPECTED_SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
EXPECTED_MODEL_PARAMS = {
    "n_estimators": 120,
    "max_depth": 7,
    "min_samples_leaf": 25,
    "max_features": 0.7,
    "class_weight": "balanced_subsample",
    "random_state": 4121,
    "n_jobs": -1,
}


def build_manifest() -> dict[str, Any]:
    """Return the immutable candidate/protocol description used for audit."""
    return {
        "format": MANIFEST_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "candidate": {
            "id": "H447_H434_CAUSAL_BREAKEVEN_EXIT",
            "signal": "H434 prequential ExtraTrees with fixed q_side >= 0.80",
            "exit": (
                "first later displayed sell-side bid with full displayed size and "
                "net proceeds covering entry cost plus both taker fees after the "
                "same slippage; otherwise hold to resolution"
            ),
            "early_exit_cash_release_acceleration": False,
            "confidence_floor": 0.80,
            "source_commit": SOURCE_COMMIT,
            "strict_quality_old_clean_only": True,
            "features": list(FEATURES),
            "model": {
                "kind": "ExtraTreesClassifier",
                "params": deepcopy(MODEL_PARAMS),
                "training_runs_strictly_before_current_run": True,
                "refit_in_this_manifest": False,
            },
            "fixed_slippages": [float(value) for value in SLIPPAGES],
            "selection_grid": False,
        },
        "selection_rule": {
            "edge_threshold": float(EDGE_THRESHOLD),
            "price_min": float(PRICE_MIN),
            "price_max": float(PRICE_MAX),
            "min_depth": float(MIN_DEPTH),
            "max_spread": float(MAX_SPREAD),
            "max_overround": float(MAX_OVERROUND),
            "entry_seconds_after_start": float(ENTRY_SECONDS_AFTER_START),
        },
        "execution_contract": {
            "initial_cash_usd": float(INITIAL_CASH),
            "minimum_order_usd": float(MINIMUM_ORDER_USD),
            "minimum_order_shares": float(MINIMUM_ORDER_SHARES),
            "taker_fee_rate": float(TAKER_FEE_RATE),
            "cash_release_delay_seconds": float(CASH_RELEASE_SECONDS),
            "single_position_only": True,
            "prior_cash_release_required": True,
            "same_ledger": True,
            "no_reset": True,
            "no_concatenation": True,
            "required_consecutive_fills": int(REQUIRED_FILLS),
            "paper_fill_count": 0,
        },
        "canary_admission": {
            "requires_h497_v2_path_adapter": True,
            "required_exchange_version": "ctf-exchange-v2",
            "required_match_paths": ["COMPLEMENTARY", "MINT", "MERGE"],
            "requires_owner_scoped_order_filled": True,
            "requires_confirmed_settlement_receipt": True,
            "requires_payout_or_redeem_receipt": True,
            "requires_cash_token_balance_before_after": True,
            "requires_reusable_cash_before_next_entry": True,
            "requires_chronological_sequence": True,
            "live_orders_enabled": False,
            "gate_passed": False,
        },
        "paper_evidence": {
            "primary_protocol": "H447_H434_CAUSAL_BREAKEVEN_EXIT",
            "cash_fraction_confirmation": "H453_H447_UPPER_CASH_FRACTION",
            "evidence_boundary": (
                "strict-quality old-clean quote replay only; displayed quotes do "
                "not prove queue position, owner fill, settlement, payout, or "
                "reusable cash"
            ),
            "paper_only": True,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
        },
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate the freeze and fail closed on any promotion or parameter drift."""
    expected = build_manifest()
    failures: list[str] = []

    if manifest.get("format") != MANIFEST_FORMAT:
        failures.append("format")
    if manifest.get("hypothesis_id") != HYPOTHESIS_ID:
        failures.append("hypothesis_id")
    if manifest.get("candidate", {}).get("source_commit") != EXPECTED_SOURCE_COMMIT:
        failures.append("source_commit")
    if manifest.get("candidate", {}).get("model", {}).get("params") != EXPECTED_MODEL_PARAMS:
        failures.append("model_params")
    if manifest.get("candidate", {}).get("confidence_floor") != 0.80:
        failures.append("confidence_floor")
    if manifest.get("candidate", {}).get("fixed_slippages") != [0.005, 0.01]:
        failures.append("slippages")

    contract = manifest.get("execution_contract", {})
    for key, value in expected["execution_contract"].items():
        if contract.get(key) != value:
            failures.append(f"execution_contract.{key}")
    admission = manifest.get("canary_admission", {})
    for key, value in expected["canary_admission"].items():
        if admission.get(key) != value:
            failures.append(f"canary_admission.{key}")
    for key in ("paper_only", "actual_fills", "verified_canary_fills", "same_ledger_compounding_rows"):
        if manifest.get("paper_evidence", {}).get(key) != expected["paper_evidence"][key]:
            failures.append(f"paper_evidence.{key}")
    for key in ("new_market_data_collected", "model_refit", "live_orders_enabled"):
        if manifest.get(key) != expected[key]:
            failures.append(key)

    return {
        "format": "frozen-canary-candidate-manifest-validation-v1",
        "manifest_format": manifest.get("format"),
        "all_checks_passed": not failures,
        "failures": failures,
        "paper_only": manifest.get("paper_evidence", {}).get("paper_only") is True,
        "gate_passed": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
    }


def audit() -> dict[str, Any]:
    manifest = build_manifest()
    result = validate_manifest(manifest)
    checks = {
        "manifest_matches_frozen_source": manifest["candidate"]["source_commit"] == EXPECTED_SOURCE_COMMIT,
        "same_ledger_is_explicit": manifest["execution_contract"]["same_ledger"] is True,
        "no_reset_or_concatenation": manifest["execution_contract"]["no_reset"] is True and manifest["execution_contract"]["no_concatenation"] is True,
        "ten_means_consecutive": manifest["execution_contract"]["required_consecutive_fills"] == 10,
        "release_required_before_next_entry": manifest["execution_contract"]["prior_cash_release_required"] is True,
        "h497_receipt_gate_required": manifest["canary_admission"]["requires_h497_v2_path_adapter"] is True,
        "paper_never_counts_as_canary": result["gate_passed"] is False and result["verified_canary_fills"] == 0,
        "no_live_action": manifest["live_orders_enabled"] is False,
        "validator_accepts_unchanged_manifest": result["all_checks_passed"] is True,
    }
    return {
        "format": "h498-frozen-canary-candidate-manifest-audit-v1",
        "hypothesis_id": HYPOTHESIS_ID,
        "manifest": manifest,
        "validation": result,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "decision": "freeze_candidate_wait_for_owner_scoped_v2_receipts",
        "root_cause": "The old-clean replay can prove a paper prefix but cannot prove ten chronological owner fills, settlement, and reusable cash; the manifest makes that boundary executable and prevents parameter drift.",
    }


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    if not report["all_checks_passed"]:
        raise SystemExit("h498_manifest_audit_failed")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": report["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))

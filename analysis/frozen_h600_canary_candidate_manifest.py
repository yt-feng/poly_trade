"""Freeze the H600 paper route without opening live admission.

H600 is the current robust old-clean paper route: the frozen H434 signal is
combined with the selected-side favorite ask floor and the H578 3/5/7 depth
map.  This module records the exact strategy identity and its ten-row
compounding contract.  It performs no replay, provider call, order, or live
gate transition.
"""
from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path
from typing import Any

_ANALYSIS_DIR = str(Path(__file__).resolve().parent)
if _ANALYSIS_DIR not in sys.path:
    sys.path.insert(0, _ANALYSIS_DIR)

try:
    from .validate_polybolt_compound_bundle import (
        H600_STRATEGY_CONFIG,
        H600_STRATEGY_CONFIG_SHA256,
        H600_STRATEGY_ID,
        H600_STRATEGY_PROFILE,
        H600_STRATEGY_VERSION,
    )
except ImportError:  # pragma: no cover - direct workflow invocation
    from validate_polybolt_compound_bundle import (
        H600_STRATEGY_CONFIG,
        H600_STRATEGY_CONFIG_SHA256,
        H600_STRATEGY_ID,
        H600_STRATEGY_PROFILE,
        H600_STRATEGY_VERSION,
    )


MANIFEST_FORMAT = "frozen-h600-canary-candidate-manifest-v1"
HYPOTHESIS_ID = "H600_FAVORITE_SIDE_ALIGNMENT_H578_DEPTH"
REQUIRED_FILLS = 10
INITIAL_CASH_USD = 10.0
MINIMUM_ORDER_USD = 5.0
MINIMUM_ORDER_SHARES = 5.0
TAKER_FEE_RATE = 0.07
CASH_RELEASE_SECONDS = 900.0
SETTLEMENT_SECONDS = 300.0
FAVORITE_PRICE_FLOOR = 0.50
DEPTH_THRESHOLDS = (3.0, 5.0, 7.0)
EXECUTION_ROLE = "TAKER"
TRADER_SIDE_SOURCE = "SETTLED_TAKER_LEG"
BUNDLE_SCHEMA = "authenticated-polybolt-compound-bundle-v2"
SOURCE_COMMIT = str(H600_STRATEGY_CONFIG["source_commit"])


def build_manifest() -> dict[str, Any]:
    """Return the immutable H600 candidate and its closed admission gate."""
    return {
        "format": MANIFEST_FORMAT,
        "hypothesis_id": HYPOTHESIS_ID,
        "candidate": {
            "id": H600_STRATEGY_ID,
            "version": H600_STRATEGY_VERSION,
            "config_sha256": H600_STRATEGY_CONFIG_SHA256,
            "source_commit": SOURCE_COMMIT,
            "signal": "frozen H434 q_side >= 0.80 prequential ExtraTrees",
            "favorite_side_rule": "selected-side two-minute ask >= 0.50",
            "depth_rule": "H578 side-specific sell-depth capacity thresholds 3/5/7",
            "strict_quality_old_clean_only": True,
            "refit_in_this_manifest": False,
            "strategy_config": deepcopy(H600_STRATEGY_CONFIG),
        },
        "execution_contract": {
            "initial_cash_usd": INITIAL_CASH_USD,
            "minimum_order_usd": MINIMUM_ORDER_USD,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE,
            "settlement_seconds": SETTLEMENT_SECONDS,
            "cash_release_seconds": CASH_RELEASE_SECONDS,
            "single_position_only": True,
            "prior_cash_release_required": True,
            "same_ledger": True,
            "no_reset": True,
            "no_concatenation": True,
            "required_consecutive_fills": REQUIRED_FILLS,
            "paper_fill_count": 0,
        },
        "canary_admission": {
            "admission_route": "auto_trace_bound",
            "required_strategy_profile": H600_STRATEGY_PROFILE,
            "required_strategy_identity": {
                "id": H600_STRATEGY_ID,
                "version": H600_STRATEGY_VERSION,
                "config_sha256": H600_STRATEGY_CONFIG_SHA256,
            },
            "required_bundle_schema": BUNDLE_SCHEMA,
            "required_execution_role": EXECUTION_ROLE,
            "required_trader_side_source": TRADER_SIDE_SOURCE,
            "requires_owner_scoped_order_filled": True,
            "requires_confirmed_settlement_receipt": True,
            "requires_cash_release_receipt": True,
            "requires_independent_balance_checkpoint": True,
            "requires_reusable_cash_before_next_entry": True,
            "requires_chronological_sequence": True,
            "live_orders_enabled": False,
            "gate_passed": False,
        },
        "paper_evidence": {
            "paper_only": True,
            "stress_gate": "192/192 selection and 216/216 holdout rows passed H600 paper stress",
            "favorite_price_floor": FAVORITE_PRICE_FLOOR,
            "depth_thresholds": list(DEPTH_THRESHOLDS),
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
        },
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on any strategy or compounding-contract drift."""
    expected = build_manifest()
    failures: list[str] = []
    if not isinstance(manifest, dict):
        failures.append("manifest_object")
        manifest = {}
    for key in ("format", "hypothesis_id", "new_market_data_collected", "model_refit", "live_orders_enabled"):
        if manifest.get(key) != expected[key]:
            failures.append(key)
    for key in ("candidate", "execution_contract", "canary_admission", "paper_evidence"):
        observed = manifest.get(key)
        if not isinstance(observed, dict):
            failures.append(key)
            continue
        for nested_key, value in expected[key].items():
            if observed.get(nested_key) != value:
                failures.append(f"{key}.{nested_key}")
    return {
        "format": "frozen-h600-canary-candidate-manifest-validation-v1",
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
    validation = validate_manifest(manifest)
    candidate = manifest["candidate"]
    contract = manifest["execution_contract"]
    admission = manifest["canary_admission"]
    checks = {
        "strategy_identity_matches_validator": (
            candidate["id"], candidate["version"], candidate["config_sha256"]
        ) == (H600_STRATEGY_ID, H600_STRATEGY_VERSION, H600_STRATEGY_CONFIG_SHA256),
        "source_commit_is_frozen": candidate["source_commit"] == SOURCE_COMMIT,
        "ten_means_consecutive": contract["required_consecutive_fills"] == REQUIRED_FILLS,
        "same_ledger_is_explicit": contract["same_ledger"] is True,
        "no_reset_or_concatenation": contract["no_reset"] is True and contract["no_concatenation"] is True,
        "release_required_before_next_entry": contract["prior_cash_release_required"] is True,
        "auto_route_is_exact": admission["admission_route"] == "auto_trace_bound",
        "paper_never_counts_as_canary": validation["gate_passed"] is False and validation["verified_canary_fills"] == 0,
        "no_live_action": manifest["live_orders_enabled"] is False,
        "manifest_validation_passed": validation["all_checks_passed"] is True,
    }
    return {
        "format": "h621-frozen-h600-canary-candidate-manifest-audit-v1",
        "hypothesis_id": HYPOTHESIS_ID,
        "manifest": manifest,
        "validation": validation,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "decision": "freeze_h600_route_wait_for_owner_scoped_v2_receipts",
        "root_cause": "H600 has the strongest old-clean stress evidence, but paper evidence does not provide owner-scoped OrderFilled, settlement, released-cash and balance continuity; the manifest freezes the route without treating paper rows as Canary fills.",
    }


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    if not result["all_checks_passed"]:
        raise SystemExit("h621_manifest_audit_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))


"""Freeze the best old-clean paper candidate and its Canary admission contract.

This module is deliberately a static contract.  It does not read market data,
fit a model, call a provider, place an order, or count a paper fill as a
Canary.  Future owner-scoped receipts may be checked against this manifest,
but the manifest itself can never open the live gate.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

# Keep this contract dependency-free.  Importing the replay module here would
# pull numpy/pandas/sklearn into a workflow that only audits a frozen manifest.
# These values are copied from H432/H434/H447 and are checked into this file so
# a future dependency or replay refactor cannot silently alter admission.
SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
FEATURES = [
    "btc_move_1m", "btc_move_2m", "btc_move_4m", "mid_up_prob_open",
    "mid_up_prob_2m", "mid_up_prob_change_2m", "buy_up_price_2m",
    "buy_down_price_2m", "buy_up_size_2m", "buy_down_size_2m",
    "sell_up_size_2m", "sell_down_size_2m", "size_imbalance_updown_2m",
    "sell_size_imbalance_updown_2m", "bid_depth_imbalance_updown_2m",
    "book_pressure_up_2m", "book_pressure_down_2m",
    "spread_up_median_first2m", "spread_down_median_first2m",
    "overround_median_first2m", "quote_count_first2m",
    "trade_count_sum_first2m", "trade_volume_sum_first2m",
    "realized_vol_first2m", "path_efficiency_first2m",
    "max_drawdown_first2m", "max_rebound_first2m",
]
MODEL_PARAMS = {
    "n_estimators": 120, "max_depth": 7, "min_samples_leaf": 25,
    "max_features": 0.7, "class_weight": "balanced_subsample",
    "random_state": 4121, "n_jobs": -1,
}
SLIPPAGES = (0.005, 0.010)
INITIAL_CASH = 10.0
MINIMUM_ORDER_USD = 5.0
MINIMUM_ORDER_SHARES = 5.0
TAKER_FEE_RATE = 0.07
CASH_RELEASE_SECONDS = 900.0
ENTRY_SECONDS_AFTER_START = 124.0
EDGE_THRESHOLD = 0.02
PRICE_MIN = 0.05
PRICE_MAX = 0.80
MIN_DEPTH = 100.0
MAX_SPREAD = 0.06
MAX_OVERROUND = 0.03
REQUIRED_FILLS = 10


def _literal_assignments(path: Path) -> dict[str, Any]:
    """Read top-level literal assignments without importing replay dependencies."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value_node = node.value
        else:
            continue
        if not targets or value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError, SyntaxError):
            continue
        for target in targets:
            values[target] = value
    return values


def source_parity(source_dir: Path | None = None) -> dict[str, Any]:
    """Check copied manifest values against frozen source literals."""
    directory = Path(source_dir) if source_dir is not None else Path(__file__).resolve().parent
    h432 = _literal_assignments(directory / "h432_old_clean_extratrees_compound.py")
    h434 = _literal_assignments(directory / "h434_old_clean_extratrees_high_confidence.py")
    h447 = _literal_assignments(directory / "h447_old_clean_breakeven_exit.py")
    base = _literal_assignments(directory / "all_monthly_clob_systematic_research_v2.py")
    expected_h432 = {
        "SOURCE_COMMIT": SOURCE_COMMIT, "MODEL_PARAMS": MODEL_PARAMS, "SLIPPAGES": SLIPPAGES,
        "INITIAL_CASH": INITIAL_CASH, "MINIMUM_ORDER_USD": MINIMUM_ORDER_USD,
        "MINIMUM_ORDER_SHARES": MINIMUM_ORDER_SHARES, "TAKER_FEE_RATE": TAKER_FEE_RATE,
        "CASH_RELEASE_SECONDS": CASH_RELEASE_SECONDS, "ENTRY_SECONDS_AFTER_START": ENTRY_SECONDS_AFTER_START,
        "EDGE_THRESHOLD": EDGE_THRESHOLD, "PRICE_MIN": PRICE_MIN, "PRICE_MAX": PRICE_MAX,
        "MIN_DEPTH": MIN_DEPTH, "MAX_SPREAD": MAX_SPREAD, "MAX_OVERROUND": MAX_OVERROUND,
        "REQUIRED_FILLS": REQUIRED_FILLS,
    }
    mismatches: list[str] = []
    for name, expected in expected_h432.items():
        if h432.get(name) != expected:
            mismatches.append(f"h432.{name}")
    for name, expected in {"SOURCE_COMMIT": SOURCE_COMMIT, "MODEL_CONFIDENCE_FLOOR": 0.80}.items():
        if h434.get(name) != expected:
            mismatches.append(f"h434.{name}")
    if h447.get("SOURCE_COMMIT") != SOURCE_COMMIT:
        mismatches.append("h447.SOURCE_COMMIT")
    if h447.get("ENTRY_SECONDS_AFTER_START") != ENTRY_SECONDS_AFTER_START:
        mismatches.append("h447.ENTRY_SECONDS_AFTER_START")
    if base.get("LOGIT_FEATURES") != FEATURES:
        mismatches.append("base.LOGIT_FEATURES")
    return {
        "source_dir": str(directory),
        "all_literals_match": not mismatches,
        "mismatches": mismatches,
        "checked_modules": ["h432_old_clean_extratrees_compound.py", "h434_old_clean_extratrees_high_confidence.py", "h447_old_clean_breakeven_exit.py", "all_monthly_clob_systematic_research_v2.py"],
    }


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
    parity = source_parity()
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
        "manifest_matches_replay_source_literals": parity["all_literals_match"] is True,
    }
    return {
        "format": "h498-frozen-canary-candidate-manifest-audit-v1",
        "hypothesis_id": HYPOTHESIS_ID,
        "manifest": manifest,
        "validation": result,
        "source_parity": parity,
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

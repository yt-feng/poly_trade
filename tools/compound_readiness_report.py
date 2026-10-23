"""Report research preparation and completed ten-fill evidence separately.

The report consumes existing state and paper artifacts only.  It never fetches
market data, accesses an account, submits an order, or infers a fill.  It
keeps the bounded paper prefix, contiguous-block diagnostics, and
settlement-grounded Canary evidence as separate gates. Completed trades are an
outcome of a Canary, never a prerequisite for offline research or preparation.
This report cannot authorize execution, even when every evidence gate passes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON_OBJECT_REQUIRED:{path}")
    return value


def _h508_paper_frontier(paper: dict[str, Any]) -> dict[str, Any]:
    """Adapt the current old-clean H508 artifact to the compound gate.

    H508 uses a predeclared cash-fraction ladder and reports one selected
    fraction per latency/depth/slippage cell.  Its holdout rows are still
    chronological same-ledger chains; they must be checked directly rather
    than being forced through the older H30/H38 gate schema.
    """
    protocol = paper.get("protocol")
    cells = paper.get("cell_summaries")
    if not isinstance(protocol, dict) or not isinstance(cells, list):
        raise ValueError("H508_PROTOCOL_AND_CELLS_REQUIRED")
    protocol_ok = (
        float(protocol.get("initial_cash_usd")) == 10.0
        and protocol.get("required_fills") == 10
        and float(protocol.get("minimum_order_usd")) == 5.0
        and float(protocol.get("minimum_order_shares")) == 5.0
        and protocol.get("same_ledger_within_window") is True
        and protocol.get("no_cross_window_concatenation") is True
        and protocol.get("old_clean_dataset_only") is True
        and protocol.get("paper_only") is True
    )
    selection_failures: list[dict[str, Any]] = []
    holdout_failures: list[dict[str, Any]] = []
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("H508_CELL_REQUIRED")
        identity = {
            key: cell.get(key)
            for key in ("slippage", "latency_seconds", "depth_haircut", "selected_fraction")
        }
        if cell.get("selection_passed") is not True:
            selection_failures.append(identity)
        if not (
            cell.get("holdout_all_ten") is True
            and cell.get("holdout_min_fills") == 10
        ):
            holdout_failures.append({
                **identity,
                "holdout_all_ten": cell.get("holdout_all_ten"),
                "holdout_min_fills": cell.get("holdout_min_fills"),
                "holdout_min_cash": cell.get("holdout_min_cash"),
            })
    selection_stable = bool(cells) and not selection_failures
    holdout_stable = bool(cells) and not holdout_failures
    is_h534 = str(paper.get("format", "")).startswith("old-clean-h534-")
    fixed_policy = paper.get("dense_fixed_fraction_policy") if is_h534 else paper.get("fixed_fraction_policy")
    expected_fraction = 0.55 if is_h534 else 0.5
    fixed_policy_ready = bool(
        isinstance(fixed_policy, dict)
        and float(fixed_policy.get("gross_cash_fraction")) == expected_fraction
        and fixed_policy.get("online_observable_policy") is True
        and fixed_policy.get("selection_all_ten") is True
        and fixed_policy.get("holdout_all_ten") is True
        and fixed_policy.get("selection_min_fills") == 10
        and fixed_policy.get("holdout_min_fills") == 10
    )
    candidate = {
        "pooled_exact_ten": protocol_ok and holdout_stable and fixed_policy_ready,
        "selection_cell_failures": selection_failures,
        "holdout_block_stress_failures": holdout_failures,
        "selection_stable": selection_stable,
        "block_stable": holdout_stable,
        "fixed_fraction_policy_ready": fixed_policy_ready,
        "cell_count": len(cells),
    }
    return {
        "protocol_consistent": protocol_ok,
        "candidate_schema": (
            "old-clean-h534-dense-cash-fraction-grid-v1"
            if is_h534 else "old-clean-h508-cash-fraction-depth-tradeoff-v1"
        ),
        "candidates": {
            "h534_dense_fixed_fraction_grid" if is_h534 else "h508_cash_fraction_depth_tradeoff": candidate
        },
        "pooled_frontier_consistent": protocol_ok and selection_stable and holdout_stable and fixed_policy_ready and paper.get("holdout_gate_passed") is True,
        "selection_cell_failures": selection_failures,
        "selection_cells_stable": selection_stable,
        "fixed_fraction_policy": fixed_policy if isinstance(fixed_policy, dict) else None,
        "fixed_fraction_policy_ready": fixed_policy_ready,
        "block_stress_failures": holdout_failures,
        "block_stress_stable": holdout_stable,
    }


def _paper_frontier(paper: dict[str, Any]) -> dict[str, Any]:
    if str(paper.get("format", "")).startswith(("old-clean-h508-", "old-clean-h534-")):
        return _h508_paper_frontier(paper)
    protocol = paper.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("PAPER_PROTOCOL_REQUIRED")
    protocol_ok = (
        protocol.get("same_ledger") is True
        and protocol.get("required_fills") == 10
        and float(protocol.get("initial_cash_usd")) == 10.0
        and float(protocol.get("minimum_order_notional_usd")) == 0.0
        and float(protocol.get("minimum_order_shares")) == 5.0
    )
    gates = paper.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("PAPER_GATES_REQUIRED")
    candidates: dict[str, Any] = {}
    block_failures: list[dict[str, Any]] = []
    for candidate in ("h30_move_uncertainty", "h38_move_uncertainty_midpath"):
        row = gates.get(candidate)
        if not isinstance(row, dict):
            raise ValueError(f"PAPER_CANDIDATE_REQUIRED:{candidate}")
        pooled = bool(
            row.get("selection_all_tick_depth_slippage_gate") is True
            and row.get("holdout_all_tick_depth_slippage_gate") is True
            and row.get("exact_ten_tick_depth_gate") is True
        )
        blocks = row.get("holdout_block_stress", [])
        if not isinstance(blocks, list):
            raise ValueError(f"PAPER_BLOCK_STRESS_REQUIRED:{candidate}")
        failures = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("first_ten_fills") != 10 or block.get("first_ten_passed") is not True:
                failure = {"candidate": candidate, **block}
                failures.append(failure)
                block_failures.append(failure)
        candidates[candidate] = {
            "pooled_exact_ten": pooled,
            "block_stress_failures": failures,
            "block_stable": bool(blocks) and not failures,
        }
    return {
        "protocol_consistent": protocol_ok,
        "selection_cells_stable": True,
        "candidates": candidates,
        "pooled_frontier_consistent": protocol_ok and all(
            row["pooled_exact_ten"] for row in candidates.values()
        ),
        "block_stress_failures": block_failures,
        "block_stress_stable": all(row["block_stable"] for row in candidates.values()),
    }


def _h600_state_frontier(state: dict[str, Any]) -> dict[str, Any] | None:
    """Use the latest verified H600 summary when the paper artifact is stale.

    The readiness report is often rebuilt from a legacy H508/H30 artifact while
    the active checkpoint already contains the newer H600 stress result.  Only
    an explicitly verified, complete H600 summary may supersede that artifact;
    incomplete or differently labelled state falls back to the supplied paper
    artifact.  This remains paper evidence and never changes the receipt gate.
    """
    summary = state.get("h600_favorite_side_depth")
    if not isinstance(summary, dict):
        return None
    if summary.get("status") != "verified_remote_retained_paper_challenger":
        return None
    required_ints = (
        "selection_rows",
        "selection_passed_rows",
        "holdout_rows",
        "holdout_passed_rows",
    )
    if any(type(summary.get(key)) is not int for key in required_ints):
        return None
    if not (
        summary.get("selection_rows") == 192
        and summary.get("selection_passed_rows") == 192
        and summary.get("holdout_rows") == 216
        and summary.get("holdout_passed_rows") == 216
        and summary.get("all_408_stress_rows_passed") is True
        and isinstance(summary.get("source_commit"), str)
        and isinstance(summary.get("remote_episode_id"), str)
    ):
        return None
    candidate = {
        "pooled_exact_ten": True,
        "selection_rows": summary["selection_rows"],
        "selection_passed_rows": summary["selection_passed_rows"],
        "holdout_rows": summary["holdout_rows"],
        "holdout_passed_rows": summary["holdout_passed_rows"],
        "block_stable": True,
        "paper_only": True,
        "source_commit": summary["source_commit"],
        "remote_episode_id": summary["remote_episode_id"],
    }
    return {
        "protocol_consistent": True,
        "candidate_schema": "old-clean-h600-favorite-side-depth-sealed-v1",
        "candidates": {"h600_favorite_side_depth": candidate},
        "pooled_frontier_consistent": True,
        "selection_cells_stable": True,
        "fixed_fraction_policy_ready": True,
        "block_stress_failures": [],
        "block_stress_stable": True,
        "paper_only": True,
        "frontier_source": "verified_checkpoint_h600_summary",
    }


def _orderfilled_frontier(state: dict[str, Any]) -> dict[str, Any]:
    """Expose the future-only on-chain binding gate without inventing fills.

    The binding audit is deliberately separate from settlement and cash release.
    A missing or synthetic audit therefore remains a hard admission failure even
    when a caller supplies optimistic aggregate fill counts.
    """
    audit = state.get("latest_orderfilled_binding_audit")
    if audit is None:
        return {
            "reported": False,
            "future_only": True,
            "owner_scope_verified": False,
            "same_ledger_rows": 0,
            "ready": False,
            "evidence_boundary": "no_orderfilled_binding_audit_in_state",
        }
    if not isinstance(audit, dict):
        raise ValueError("ORDERFILLED_BINDING_AUDIT_REQUIRED")
    verification = audit.get("verification")
    decision = audit.get("decision")
    if isinstance(verification, dict) and isinstance(decision, dict):
        # Full binding-review.json shape.
        owner_scope = verification.get("current_owner_bundle_available") is True
        synthetic_marker = verification.get("synthetic_only")
        synthetic_only = synthetic_marker is True
        same_ledger_rows = decision.get("same_ledger_compounding_rows")
        canary_ready = decision.get("canary_ready") is True
        boundary = audit.get("evidence_boundary")
    else:
        # active-episode.json stores a compact, already-sanitized summary.
        # Missing row counts remain zero; the summary can never manufacture
        # an admissible ten-row binding.
        owner_scope = audit.get("current_owner_bundle_available") is True
        synthetic_marker = audit.get("synthetic_only")
        synthetic_only = synthetic_marker is True
        same_ledger_rows = audit.get("same_ledger_compounding_rows", 0)
        canary_ready = audit.get("canary_ready") is True
        boundary = audit.get("evidence_boundary")
    if synthetic_marker is not False:
        boundary = boundary or (
            "synthetic_marker_missing" if synthetic_marker is None
            else "synthetic_only_binding_evidence"
        )
        return {
            "reported": True,
            "future_only": True,
            "owner_scope_verified": owner_scope,
            "same_ledger_rows": same_ledger_rows if type(same_ledger_rows) is int else 0,
            "ready": False,
            "synthetic_only": synthetic_only,
            "evidence_boundary": boundary,
        }
    if type(same_ledger_rows) is not int:
        raise ValueError("ORDERFILLED_SAME_LEDGER_ROWS_MUST_BE_INTEGER")
    ready = (
        owner_scope
        and not synthetic_only
        and same_ledger_rows == 10
        and canary_ready
    )
    return {
        "reported": True,
        "future_only": True,
        "owner_scope_verified": owner_scope,
        "same_ledger_rows": same_ledger_rows,
        "ready": ready,
        "synthetic_only": synthetic_only,
        "evidence_boundary": boundary or "binding_reported_without_evidence_boundary",
    }


def build_report(state: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    # A first research report has no execution receipts. Absence is valid and
    # means zero completed evidence; explicit malformed evidence still fails.
    receipt = state.get("receipt_boundary_status", {})
    if not isinstance(receipt, dict):
        raise ValueError("RECEIPT_BOUNDARY_STATUS_REQUIRED")
    frontier = _h600_state_frontier(state) or _paper_frontier(paper)
    orderfilled = _orderfilled_frontier(state)
    owner_bundle = receipt.get("authenticated_owner_scoped_bundle_available") is True
    btc_bundle = receipt.get("btc_five_minute_bundle_available") is True
    verified = receipt.get("verified_canary_fills", 0)
    actual = state.get("actual_fills", 0)
    if type(verified) is not int or type(actual) is not int:
        raise ValueError("FILL_COUNTS_MUST_BE_INTEGERS")
    if verified < 0 or actual < 0:
        raise ValueError("FILL_COUNTS_MUST_BE_NONNEGATIVE")
    ledger_rows = state.get("same_ledger_compounding_rows", 0)
    if type(ledger_rows) is not int:
        raise ValueError("SAME_LEDGER_COMPOUNDING_ROWS_MUST_BE_INTEGER")
    if ledger_rows < 0:
        raise ValueError("SAME_LEDGER_COMPOUNDING_ROWS_MUST_BE_NONNEGATIVE")
    same_ledger_ready = ledger_rows == 10
    evidence_counts = {
        "settlement": receipt.get("owner_scoped_settlement_rows", 0),
        "cash_release": receipt.get("owner_scoped_cash_release_rows", 0),
        "balance_checkpoint": receipt.get("owner_scoped_balance_checkpoint_rows", 0),
    }
    if any(type(value) is not int for value in evidence_counts.values()):
        raise ValueError("RECEIPT_EVIDENCE_COUNTS_MUST_BE_INTEGERS")
    if any(value < 0 for value in evidence_counts.values()):
        raise ValueError("RECEIPT_EVIDENCE_COUNTS_MUST_BE_NONNEGATIVE")
    evidence_flags = {
        "settlement": receipt.get("settlement_evidence_verified") is True,
        "cash_release": receipt.get("cash_release_evidence_verified") is True,
        "balance_checkpoint": receipt.get("balance_checkpoint_verified") is True,
    }
    evidence_ready = {
        key: evidence_flags[key] and evidence_counts[key] == 10
        for key in evidence_counts
    }
    all_evidence_ready = all(evidence_ready.values())
    paper_validity = {
        "causality_verified": state.get("paper_causality_verified") is True,
        "split_integrity_verified": state.get("paper_split_integrity_verified") is True,
        "evidence_scope": "supplied_checkpoint_claims",
    }
    settlement_evidence_ready = (
        owner_bundle
        and btc_bundle
        and verified >= 10
        and actual >= 10
        and orderfilled["ready"]
        and same_ledger_ready
        and all_evidence_ready
    )
    # The paper gate prepares a candidate for execution engineering. It does
    # not need future fills, and real receipts cannot repair paper instability.
    paper_preparation_ready = (
        frontier["pooled_frontier_consistent"]
        and frontier["block_stress_stable"]
        and frontier.get("selection_cells_stable", True)
        and frontier.get("fixed_fraction_policy_ready", True)
        and paper_validity["causality_verified"]
        and paper_validity["split_integrity_verified"]
    )
    completion_blockers = []
    if not owner_bundle or not btc_bundle:
        completion_blockers.append("authenticated_owner_scoped_btc_five_minute_bundle_missing")
    if verified < 10 or actual < 10:
        completion_blockers.append("ten_verified_same_ledger_fills_missing")
    if not same_ledger_ready:
        completion_blockers.append("same_ledger_compounding_rows_missing_or_not_exactly_ten")
    if not orderfilled["ready"]:
        completion_blockers.append("onchain_orderfilled_binding_missing_or_unverified")
    preparation_blockers = []
    if not paper_validity["causality_verified"]:
        preparation_blockers.append("paper_causality_missing_or_failed")
    if not paper_validity["split_integrity_verified"]:
        preparation_blockers.append("paper_split_integrity_missing_or_failed")
    if not frontier["protocol_consistent"]:
        preparation_blockers.append("paper_protocol_inconsistent")
    if not frontier["pooled_frontier_consistent"]:
        preparation_blockers.append("paper_pooled_frontier_inconsistent")
    if not frontier["block_stress_stable"]:
        preparation_blockers.append("paper_block_stress_unstable")
    if not frontier.get("selection_cells_stable", True):
        preparation_blockers.append("paper_selection_cells_unstable")
    if not frontier.get("fixed_fraction_policy_ready", True):
        preparation_blockers.append("paper_executable_fraction_policy_missing_or_failed")
    for key, ready in evidence_ready.items():
        if not ready:
            completion_blockers.append(f"{key}_receipts_missing_or_unverified")
    next_stage = (
        "completed_evidence_review" if settlement_evidence_ready
        else "execution_preparation" if paper_preparation_ready
        else "offline_research"
    )
    return {
        "format": "compound-readiness-report-v2",
        "source_scope": "existing_state_and_paper_artifact_only",
        "required_fills": 10,
        "same_ledger_contract": "one chronological evolving cash ledger; no reset or concatenation",
        "same_ledger": {
            "rows": ledger_rows,
            "required_rows": 10,
            "ready": same_ledger_ready,
        },
        "paper_frontier": frontier,
        "paper_validity": paper_validity,
        "orderfilled_binding": orderfilled,
        "settlement_evidence": {
            "owner_scoped_bundle": owner_bundle,
            "btc_five_minute_bundle": btc_bundle,
            "actual_fills": actual,
            "verified_canary_fills": verified,
            "evidence_counts": evidence_counts,
            "evidence_verified": evidence_flags,
            "evidence_ready": evidence_ready,
            "ready": settlement_evidence_ready,
        },
        "lifecycle": {
            "offline_research_allowed": True,
            "paper_candidate_ready_for_execution_preparation": paper_preparation_ready,
            "completed_canary_evidence_verified": settlement_evidence_ready,
            "completed_evidence_scope": "supplied_checkpoint_claims_not_independent_receipt_authentication",
            "canary_launch_authorized": False,
            "next_stage": next_stage,
            "next_actions": (
                ["review_completed_chain_and_candidate_evidence"]
                if settlement_evidence_ready else
                ["prepare_frozen_candidate_execution_and_incremental_receipt_capture",
                 "continue_separately_registered_offline_research"]
                if paper_preparation_ready else
                ["continue_clean_historical_training_and_validation"]
            ),
            "preparation_blockers": preparation_blockers,
            "completion_evidence_blockers": completion_blockers,
            "execution_authorization_boundary": "requires_separate_explicit_authorization_and_execution_preflight",
            "future_fills_required_for_research_or_preparation": False,
        },
        # Kept for older readers, with an explicit retrospective meaning.
        # New orchestration must use lifecycle instead of treating this as a
        # prerequisite to starting research or the first Canary trade.
        "canary_ready": paper_preparation_ready and settlement_evidence_ready,
        "canary_ready_semantics": "legacy_completed_evidence_and_paper_gate_not_launch_permission",
        "hard_blockers": preparation_blockers + completion_blockers,
        "hard_blockers_scope": "legacy_combined_completed_evidence_report_only",
        "reported_live_orders_enabled": state.get("live_orders_enabled") is True,
        "live_orders_enabled": False,
        "new_data_collected": False,
        "model_refit": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--paper-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(_load(args.state), _load(args.paper_artifact))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

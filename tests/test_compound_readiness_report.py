from __future__ import annotations

import pytest

from tools.compound_readiness_report import build_report


def paper_artifact(*, block_failure: bool = True) -> dict:
    block = {"split": "holdout_block_1", "first_ten_fills": 10, "first_ten_passed": True}
    if block_failure:
        block = {"split": "holdout_block_3", "first_ten_fills": 3, "first_ten_passed": False}
    row = {
        "selection_all_tick_depth_slippage_gate": True,
        "holdout_all_tick_depth_slippage_gate": True,
        "exact_ten_tick_depth_gate": True,
        "holdout_block_stress": [block],
    }
    return {
        "protocol": {
            "same_ledger": True,
            "required_fills": 10,
            "initial_cash_usd": 10.0,
            "minimum_order_notional_usd": 0.0,
            "minimum_order_shares": 5.0,
        },
        "gates": {"h30_move_uncertainty": row, "h38_move_uncertainty_midpath": row},
    }


def h508_paper_artifact(*, holdout_failure: bool = False) -> dict:
    return {
        "format": "old-clean-h508-cash-fraction-depth-tradeoff-v1",
        "holdout_gate_passed": not holdout_failure,
        "protocol": {
            "initial_cash_usd": 10.0,
            "required_fills": 10,
            "minimum_order_usd": 5.0,
            "minimum_order_shares": 5.0,
            "same_ledger_within_window": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "cell_summaries": [{
            "slippage": 0.005,
            "latency_seconds": 2.0,
            "depth_haircut": 0.1,
            "selected_fraction": 0.5,
            "selection_passed": True,
            "holdout_all_ten": not holdout_failure,
            "holdout_min_fills": 10 if not holdout_failure else 9,
            "holdout_min_cash": 11.0,
        }],
        "fixed_fraction_policy": {
            "gross_cash_fraction": 0.5,
            "online_observable_policy": True,
            "selection_all_ten": not holdout_failure,
            "holdout_all_ten": not holdout_failure,
            "selection_min_fills": 10 if not holdout_failure else 9,
            "holdout_min_fills": 10 if not holdout_failure else 9,
        },
    }


def h534_paper_artifact(*, holdout_failure: bool = False) -> dict:
    artifact = h508_paper_artifact(holdout_failure=holdout_failure)
    artifact["format"] = "old-clean-h534-dense-cash-fraction-grid-v1"
    artifact.pop("fixed_fraction_policy")
    artifact["dense_fixed_fraction_policy"] = {
        "gross_cash_fraction": 0.55,
        "online_observable_policy": True,
        "selection_all_ten": not holdout_failure,
        "holdout_all_ten": not holdout_failure,
        "selection_min_fills": 10 if not holdout_failure else 9,
        "holdout_min_fills": 10 if not holdout_failure else 9,
    }
    artifact["selected_fraction"] = 0.55
    return artifact


def state(*, bundle: bool = False, fills: int = 0, binding: bool = False, receipts: bool = False) -> dict:
    return {
        "paper_causality_verified": True,
        "paper_split_integrity_verified": True,
        "actual_fills": fills,
        "same_ledger_compounding_rows": 10 if binding else 0,
        "live_orders_enabled": False,
        "receipt_boundary_status": {
            "authenticated_owner_scoped_bundle_available": bundle,
            "btc_five_minute_bundle_available": bundle,
            "verified_canary_fills": fills,
            "owner_scoped_settlement_rows": 10 if receipts else 0,
            "owner_scoped_cash_release_rows": 10 if receipts else 0,
            "owner_scoped_balance_checkpoint_rows": 10 if receipts else 0,
            "settlement_evidence_verified": receipts,
            "cash_release_evidence_verified": receipts,
            "balance_checkpoint_verified": receipts,
        },
        "latest_orderfilled_binding_audit": {
            "verification": {
                "current_owner_bundle_available": binding,
                "synthetic_only": not binding,
            },
            "decision": {
                "same_ledger_compounding_rows": 10 if binding else 0,
                "canary_ready": binding,
            },
            "evidence_boundary": "test_fixture",
        },
    }


def h600_state() -> dict:
    fixture = state()
    fixture["h600_favorite_side_depth"] = {
        "status": "verified_remote_retained_paper_challenger",
        "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
        "remote_episode_id": "episode-h600",
        "selection_rows": 192,
        "selection_passed_rows": 192,
        "holdout_rows": 216,
        "holdout_passed_rows": 216,
        "all_408_stress_rows_passed": True,
    }
    return fixture


def test_report_separates_pooled_prefix_from_block_stress_and_receipts():
    report = build_report(state(), paper_artifact())
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert report["settlement_evidence"]["ready"] is False
    assert report["canary_ready"] is False
    assert "authenticated_owner_scoped_btc_five_minute_bundle_missing" in report["hard_blockers"]


def test_report_can_only_be_ready_with_ten_settled_fills_and_consistent_protocol():
    report = build_report(state(bundle=True, fills=10, binding=True, receipts=True), paper_artifact(block_failure=False))
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["settlement_evidence"]["ready"] is True
    assert report["canary_ready"] is True


def test_block_stress_blocks_canary_admission_even_with_real_settlement_evidence():
    report = build_report(state(bundle=True, fills=10, binding=True, receipts=True), paper_artifact(block_failure=True))
    assert report["settlement_evidence"]["ready"] is True
    assert report["canary_ready"] is False
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert "paper_block_stress_unstable" in report["hard_blockers"]


def test_report_fails_closed_when_binding_audit_is_absent():
    fixture = state(bundle=True, fills=10)
    fixture.pop("latest_orderfilled_binding_audit")
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is False
    assert report["settlement_evidence"]["ready"] is False
    assert "onchain_orderfilled_binding_missing_or_unverified" in report["hard_blockers"]


def test_report_fails_closed_when_receipt_components_are_absent():
    report = build_report(state(bundle=True, fills=10, binding=True), paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is True
    assert report["settlement_evidence"]["ready"] is False
    assert "settlement_receipts_missing_or_unverified" in report["hard_blockers"]
    assert "cash_release_receipts_missing_or_unverified" in report["hard_blockers"]
    assert "balance_checkpoint_receipts_missing_or_unverified" in report["hard_blockers"]


def test_report_fails_closed_when_same_ledger_row_count_is_not_exactly_ten():
    fixture = state(bundle=True, fills=10, binding=True, receipts=True)
    fixture["same_ledger_compounding_rows"] = 9
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is True
    assert report["settlement_evidence"]["ready"] is False
    assert "same_ledger_compounding_rows_missing_or_not_exactly_ten" in report["hard_blockers"]


def test_report_fails_closed_when_binding_is_not_explicitly_non_synthetic():
    fixture = state(bundle=True, fills=10, binding=True)
    del fixture["latest_orderfilled_binding_audit"]["verification"]["synthetic_only"]
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is False
    assert report["settlement_evidence"]["ready"] is False


def test_report_consumes_h508_old_clean_holdout_contract():
    report = build_report(state(), h508_paper_artifact())
    assert report["paper_frontier"]["candidate_schema"] == "old-clean-h508-cash-fraction-depth-tradeoff-v1"
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["paper_frontier"]["block_stress_stable"] is True
    assert report["paper_frontier"]["selection_cells_stable"] is True
    assert report["paper_frontier"]["fixed_fraction_policy_ready"] is True
    assert report["canary_ready"] is False


def test_h508_holdout_failure_blocks_the_compound_gate():
    report = build_report(state(), h508_paper_artifact(holdout_failure=True))
    assert report["paper_frontier"]["pooled_frontier_consistent"] is False
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert "paper_block_stress_unstable" in report["hard_blockers"]


def test_h508_without_online_fraction_policy_fails_closed():
    artifact = h508_paper_artifact()
    artifact.pop("fixed_fraction_policy")
    report = build_report(state(), artifact)
    assert report["paper_frontier"]["pooled_frontier_consistent"] is False
    assert "paper_executable_fraction_policy_missing_or_failed" in report["hard_blockers"]


def test_report_consumes_h534_dense_fixed_fraction_contract():
    report = build_report(state(), h534_paper_artifact())
    assert report["paper_frontier"]["candidate_schema"] == "old-clean-h534-dense-cash-fraction-grid-v1"
    assert report["paper_frontier"]["fixed_fraction_policy"]["gross_cash_fraction"] == 0.55
    assert report["paper_frontier"]["fixed_fraction_policy_ready"] is True
    assert report["canary_ready"] is False


def test_report_prefers_verified_h600_checkpoint_over_stale_paper_artifact():
    report = build_report(h600_state(), paper_artifact(block_failure=True))
    assert report["paper_frontier"]["candidate_schema"] == "old-clean-h600-favorite-side-depth-sealed-v1"
    assert report["paper_frontier"]["frontier_source"] == "verified_checkpoint_h600_summary"
    assert report["paper_frontier"]["block_stress_stable"] is True
    assert "paper_block_stress_unstable" not in report["hard_blockers"]
    assert report["canary_ready"] is False


def test_incomplete_h600_checkpoint_does_not_override_paper_artifact():
    fixture = h600_state()
    fixture["h600_favorite_side_depth"]["holdout_passed_rows"] = 215
    report = build_report(fixture, paper_artifact(block_failure=True))
    assert "h30_move_uncertainty" in report["paper_frontier"]["candidates"]
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert "paper_block_stress_unstable" in report["hard_blockers"]


def test_first_offline_candidate_needs_no_future_receipts_or_fill_counters():
    report = build_report({
        "paper_causality_verified": True,
        "paper_split_integrity_verified": True,
    }, paper_artifact(block_failure=False))
    lifecycle = report["lifecycle"]
    assert lifecycle["offline_research_allowed"] is True
    assert lifecycle["paper_candidate_ready_for_execution_preparation"] is True
    assert lifecycle["preparation_blockers"] == []
    assert lifecycle["next_stage"] == "execution_preparation"
    assert lifecycle["future_fills_required_for_research_or_preparation"] is False
    assert lifecycle["completed_canary_evidence_verified"] is False
    assert "ten_verified_same_ledger_fills_missing" in lifecycle["completion_evidence_blockers"]
    assert lifecycle["canary_launch_authorized"] is False
    assert report["settlement_evidence"]["actual_fills"] == 0


def test_failed_paper_candidate_keeps_offline_work_available():
    report = build_report({}, paper_artifact(block_failure=True))
    lifecycle = report["lifecycle"]
    assert lifecycle["offline_research_allowed"] is True
    assert lifecycle["paper_candidate_ready_for_execution_preparation"] is False
    assert lifecycle["next_stage"] == "offline_research"
    assert "paper_block_stress_unstable" in lifecycle["preparation_blockers"]
    assert "ten_verified_same_ledger_fills_missing" not in lifecycle["preparation_blockers"]


def test_completed_evidence_is_separate_from_paper_quality_and_launch_permission():
    fixture = state(bundle=True, fills=10, binding=True, receipts=True)
    fixture["live_orders_enabled"] = True
    report = build_report(fixture, paper_artifact(block_failure=True))
    lifecycle = report["lifecycle"]
    assert lifecycle["completed_canary_evidence_verified"] is True
    assert lifecycle["completion_evidence_blockers"] == []
    assert lifecycle["paper_candidate_ready_for_execution_preparation"] is False
    assert lifecycle["canary_launch_authorized"] is False
    assert report["reported_live_orders_enabled"] is True
    assert report["live_orders_enabled"] is False
    assert report["canary_ready"] is False


def test_passing_every_evidence_gate_still_cannot_authorize_trading():
    report = build_report(
        state(bundle=True, fills=10, binding=True, receipts=True),
        paper_artifact(block_failure=False),
    )
    assert report["lifecycle"]["completed_canary_evidence_verified"] is True
    assert report["lifecycle"]["canary_launch_authorized"] is False
    assert report["live_orders_enabled"] is False


def test_nine_completed_rows_do_not_block_preparation_or_complete_the_canary():
    fixture = state(bundle=True, fills=10, binding=True, receipts=True)
    fixture["same_ledger_compounding_rows"] = 9
    report = build_report(fixture, paper_artifact(block_failure=False))
    lifecycle = report["lifecycle"]
    assert lifecycle["paper_candidate_ready_for_execution_preparation"] is True
    assert lifecycle["completed_canary_evidence_verified"] is False
    assert lifecycle["preparation_blockers"] == []
    assert "same_ledger_compounding_rows_missing_or_not_exactly_ten" in lifecycle["completion_evidence_blockers"]


@pytest.mark.parametrize("schema", ["legacy", "cash_fraction"])
def test_missing_stress_observations_cannot_qualify_preparation(schema):
    artifact = paper_artifact(block_failure=False) if schema == "legacy" else h508_paper_artifact()
    if schema == "legacy":
        for row in artifact["gates"].values():
            row["holdout_block_stress"] = []
    else:
        artifact["cell_summaries"] = []
    report = build_report({}, artifact)
    assert report["lifecycle"]["paper_candidate_ready_for_execution_preparation"] is False
    assert "paper_block_stress_unstable" in report["lifecycle"]["preparation_blockers"]


@pytest.mark.parametrize("validity", [{}, {
    "paper_causality_verified": False,
    "paper_split_integrity_verified": True,
}, {
    "paper_causality_verified": True,
    "paper_split_integrity_verified": False,
}])
def test_stable_paper_counts_do_not_substitute_for_causal_and_split_audit(validity):
    fixture = h600_state()
    fixture.pop("paper_causality_verified")
    fixture.pop("paper_split_integrity_verified")
    fixture.update(validity)
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["lifecycle"]["paper_candidate_ready_for_execution_preparation"] is False
    assert report["lifecycle"]["offline_research_allowed"] is True
    assert report["lifecycle"]["next_stage"] == "offline_research"


@pytest.mark.parametrize("bad_state", [
    {"receipt_boundary_status": None},
    {"actual_fills": True},
    {"actual_fills": -1},
    {"same_ledger_compounding_rows": -1},
    {"receipt_boundary_status": {"owner_scoped_settlement_rows": -1}},
])
def test_absent_evidence_is_allowed_but_malformed_evidence_is_not(bad_state):
    with pytest.raises(ValueError):
        build_report(bad_state, paper_artifact(block_failure=False))

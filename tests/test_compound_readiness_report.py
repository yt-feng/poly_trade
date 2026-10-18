from __future__ import annotations

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


def state(*, bundle: bool = False, fills: int = 0, binding: bool = False) -> dict:
    return {
        "actual_fills": fills,
        "live_orders_enabled": False,
        "receipt_boundary_status": {
            "authenticated_owner_scoped_bundle_available": bundle,
            "btc_five_minute_bundle_available": bundle,
            "verified_canary_fills": fills,
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


def test_report_separates_pooled_prefix_from_block_stress_and_receipts():
    report = build_report(state(), paper_artifact())
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert report["settlement_evidence"]["ready"] is False
    assert report["canary_ready"] is False
    assert "authenticated_owner_scoped_btc_five_minute_bundle_missing" in report["hard_blockers"]


def test_report_can_only_be_ready_with_ten_settled_fills_and_consistent_protocol():
    report = build_report(state(bundle=True, fills=10, binding=True), paper_artifact(block_failure=False))
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["settlement_evidence"]["ready"] is True
    assert report["canary_ready"] is True


def test_block_stress_is_diagnostic_when_real_settlement_evidence_exists():
    report = build_report(state(bundle=True, fills=10, binding=True), paper_artifact(block_failure=True))
    assert report["settlement_evidence"]["ready"] is True
    assert report["canary_ready"] is True
    assert report["paper_frontier"]["block_stress_stable"] is False


def test_report_fails_closed_when_binding_audit_is_absent():
    fixture = state(bundle=True, fills=10)
    fixture.pop("latest_orderfilled_binding_audit")
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is False
    assert report["settlement_evidence"]["ready"] is False
    assert "onchain_orderfilled_binding_missing_or_unverified" in report["hard_blockers"]

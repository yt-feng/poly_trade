"""Build a deterministic ten-fill compound readiness report.

The report consumes existing state and paper artifacts only.  It never fetches
market data, accesses an account, submits an order, or infers a fill.  It
keeps the bounded paper prefix, contiguous-block diagnostics, and
settlement-grounded Canary evidence as separate gates.
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


def _paper_frontier(paper: dict[str, Any]) -> dict[str, Any]:
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
            "block_stable": not failures,
        }
    return {
        "protocol_consistent": protocol_ok,
        "candidates": candidates,
        "pooled_frontier_consistent": protocol_ok and all(
            row["pooled_exact_ten"] for row in candidates.values()
        ),
        "block_stress_failures": block_failures,
        "block_stress_stable": not block_failures,
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
        synthetic_only = verification.get("synthetic_only") is True
        same_ledger_rows = decision.get("same_ledger_compounding_rows")
        canary_ready = decision.get("canary_ready") is True
        boundary = audit.get("evidence_boundary")
    else:
        # active-episode.json stores a compact, already-sanitized summary.
        # Missing row counts remain zero; the summary can never manufacture
        # an admissible ten-row binding.
        owner_scope = audit.get("current_owner_bundle_available") is True
        synthetic_only = audit.get("synthetic_only") is True
        same_ledger_rows = audit.get("same_ledger_compounding_rows", 0)
        canary_ready = audit.get("canary_ready") is True
        boundary = audit.get("evidence_boundary")
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
    receipt = state.get("receipt_boundary_status")
    if not isinstance(receipt, dict):
        raise ValueError("RECEIPT_BOUNDARY_STATUS_REQUIRED")
    frontier = _paper_frontier(paper)
    orderfilled = _orderfilled_frontier(state)
    owner_bundle = receipt.get("authenticated_owner_scoped_bundle_available") is True
    btc_bundle = receipt.get("btc_five_minute_bundle_available") is True
    verified = receipt.get("verified_canary_fills")
    actual = state.get("actual_fills")
    if type(verified) is not int or type(actual) is not int:
        raise ValueError("FILL_COUNTS_MUST_BE_INTEGERS")
    settlement_evidence_ready = (
        owner_bundle
        and btc_bundle
        and verified >= 10
        and actual >= 10
        and orderfilled["ready"]
    )
    canary_ready = frontier["pooled_frontier_consistent"] and settlement_evidence_ready
    hard_blockers = []
    if not owner_bundle or not btc_bundle:
        hard_blockers.append("authenticated_owner_scoped_btc_five_minute_bundle_missing")
    if verified < 10 or actual < 10:
        hard_blockers.append("ten_verified_same_ledger_fills_missing")
    if not orderfilled["ready"]:
        hard_blockers.append("onchain_orderfilled_binding_missing_or_unverified")
    return {
        "format": "compound-readiness-report-v1",
        "source_scope": "existing_state_and_paper_artifact_only",
        "required_fills": 10,
        "same_ledger_contract": "one chronological evolving cash ledger; no reset or concatenation",
        "paper_frontier": frontier,
        "orderfilled_binding": orderfilled,
        "settlement_evidence": {
            "owner_scoped_bundle": owner_bundle,
            "btc_five_minute_bundle": btc_bundle,
            "actual_fills": actual,
            "verified_canary_fills": verified,
            "ready": settlement_evidence_ready,
        },
        "canary_ready": canary_ready,
        "hard_blockers": hard_blockers,
        "live_orders_enabled": state.get("live_orders_enabled") is True,
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

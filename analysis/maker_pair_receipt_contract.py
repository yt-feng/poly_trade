"""Fail-closed offline validator for a maker-only two-leg pair cash chain.

The validator consumes captured evidence only.  It has no wallet, network,
order, cancel, merge, redeem or balance-reading capability.  A pair is an
offline settlement candidate only after both maker legs, the merge/settlement
receipt, a distinct cash-release/redemption receipt, and an independent cash
checkpoint are joined.  Even a complete synthetic chain never becomes an
authenticated Canary here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from sequential_compound_gate import evaluate_compound_chain


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def validate_maker_pair_chain(
    events: Iterable[dict[str, Any]],
    *,
    owner: str,
    initial_cash: Any = "10",
    minimum_order: Any = "5",
    required_pairs: int = 10,
) -> dict[str, Any]:
    """Validate chronological maker pairs and feed them to the shared ledger."""
    expected_owner = _text(owner)
    if not expected_owner:
        raise ValueError("owner is required")
    if required_pairs <= 0:
        raise ValueError("required_pairs must be positive")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = (
        "pair_id", "up_order_id", "down_order_id", "up_trade_id", "down_trade_id",
        "up_transaction_ref", "down_transaction_ref", "merge_receipt_id",
        "settlement_receipt_id", "cash_release_receipt_id", "balance_checkpoint_id",
        "entry_ts", "cash_available_ts", "entry_cost", "fee", "payout",
    )
    for row in events:
        if not isinstance(row, dict):
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "invalid_pair_event"}
        if _text(row.get("owner")) != expected_owner:
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "owner_scope_mismatch"}
        missing = next((field for field in required if not _text(row.get(field))), None)
        if missing:
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"missing_{missing}"}
        if _text(row.get("up_trader_side")).upper() != "MAKER" or _text(row.get("down_trader_side")).upper() != "MAKER":
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "maker_side_not_confirmed"}
        if _text(row.get("status")).lower() != "confirmed":
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "pair_not_confirmed"}
        for leg in ("up", "down"):
            if _text(row.get(f"{leg}_order_type")).upper() not in {"GTC", "GTD"}:
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_order_type_must_be_gtc_or_gtd"}
            if row.get(f"{leg}_post_only") is not True:
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_post_only_required"}
            if _text(row.get(f"{leg}_order_status")).upper() != "MATCHED":
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_order_match_status_required"}
            if _text(row.get(f"{leg}_trade_status")).upper() not in {"CONFIRMED", "TRADE_STATUS_CONFIRMED"}:
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_trade_confirmation_required"}
            if _text(row.get(f"{leg}_trader_side")).upper() != "MAKER":
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_trader_side_required"}
            try:
                acceptance_ts = int(str(row.get(f"{leg}_order_acceptance_ts")))
                matched_ts = int(str(row.get(f"{leg}_trade_matched_ts")))
                entry_ts = int(str(row.get("entry_ts")))
            except (TypeError, ValueError):
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_order_timestamps_required"}
            if acceptance_ts > entry_ts:
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_order_acceptance_after_entry"}
            if matched_ts > entry_ts:
                return {"gate_passed": False, "pairs": len(rows), "failure_reason": f"{leg}_trade_match_after_entry"}
        if _text(row.get("up_fee_rate_bps")) not in {"0", "0.0", "0.00"} or _text(row.get("down_fee_rate_bps")) not in {"0", "0.0", "0.00"}:
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "maker_fee_not_zero"}
        if row.get("cash_reconciled") is not True:
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "cash_checkpoint_not_reconciled"}
        if row.get("independent_checkpoint") is not True:
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "checkpoint_not_independent"}
        identities = (
            _text(row["pair_id"]), _text(row["up_order_id"]), _text(row["down_order_id"]),
            _text(row["up_trade_id"]), _text(row["down_trade_id"]),
            _text(row["up_transaction_ref"]), _text(row["down_transaction_ref"]),
            _text(row["merge_receipt_id"]), _text(row["settlement_receipt_id"]),
            _text(row["cash_release_receipt_id"]),
            _text(row["balance_checkpoint_id"]),
        )
        if any(identity in seen for identity in identities):
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "duplicate_pair_identity"}
        seen.update(identities)
        if len({
            _text(row["merge_receipt_id"]),
            _text(row["settlement_receipt_id"]),
            _text(row["cash_release_receipt_id"]),
        }) != 3:
            return {"gate_passed": False, "pairs": len(rows), "failure_reason": "pair_receipts_must_be_independent"}
        rows.append({
            "owner": expected_owner,
            "status": "confirmed",
            "sequence_no": row.get("sequence_no"),
            "trade_id": _text(row["pair_id"]),
            "transaction_ref": _text(row["up_transaction_ref"]) + "|" + _text(row["down_transaction_ref"]),
            "settlement_receipt_id": _text(row["settlement_receipt_id"]),
            "cash_release_receipt_id": _text(row["cash_release_receipt_id"]),
            "balance_checkpoint_id": _text(row["balance_checkpoint_id"]),
            "cash_reconciled": True,
            "independent_checkpoint": True,
            "entry_ts": row["entry_ts"],
            "cash_available_ts": row["cash_available_ts"],
            "entry_cost": row["entry_cost"],
            "fee": row["fee"],
            "payout": row["payout"],
        })
    if [row.get("sequence_no") for row in rows] != list(range(1, required_pairs + 1)):
        return {"gate_passed": False, "pairs": len(rows), "failure_reason": "sequence_no_must_be_exactly_one_to_ten"}
    result = evaluate_compound_chain(
        rows,
        initial_cash=initial_cash,
        minimum_order=minimum_order,
        required_fills=required_pairs,
        evidence_mode="authenticated",
        require_cash_release_receipt=True,
        require_prior_cash_release=True,
    )
    offline_passed = bool(result["gate_passed"])
    return {
        "pairs": len(rows),
        "required_pairs": int(required_pairs),
        # This validator only receives caller-supplied artifacts.  Even a
        # perfectly consistent synthetic chain must never be labelled as a
        # Canary without independently authenticated account receipts.
        "gate_passed": False,
        "offline_consistency_passed": offline_passed,
        "receipt_authenticity_verified": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "fills": int(result["fills"]),
        "consecutive": bool(result["consecutive"]),
        "paper_replay_passed": bool(result["paper_replay_passed"]),
        "failure_reason": result["failure_reason"],
        "ending_cash": result["ending_cash"],
        "net_pnl": result["net_pnl"],
        "owner_scoped": True,
        "maker_pair_evidence_complete": True,
    }

"""Verify that strict asynchronous maker admission is the default path."""
from __future__ import annotations

import json
from typing import Any

try:
    from .h342_strict_maker_admission import _event, _fill
    from .maker_compound_receipt_contract import validate_maker_compound_chain
except ImportError:  # pragma: no cover - standalone repository tools.
    from h342_strict_maker_admission import _event, _fill
    from maker_compound_receipt_contract import validate_maker_compound_chain


def _validate(events: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    return validate_maker_compound_chain(
        events,
        fills,
        owner="owner-key",
        maker_address="0xmaker",
        starting_checkpoint_id="opening",
        closing_balance_checkpoint_id="closing",
        closing_balance="16.10",
        require_rebate=True,
    )


def audit() -> dict[str, Any]:
    fills = [_fill(i) for i in range(1, 11)]
    unbound = _validate([_event(i, async_binding=False) for i in range(1, 11)], fills)
    bound = _validate([_event(i, async_binding=True) for i in range(1, 11)], fills)
    return {
        "format": "h343-strict-maker-default-v1",
        "hypothesis_id": "H343-strict-maker-default",
        "source_scope": "local maker contract default behavior over synthetic shaped rows; no market data, account access, orders or settlement access",
        "default_unbound_chain_passed": bool(unbound.get("offline_consistency_passed")),
        "default_unbound_failure": unbound.get("failure_reason"),
        "default_bound_chain_passed": bool(bound.get("offline_consistency_passed")),
        "default_bound_rows": int(bound.get("same_ledger_compounding_rows", 0)),
        "default_bound_gate_passed": bool(bound.get("gate_passed")),
        "receipt_authenticity_verified": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "decision": "strict_async_binding_is_default_but_external_receipts_missing",
        "root_cause": "An optional strict flag left a bypass in ordinary maker admission. The default is now strict, so unbound synthetic rows cannot enter the offline ledger contract; bound synthetic rows still do not count as Canary without authenticated external receipts and reusable cash.",
    }


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, sort_keys=True))

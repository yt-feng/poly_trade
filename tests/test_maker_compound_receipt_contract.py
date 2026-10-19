import unittest
from decimal import Decimal

from analysis.maker_compound_receipt_contract import validate_maker_compound_chain


def maker_event(i: int, **updates):
    sequence = i
    before = Decimal("10") + (sequence - 1) * Decimal("0.61")
    value = {
        "owner": "owner-key",
        "maker_address": "0xmaker",
        "ledger_id": "maker-ledger-1",
        "chain_id": "maker-chain-1",
        "sequence_no": sequence,
        "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{sequence - 1}",
        "status": "confirmed",
        "trade_status": "TRADE_STATUS_CONFIRMED",
        "trade_id": f"trade-{sequence}",
        "transaction_ref": f"0xtx{sequence}",
        "settlement_receipt_id": f"settlement-{sequence}",
        "cash_release_receipt_id": f"cash-release-{sequence}",
        "balance_checkpoint_id": f"balance-{sequence}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(sequence * 100),
        "cash_available_ts": str(sequence * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.61",
        "released_cash_since_previous_entry": "0" if sequence == 1 else "5.61",
        "balance_before": str(before),
        "balance_after": str(before - Decimal("5")),
        "market_id": "m1",
        "shares": "10",
        "price": "0.5",
        "execution_role": "MAKER",
        "orderfilled_event_id": f"0xtx{sequence}:1",
        "order_id": f"order-{sequence}",
        "post_only_accepted": True,
        "order_status": "MATCHED",
        "trader_side": "MAKER",
        "fee_rate": "0",
        "settlement_payout": "5.50",
        "rebate_amount": "0.11",
        "rebate_status": "CONFIRMED",
        "rebate_batch_id": "rebate-day-1",
        "rebate_receipt_id": "rebate-receipt-day-1",
        "rebate_batch_amount": "1.10",
        "rebate_allocated_amount": "0.11",
        "rebate_available_ts": str(sequence * 100 + 10),
        "async_trade_id_binding_ready": True,
    }
    value.update(updates)
    return value


def orderfilled(i: int, *, maker: str = "0xmaker"):
    return {
        "event_id": f"0xtx{i}:1",
        "tx_hash": f"0xtx{i}",
        "market_id": "m1",
        "maker": maker,
        "taker": "0xtaker",
        "side": "BUY",
        "sign": 1,
        "size": "10",
        "cash_amount": "5",
        "price": "0.5",
    }


def bundle():
    return [maker_event(i) for i in range(1, 11)], [orderfilled(i) for i in range(1, 11)]


class MakerCompoundReceiptContractTest(unittest.TestCase):
    def test_valid_chain_is_offline_only_and_has_exactly_ten_rows(self):
        events, fills = bundle()
        result = validate_maker_compound_chain(
            events,
            fills,
            owner="owner-key",
            maker_address="0xmaker",
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
        )
        self.assertTrue(result["offline_consistency_passed"])
        self.assertTrue(result["paper_replay_passed"])
        self.assertEqual(result["same_ledger_compounding_rows"], 10)
        self.assertFalse(result["receipt_authenticity_verified"])
        self.assertEqual(result["actual_fills"], 0)
        self.assertFalse(result["gate_passed"])

    def test_taker_row_is_not_admitted_to_maker_contract(self):
        events, fills = bundle()
        events[0]["execution_role"] = "TAKER"
        result = validate_maker_compound_chain(
            events,
            fills,
            owner="owner-key",
            maker_address="0xmaker",
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
        )
        self.assertEqual(result["failure_reason"], "maker_event_contract_failed")
        self.assertIn("execution_role_must_be_maker", {x["reason"] for x in result["issues"]})

    def test_post_only_acceptance_is_required(self):
        events, fills = bundle()
        events[3]["post_only_accepted"] = False
        result = validate_maker_compound_chain(
            events,
            fills,
            owner="owner-key",
            maker_address="0xmaker",
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
        )
        self.assertIn("post_only_acceptance_required", {x["reason"] for x in result["issues"]})

    def test_orderfilled_owner_must_bind_to_maker(self):
        events, fills = bundle()
        fills[5]["maker"] = "0xother"
        result = validate_maker_compound_chain(
            events,
            fills,
            owner="owner-key",
            maker_address="0xmaker",
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
        )
        self.assertEqual(result["failure_reason"], "maker_orderfilled_binding_failed")
        self.assertIn("maker_owner_mismatch", {x["reason"] for x in result["issues"]})

    def test_settlement_and_rebate_must_equal_released_payout(self):
        events, fills = bundle()
        events[2]["rebate_amount"] = "0.02"
        result = validate_maker_compound_chain(
            events,
            fills,
            owner="owner-key",
            maker_address="0xmaker",
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
        )
        self.assertIn("settlement_plus_rebate_mismatch", {x["reason"] for x in result["issues"]})

    def test_async_binding_is_required_when_strict_admission_is_enabled(self):
        events, fills = bundle()
        for event in events:
            event.pop("async_trade_id_binding_ready", None)
        result = validate_maker_compound_chain(
            events,
            fills,
            owner="owner-key",
            maker_address="0xmaker",
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
        )
        self.assertEqual(result["failure_reason"], "maker_event_contract_failed")
        self.assertTrue(all(issue["reason"] == "async_trade_id_binding_required" for issue in result["issues"]))

        for event in events:
            event["async_trade_id_binding_ready"] = True
        result = validate_maker_compound_chain(
            events,
            fills,
            owner="owner-key",
            maker_address="0xmaker",
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
            require_async_binding=True,
        )
        self.assertTrue(result["offline_consistency_passed"])
        self.assertEqual(result["same_ledger_compounding_rows"], 10)
        self.assertFalse(result["gate_passed"])


if __name__ == "__main__":
    unittest.main()

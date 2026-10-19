import unittest
from decimal import Decimal

from analysis.maker_compound_bundle_adapter import assemble_maker_compound_row
from analysis.maker_compound_receipt_contract import validate_maker_compound_chain


def order_event(status="MATCHED"):
    return {"kind": "entry_order_accepted", "order_id": "maker-order-1", "order_status": status}


def trade_event(**updates):
    value = {
        "kind": "entry_fill",
        "owner": "owner-key",
        "trade_owner": "owner-key",
        "maker_address": "0xmaker",
        "trade_id": "trade-1",
        "transaction_ref": "0xtx",
        "market_id": "market-1",
        "asset_id": "token-1",
        "shares": "10",
        "price": "0.5",
        "fee_rate_bps": "0",
        "side": "BUY",
        "execution_role": "MAKER",
        "trader_side": "MAKER",
        "trade_status": "TRADE_STATUS_CONFIRMED",
        "maker_order_ids": ["maker-order-1"],
    }
    value.update(updates)
    return value


def ledger_fields():
    return {
        "sequence_no": 1,
        "ledger_id": "ledger-1",
        "chain_id": "chain-1",
        "orderfilled_event_id": "0xtx:7",
        "entry_ts": "100",
        "cash_available_ts": "110",
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.61",
        "released_cash_since_previous_entry": "0",
        "balance_before": "10",
        "balance_after": "5",
        "previous_checkpoint_id": "opening",
        "balance_checkpoint_id": "balance-1",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "status": "confirmed",
    }


def settlement_fields():
    return {
        "settlement_receipt_id": "settlement-1",
        "cash_release_receipt_id": "cash-release-1",
        "settlement_payout": "5.50",
    }


def rebate_fields():
    return {
        "rebate_status": "CONFIRMED",
        "rebate_amount": "0.11",
        "rebate_batch_id": "rebate-day-1",
        "rebate_receipt_id": "rebate-receipt-day-1",
        "rebate_batch_amount": "1.10",
        "rebate_allocated_amount": "0.11",
        "rebate_available_ts": "110",
    }


class MakerCompoundBundleAdapterTest(unittest.TestCase):
    def test_ten_assembled_rows_reach_the_same_ledger_contract(self):
        rows = []
        fills = []
        for sequence in range(1, 11):
            order = order_event()
            order["order_id"] = f"maker-order-{sequence}"
            trade = trade_event(
                maker_order_ids=[order["order_id"]],
                trade_id=f"trade-{sequence}",
                transaction_ref=f"0xtx{sequence}",
            )
            ledger = ledger_fields()
            before = Decimal("10") + Decimal("0.61") * (sequence - 1)
            ledger.update({
                "sequence_no": sequence,
                "orderfilled_event_id": f"0xtx{sequence}:7",
                "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{sequence - 1}",
                "balance_checkpoint_id": f"balance-{sequence}",
                "entry_ts": str(sequence * 100),
                "cash_available_ts": str(sequence * 100 + 10),
                "released_cash_since_previous_entry": "0" if sequence == 1 else "5.61",
                "balance_before": str(before),
                "balance_after": str(before - Decimal("5")),
            })
            settlement = settlement_fields()
            settlement.update({
                "settlement_receipt_id": f"settlement-{sequence}",
                "cash_release_receipt_id": f"cash-release-{sequence}",
            })
            row = assemble_maker_compound_row(
                order_event=order, trade_event=trade, ledger_fields=ledger,
                settlement_fields=settlement, rebate_fields=rebate_fields(),
                post_only_accepted=True,
                order_updates=[{
                    "kind": "entry_order_accepted",
                    "order_id": order["order_id"],
                    "owner": "owner-key",
                    "trade_ids": [trade["trade_id"]],
                }],
            )
            rows.append(row)
            fills.append({
                "event_id": f"0xtx{sequence}:7", "tx_hash": f"0xtx{sequence}",
                "market_id": "market-1", "maker": "0xmaker", "taker": "0xtaker",
                "side": "BUY", "sign": 1, "size": "10", "cash_amount": "5", "price": "0.5",
            })
        result = validate_maker_compound_chain(
            rows, fills, owner="owner-key", maker_address="0xmaker",
            starting_checkpoint_id="opening", closing_balance_checkpoint_id="closing",
            closing_balance="16.10",
        )
        self.assertTrue(result["offline_consistency_passed"])
        self.assertEqual(result["same_ledger_compounding_rows"], 10)
        self.assertFalse(result["gate_passed"])

    def test_joins_maker_order_trade_and_cash_groups(self):
        row = assemble_maker_compound_row(
            order_event=order_event(), trade_event=trade_event(),
            ledger_fields=ledger_fields(), settlement_fields=settlement_fields(),
            rebate_fields=rebate_fields(), post_only_accepted=True,
            order_updates=[{
                "kind": "entry_order_accepted",
                "order_id": "maker-order-1",
                "owner": "owner-key",
                "trade_ids": ["trade-1"],
            }],
        )
        self.assertEqual(row["owner"], "owner-key")
        self.assertEqual(row["maker_address"], "0xmaker")
        self.assertEqual(row["order_id"], "maker-order-1")
        self.assertEqual(row["trade_id"], "trade-1")
        self.assertEqual(row["execution_role"], "MAKER")
        self.assertTrue(row["post_only_accepted"])
        self.assertEqual(row["settlement_payout"], "5.50")
        self.assertEqual(row["rebate_batch_id"], "rebate-day-1")

    def test_counterparty_taker_order_cannot_be_used_as_maker_order(self):
        with self.assertRaisesRegex(ValueError, "maker_order_not_linked_to_trade"):
            assemble_maker_compound_row(
                order_event=order_event(),
                trade_event=trade_event(maker_order_ids=["other-order"]),
                ledger_fields=ledger_fields(), settlement_fields=settlement_fields(),
                rebate_fields=rebate_fields(), post_only_accepted=True,
            )

    def test_taker_role_and_unconfirmed_trade_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "trade_execution_role_must_be_maker"):
            assemble_maker_compound_row(
                order_event=order_event(), trade_event=trade_event(execution_role="TAKER"),
                ledger_fields=ledger_fields(), settlement_fields=settlement_fields(),
                rebate_fields=rebate_fields(), post_only_accepted=True,
            )
        with self.assertRaisesRegex(ValueError, "trade_status_must_be_confirmed"):
            assemble_maker_compound_row(
                order_event=order_event(), trade_event=trade_event(trade_status="TRADE_STATUS_MATCHED"),
                ledger_fields=ledger_fields(), settlement_fields=settlement_fields(),
                rebate_fields=rebate_fields(), post_only_accepted=True,
            )

    def test_post_only_flag_must_be_explicit_boolean(self):
        with self.assertRaisesRegex(ValueError, "post_only_acceptance_must_be_bool"):
            assemble_maker_compound_row(
                order_event=order_event(), trade_event=trade_event(),
                ledger_fields=ledger_fields(), settlement_fields=settlement_fields(),
                rebate_fields=rebate_fields(), post_only_accepted="true",
            )


if __name__ == "__main__":
    unittest.main()

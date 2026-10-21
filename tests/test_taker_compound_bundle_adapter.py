import unittest
from decimal import Decimal

from analysis.authenticated_compound_reconciler import reconcile_authenticated_chain
from analysis.taker_compound_bundle_adapter import assemble_taker_compound_row


def order_event(status="MATCHED"):
    return {"kind": "entry_order_accepted", "order_id": "taker-order-1", "order_status": status}


def trade_event(**updates):
    value = {
        "kind": "entry_fill",
        "owner": "owner-key",
        "trade_owner": "owner-key",
        "trade_id": "trade-1",
        "transaction_ref": "0xtx",
        "taker_order_id": "taker-order-1",
        "market_id": "market-1",
        "asset_id": "token-1",
        "shares": "10",
        "price": "0.5",
        "side": "BUY",
        "execution_role": "TAKER",
        "trader_side": "TAKER",
        "trade_status": "TRADE_STATUS_CONFIRMED",
    }
    value.update(updates)
    return value


def orderfilled_event(**updates):
    value = {
        "event_id": "0xtx:7",
        "tx_hash": "0xtx",
        "market_id": "market-1",
        "taker": "owner-key",
        "side": "BUY",
        "sign": 1,
        "size": "10",
        "cash_amount": "5",
        "price": "0.5",
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
        "payout": "5.50",
        "status": "confirmed",
        "released_cash_since_previous_entry": "0",
        "balance_before": "10",
        "balance_after": "5",
        "previous_checkpoint_id": "opening",
    }


def settlement_fields():
    return {
        "settlement_receipt_id": "settlement-1",
        "cash_release_receipt_id": "cash-release-1",
        "settlement_status": "CONFIRMED",
        "settlement_transaction_hash": "0xtx",
        "settlement_timestamp": "105",
        "settlement_payout": "5.50",
        "settlement_reverted": False,
        "cash_release_status": "CONFIRMED",
        "cash_release_transaction_hash": "0xrelease",
        "cash_release_timestamp": "110",
        "cash_release_amount": "5.50",
    }


def balance_fields():
    return {
        "balance_checkpoint_id": "balance-1",
        "cash_reconciled": True,
        "independent_checkpoint": True,
    }


class TakerCompoundBundleAdapterTest(unittest.TestCase):
    def assemble(self, **updates):
        trade_updates = updates.pop("trade", {})
        fill_updates = updates.pop("fill", {})
        return assemble_taker_compound_row(
            order_event=updates.pop("order", order_event()),
            trade_event=trade_event(**trade_updates),
            orderfilled_event=orderfilled_event(**fill_updates),
            ledger_fields=updates.pop("ledger", ledger_fields()),
            settlement_fields=updates.pop("settlement", settlement_fields()),
            balance_fields=updates.pop("balance", balance_fields()),
        )

    def test_joins_taker_trade_orderfilled_and_cash_groups(self):
        row = self.assemble()
        self.assertEqual(row["owner"], "owner-key")
        self.assertEqual(row["execution_role"], "TAKER")
        self.assertEqual(row["orderfilled_event_id"], "0xtx:7")
        self.assertEqual(row["settlement_receipt_id"], "settlement-1")
        self.assertEqual(row["cash_release_receipt_id"], "cash-release-1")

    def test_onchain_identity_and_amounts_must_match_trade(self):
        with self.assertRaisesRegex(ValueError, "orderfilled_transaction_mismatch"):
            self.assemble(fill={"tx_hash": "0xother"})
        with self.assertRaisesRegex(ValueError, "orderfilled_size_mismatch"):
            self.assemble(fill={"size": "9"})
        with self.assertRaisesRegex(ValueError, "entry_cost_mismatch"):
            self.assemble(fill={"cash_amount": "5.1"})

    def test_owner_and_order_link_are_required(self):
        with self.assertRaisesRegex(ValueError, "orderfilled_taker_owner_mismatch"):
            self.assemble(fill={"taker": "other-owner"})
        with self.assertRaisesRegex(ValueError, "taker_order_not_linked_to_trade"):
            self.assemble(trade={"taker_order_id": "other-order"})

    def test_unconfirmed_or_wrong_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "trade_status_must_be_confirmed"):
            self.assemble(trade={"trade_status": "TRADE_STATUS_MATCHED"})
        with self.assertRaisesRegex(ValueError, "trade_execution_role_must_be_taker"):
            self.assemble(trade={"execution_role": "MAKER"})
        with self.assertRaisesRegex(ValueError, "order_status_must_be_matched"):
            self.assemble(order={"order_status": "LIVE"})

    def test_settlement_and_release_metadata_are_bound_to_the_row(self):
        bad = settlement_fields()
        bad["settlement_status"] = "MINED"
        with self.assertRaisesRegex(ValueError, "settlement_status_must_be_confirmed"):
            self.assemble(settlement=bad)
        bad = settlement_fields()
        bad["settlement_transaction_hash"] = "0xother"
        with self.assertRaisesRegex(ValueError, "settlement_transaction_mismatch"):
            self.assemble(settlement=bad)
        bad = settlement_fields()
        bad["settlement_reverted"] = True
        with self.assertRaisesRegex(ValueError, "settlement_must_not_be_reverted"):
            self.assemble(settlement=bad)
        bad = settlement_fields()
        bad["cash_release_amount"] = "5.49"
        with self.assertRaisesRegex(ValueError, "cash_release_amount_mismatch"):
            self.assemble(settlement=bad)
        bad = settlement_fields()
        bad["cash_release_timestamp"] = "111"
        with self.assertRaisesRegex(ValueError, "cash_release_timestamp_mismatch"):
            self.assemble(settlement=bad)

    def test_settlement_and_cash_release_receipts_must_be_distinct(self):
        bad = settlement_fields()
        bad.update({
            "settlement_receipt_id": "same",
            "cash_release_receipt_id": "same",
        })
        with self.assertRaisesRegex(ValueError, "cash_release_receipt_not_independent"):
            self.assemble(settlement=bad)

    def test_ten_assembled_rows_pass_the_continuous_cash_reconciler(self):
        rows = []
        for sequence in range(1, 11):
            order = order_event()
            order["order_id"] = f"taker-order-{sequence}"
            trade = trade_event(
                taker_order_id=order["order_id"],
                trade_id=f"trade-{sequence}",
                transaction_ref=f"0xtx{sequence}",
            )
            fill = orderfilled_event(
                event_id=f"0xtx{sequence}:7",
                tx_hash=f"0xtx{sequence}",
            )
            ledger = ledger_fields()
            before = Decimal("10") + Decimal("0.50") * (sequence - 1)
            ledger.update({
                "sequence_no": sequence,
                "orderfilled_event_id": f"0xtx{sequence}:7",
                "previous_checkpoint_id": "opening" if sequence == 1 else f"balance-{sequence - 1}",
                "entry_ts": str(sequence * 100),
                "cash_available_ts": str(sequence * 100 + 10),
                "released_cash_since_previous_entry": "0" if sequence == 1 else "5.50",
                "balance_before": str(before),
                "balance_after": str(before - Decimal("5")),
            })
            settlement = {
                "settlement_receipt_id": f"settlement-{sequence}",
                "cash_release_receipt_id": f"cash-release-{sequence}",
                "settlement_status": "CONFIRMED",
                "settlement_transaction_hash": f"0xtx{sequence}",
                "settlement_timestamp": str(sequence * 100 + 5),
                "settlement_payout": "5.50",
                "settlement_reverted": False,
                "cash_release_status": "CONFIRMED",
                "cash_release_transaction_hash": f"0xrelease{sequence}",
                "cash_release_timestamp": str(sequence * 100 + 10),
                "cash_release_amount": "5.50",
            }
            balance = {
                "balance_checkpoint_id": f"balance-{sequence}",
                "cash_reconciled": True,
                "independent_checkpoint": True,
            }
            rows.append(assemble_taker_compound_row(
                order_event=order,
                trade_event=trade,
                orderfilled_event=fill,
                ledger_fields=ledger,
                settlement_fields=settlement,
                balance_fields=balance,
            ))
        result = reconcile_authenticated_chain(
            rows,
            owner="owner-key",
            initial_cash="10",
            minimum_order="5",
            required_fills=10,
            starting_checkpoint_id="opening",
            closing_balance_checkpoint_id="closing",
            closing_balance="15.00",
        )
        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["fills"], 10)
        self.assertEqual(result["ending_cash"], "15.00")


if __name__ == "__main__":
    unittest.main()

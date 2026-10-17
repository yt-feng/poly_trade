"""Synthetic user-stream shapes only; no account, network or order access."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from polymarket_receipt_adapter import normalize_user_event


class WireAdapterTests(unittest.TestCase):
    def order(self, **updates):
        value = {
            "event_type": "order", "id": "order-1", "market": "condition-1",
            "asset_id": "token-up", "original_size": "5", "size_matched": "0",
            "status": "LIVE", "type": "PLACEMENT",
        }
        value.update(updates)
        return value

    def trade(self, **updates):
        value = {
            "event_type": "trade", "id": "trade-1", "taker_order_id": "order-1",
            "market": "condition-1", "asset_id": "token-up", "size": "5",
            "status": "MATCHED", "transaction_hash": None,
        }
        value.update(updates)
        return value

    def test_order_wire_names_are_normalized_without_inventing_fill(self):
        out = normalize_user_event(self.order(size_matched="2"))
        self.assertEqual(out["condition_id"], "condition-1")
        self.assertEqual(out["token_id"], "token-up")
        self.assertEqual(out["matched_shares"], "2")
        self.assertEqual(out["confirmed_filled_shares"], "0")
        self.assertFalse(out["private_fill_inferred"])

    def test_wire_order_lifecycle_is_retained_without_fill_or_cash_evidence(self):
        for lifecycle, status in (("PLACEMENT", "LIVE"), ("UPDATE", "MATCHED"),
                                  ("CANCELLATION", "CANCELED")):
            with self.subTest(lifecycle=lifecycle):
                out = normalize_user_event(self.order(type=lifecycle, status=status, size_matched="2"))
                self.assertEqual(out["order_event_type"], lifecycle)
                self.assertEqual(out["matched_shares"], "2")
                self.assertEqual(out["confirmed_filled_shares"], "0")
                self.assertEqual(out["order_terminal"], status == "CANCELED")
                self.assertFalse(out["private_fill_inferred"])
                self.assertNotIn("cash_reconciled", out)
                self.assertNotIn("trade_confirmed", out)

    def test_sdk_order_aliases_take_precedence_over_wire_type(self):
        for alias in ("orderEventType", "order_event_type"):
            for family in ("order", "order_update"):
                for lifecycle in ("PLACEMENT", "UPDATE", "CANCELLATION"):
                    with self.subTest(alias=alias, family=family, lifecycle=lifecycle):
                        body = self.order(**{alias: lifecycle, "type": "UPDATE"})
                        out = normalize_user_event({"type": family, "payload": body})
                        self.assertEqual(out["order_event_type"], lifecycle)
                        self.assertEqual(out["confirmed_filled_shares"], "0")
        out = normalize_user_event(self.order(orderEventType="UPDATE", order_event_type="CANCELLATION"))
        self.assertEqual(out["order_event_type"], "UPDATE")

    def test_explicit_empty_or_unknown_alias_does_not_infer_a_wire_lifecycle(self):
        for alias in ("orderEventType", "order_event_type"):
            for value, expected in ((None, ""), ("", ""), ("future_status", "FUTURE_STATUS")):
                with self.subTest(alias=alias, value=value):
                    out = normalize_user_event(self.order(**{alias: value, "type": "CANCELLATION"}))
                    self.assertEqual(out["order_event_type"], expected)

    def test_family_only_and_unknown_types_are_not_order_lifecycles(self):
        for event_type in ("order", "order_update", "trade", "trade_update", "FUTURE", "", None):
            with self.subTest(type=event_type):
                body = self.order(type=event_type)
                for event in (body, {"type": "order", "payload": body}):
                    out = normalize_user_event(event)
                    self.assertEqual(out["order_event_type"], "")
        for family in ("order", "order_update"):
            body = self.order()
            del body["type"]
            out = normalize_user_event({"type": family, "payload": body})
            self.assertEqual(out["order_event_type"], "")

    def test_wire_lifecycle_uses_existing_case_and_whitespace_normalization(self):
        out = normalize_user_event(self.order(type=" cancellation "))
        self.assertEqual(out["order_event_type"], "CANCELLATION")

    def test_pending_wire_and_typescript_trade_states_never_confirm(self):
        for state in ("MATCHED", "MATCHED_NOT_BROADCASTED", "MINED", "RETRYING"):
            for prefix in ("", "TRADE_STATUS_"):
                with self.subTest(status=prefix + state):
                    out = normalize_user_event({"type": "trade", "payload": {
                        "id": "synthetic-trade", "takerOrderId": "synthetic-order",
                        "conditionId": "synthetic-condition", "assetId": "synthetic-token",
                        "size": "2.5", "status": prefix + state, "transactionHash": "synthetic-tx",
                    }})
                    self.assertEqual(out["trade_status"], prefix + state)
                    self.assertTrue(out["trade_pending"])
                    self.assertFalse(out["trade_confirmed"])
                    self.assertFalse(out["trade_failed"])
                    self.assertEqual(out["matched_shares"], "2.5")
                    self.assertEqual(out["confirmed_filled_shares"], "0")
                    self.assertFalse(out["private_fill_inferred"])
                    self.assertNotIn("cash_reconciled", out)

    def test_terminal_and_unknown_trade_states_are_not_pending(self):
        for prefix in ("", "TRADE_STATUS_"):
            for state in ("CONFIRMED", "FAILED", "UNKNOWN"):
                with self.subTest(status=prefix + state):
                    out = normalize_user_event(self.trade(status=prefix + state))
                    self.assertFalse(out["trade_pending"])
                    self.assertEqual(out["trade_confirmed"], state == "CONFIRMED")
                    self.assertEqual(out["trade_failed"], state == "FAILED")
                    self.assertEqual(out["confirmed_filled_shares"], "5" if state == "CONFIRMED" else "0")

    def test_matched_trade_is_not_confirmed(self):
        out = normalize_user_event(self.trade())
        self.assertEqual(out["submission_reference"], "order-1")
        self.assertEqual(out["trade_id"], "trade-1")
        self.assertEqual(out["matched_shares"], "5")
        self.assertEqual(out["confirmed_filled_shares"], "0")
        self.assertTrue(out["trade_pending"])
        self.assertFalse(out["trade_confirmed"])

    def test_confirmed_trade_carries_transaction_reference(self):
        out = normalize_user_event(self.trade(status="CONFIRMED", transaction_hash="0xtx"))
        self.assertEqual(out["confirmed_filled_shares"], "5")
        self.assertTrue(out["trade_confirmed"])
        self.assertEqual(out["transaction_ref"], "0xtx")

    def test_nested_sdk_trade_uses_taker_order_identity(self):
        out = normalize_user_event({
            "type": "trade",
            "payload": {
                "id": "trade-2", "takerOrderId": "order-2",
                "conditionId": "condition-2", "assetId": "token-down",
                "size": "5", "status": "TRADE_STATUS_CONFIRMED",
            },
        })
        self.assertEqual(out["submission_reference"], "order-2")
        self.assertEqual(out["trade_id"], "trade-2")
        self.assertEqual(out["confirmed_filled_shares"], "5")

    def test_failed_trade_never_confirms(self):
        out = normalize_user_event(self.trade(status="FAILED", transaction_hash="0xtx"))
        self.assertEqual(out["confirmed_filled_shares"], "0")
        self.assertTrue(out["trade_failed"])

    def test_unknown_event_fails_closed(self):
        with self.assertRaises(ValueError):
            normalize_user_event({"event_type": "balance", "id": "x"})

    def test_incomplete_identity_fails_closed(self):
        with self.assertRaises(ValueError):
            normalize_user_event(self.order(asset_id=""))


if __name__ == "__main__":
    unittest.main()

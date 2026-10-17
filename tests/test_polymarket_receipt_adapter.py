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

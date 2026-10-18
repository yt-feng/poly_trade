import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from polymarket_user_stream_adapter import (  # noqa: E402
    adapt_user_stream_frame,
    adapt_user_stream_frames,
)


class PolymarketUserStreamAdapter(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "market_id": "market-1",
            "token_id": "token-1",
            "owner": "account-1",
            "order_id": "order-1",
        }

    def order_frame(self, status="LIVE"):
        return {
            "topic": "user",
            "type": "order",
            "payload": {
                "id": "order-1",
                "owner": "account-1",
                "conditionId": "market-1",
                "assetId": "token-1",
                "side": "BUY",
                "originalSize": "10",
                "sizeMatched": "0",
                "price": "0.52",
                "orderEventType": "PLACEMENT",
                "status": status,
                "tradeIDs": ["trade-1"],
                "transactionsHashes": ["0xsettlement"],
                "timestamp": 1782753357257,
            },
        }

    def trade_frame(self, status="TRADE_STATUS_CONFIRMED"):
        return {
            "topic": "user",
            "type": "trade",
            "payload": {
                "id": "trade-1",
                "takerOrderId": "order-1",
                "conditionId": "market-1",
                "assetId": "token-1",
                "side": "BUY",
                "size": "5",
                "price": "0.52",
                "status": status,
                "owner": "account-1",
                "tradeOwner": "account-1",
                "makerAddress": "maker-1",
                "feeRateBps": "7",
                "matchedAt": "2026-06-29T17:15:57.257000Z",
                "updatedAt": "2026-06-29T17:15:58.257000Z",
                "transactionHash": "0xtx",
                "traderSide": "TAKER",
                "makerOrders": [],
                "timestamp": 1782753357257,
            },
        }

    def test_documented_order_wire_shape_is_adapted(self):
        result = adapt_user_stream_frame(self.order_frame(), expected=self.expected)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["raw_event_type"], "order")
        self.assertEqual(result["dedupe_key"], "order:order-1:LIVE:1782753357257")
        self.assertEqual([event["kind"] for event in result["events"]], ["entry_order_accepted"])
        self.assertEqual(result["events"][0]["trade_ids"], ["trade-1"])
        self.assertEqual(result["events"][0]["transaction_hashes"], ["0xsettlement"])

    def test_documented_trade_wire_shape_emits_confirmed_fill(self):
        result = adapt_user_stream_frame(self.trade_frame(), expected=self.expected)
        self.assertEqual(result["issues"], [])
        self.assertEqual([event["kind"] for event in result["events"]], ["entry_fill"])
        self.assertEqual(result["events"][0]["trade_id"], "trade-1")
        self.assertEqual(result["events"][0]["owner"], "account-1")
        self.assertEqual(result["events"][0]["market_id"], "market-1")
        self.assertEqual(result["events"][0]["asset_id"], "token-1")
        self.assertEqual(result["events"][0]["taker_order_id"], "order-1")
        self.assertEqual(result["events"][0]["shares"], "5")
        self.assertEqual(result["events"][0]["execution_role"], "TAKER")
        self.assertEqual(result["events"][0]["price"], "0.52")
        self.assertEqual(result["events"][0]["timestamp"], "1782753357257")

    def test_duplicate_frame_is_idempotent_by_type_and_payload_id(self):
        seen = set()
        first = adapt_user_stream_frame(
            self.trade_frame(), expected=self.expected, seen_event_ids=seen
        )
        second = adapt_user_stream_frame(
            copy.deepcopy(self.trade_frame()), expected=self.expected, seen_event_ids=seen
        )
        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(second["events"], [])
        self.assertTrue(second["duplicate"])
        self.assertEqual(seen, {"trade:trade-1:TRADE_STATUS_CONFIRMED:2026-06-29T17:15:58.257000Z"})

    def test_status_progression_for_one_trade_reaches_confirmation(self):
        seen = set()
        matched = adapt_user_stream_frame(
            self.trade_frame("TRADE_STATUS_MATCHED"),
            expected=self.expected,
            seen_event_ids=seen,
        )
        confirmed = self.trade_frame("TRADE_STATUS_CONFIRMED")
        confirmed["payload"]["updatedAt"] = "2026-06-29T17:16:00.257000Z"
        confirmed_result = adapt_user_stream_frame(
            confirmed,
            expected=self.expected,
            seen_event_ids=seen,
        )
        self.assertIn("TRADE_NOT_CONFIRMED", {issue["code"] for issue in matched["issues"]})
        self.assertEqual([event["kind"] for event in confirmed_result["events"]], ["entry_fill"])
        self.assertFalse(confirmed_result["duplicate"])

    def test_stream_slice_collapses_duplicates_and_preserves_issues(self):
        result = adapt_user_stream_frames(
            [self.order_frame("DELAYED"), self.order_frame("DELAYED"), self.trade_frame()],
            expected=self.expected,
        )
        self.assertEqual(result["frames_seen"], 3)
        self.assertEqual(result["duplicate_frames"], 1)
        self.assertEqual([event["kind"] for event in result["events"]], ["entry_order_accepted", "entry_fill"])
        self.assertEqual(result["issues"], [])

    def test_public_row_and_wrong_topic_are_rejected(self):
        public = {
            "topic": "market",
            "type": "trade",
            "payload": {"id": "trade-1", "conditionId": "market-1"},
        }
        result = adapt_user_stream_frame(public, expected=self.expected)
        self.assertEqual(result["events"], [])
        self.assertIn("UNAUTHENTICATED_USER_TOPIC", {issue["code"] for issue in result["issues"]})

        user_public = self.trade_frame()
        del user_public["payload"]["owner"]
        result = adapt_user_stream_frame(user_public, expected=self.expected)
        self.assertEqual(result["events"], [])
        self.assertIn("MISSING_OWNER_SCOPE", {issue["code"] for issue in result["issues"]})

    def test_unknown_type_and_malformed_payload_fail_closed(self):
        unknown = {"topic": "user", "type": "balance", "payload": {"id": "b-1"}}
        result = adapt_user_stream_frame(unknown, expected=self.expected)
        self.assertIn("UNKNOWN_USER_EVENT_TYPE", {issue["code"] for issue in result["issues"]})

        malformed = {"topic": "user", "type": "trade", "payload": []}
        result = adapt_user_stream_frame(malformed, expected=self.expected)
        self.assertIn("INVALID_USER_PAYLOAD", {issue["code"] for issue in result["issues"]})

    def test_maker_trade_preserves_role_and_zero_fee_requirement(self):
        frame = self.trade_frame()
        frame["payload"].update({"traderSide": "MAKER", "feeRateBps": "0"})
        result = adapt_user_stream_frame(
            frame, expected=self.expected, required_trader_side="MAKER"
        )
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["events"][0]["trader_side"], "MAKER")
        self.assertEqual(result["events"][0]["fee_rate_bps"], "0")

    def test_missing_payload_id_is_rejected_before_dedupe(self):
        frame = self.trade_frame()
        del frame["payload"]["id"]
        seen = set()
        result = adapt_user_stream_frame(
            frame, expected=self.expected, seen_event_ids=seen
        )
        self.assertIn("MISSING_USER_EVENT_ID", {issue["code"] for issue in result["issues"]})
        self.assertEqual(seen, set())


if __name__ == "__main__":
    unittest.main()


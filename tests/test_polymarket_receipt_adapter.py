import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from canary_receipt_state import apply_event, new_episode
from polymarket_receipt_adapter import adapt_account_snapshot, adapt_order, adapt_trade, normalize_user_event


class PolymarketReceiptAdapter(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "market_id": "market-1",
            "token_id": "token-1",
            "owner": "account-1",
            "order_id": "order-1",
        }

    def order(self, status="LIVE"):
        return {
            "id": "order-1",
            "market": "market-1",
            "asset_id": "token-1",
            "owner": "account-1",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "5",
            "status": status,
            "associate_trades": ["trade-1"],
            "tradeIDs": ["trade-1"],
            "transactionsHashes": [],
        }

    def trade(self, status="TRADE_STATUS_CONFIRMED", tx="0xtx"):
        return {
            "id": "trade-1",
            "taker_order_id": "order-1",
            "market": "market-1",
            "asset_id": "token-1",
            "owner": "account-1",
            "side": "BUY",
            "size": "5",
            "status": status,
            "trader_side": "TAKER",
            "fee_rate_bps": "7",
            "maker_address": "maker-1",
            "trade_owner": "account-1",
            "match_time": "1700000000",
            "last_update": "1700000001",
            "transaction_hash": tx,
            "maker_orders": [],
        }

    def test_order_keeps_share_quantities_and_acceptance_is_not_a_fill(self):
        result = adapt_order(self.order(), expected=self.expected)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["order"]["size_matched"], "5")
        self.assertEqual(result["order"]["trade_ids"], ["trade-1"])
        self.assertEqual([event["kind"] for event in result["events"]], ["entry_order_accepted"])
        self.assertEqual(result["events"][0]["trade_ids"], ["trade-1"])
        self.assertEqual(result["events"][0]["transaction_hashes"], [])

    def test_order_preserves_best_effort_transaction_hash_without_promoting_fill(self):
        payload = self.order()
        payload["transactionsHashes"] = ["0xsettlement"]
        result = adapt_order(payload, expected=self.expected)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["order"]["transaction_hashes"], ["0xsettlement"])
        self.assertEqual([event["kind"] for event in result["events"]], ["entry_order_accepted"])
        self.assertEqual(result["events"][0]["transaction_hashes"], ["0xsettlement"])

    def test_matched_order_emits_terminal_event(self):
        result = adapt_order(self.order("MATCHED"), expected=self.expected)
        self.assertEqual(result["issues"], [])
        self.assertEqual(
            [event["kind"] for event in result["events"]],
            ["entry_order_accepted", "entry_order_terminal"],
        )

    def test_invalid_order_is_rejection_without_acceptance(self):
        result = adapt_order(self.order("INVALID"), expected=self.expected)
        self.assertEqual(result["issues"], [])
        self.assertEqual([event["kind"] for event in result["events"]], ["entry_order_rejected"])

    def test_canceled_order_does_not_hide_associated_confirmed_trade(self):
        result = adapt_account_snapshot(
            order=self.order("CANCELED"), trades=[self.trade()], expected=self.expected
        )
        self.assertEqual(result["issues"], [])
        self.assertEqual(
            [event["kind"] for event in result["events"]],
            ["entry_order_accepted", "entry_order_terminal", "entry_fill"],
        )

    def test_duplicate_trade_is_idempotent(self):
        result = adapt_account_snapshot(
            order=self.order(),
            trades=[self.trade(), copy.deepcopy(self.trade())],
            expected=self.expected,
        )
        self.assertEqual(sum(event["kind"] == "entry_fill" for event in result["events"]), 1)

    def test_matched_or_missing_hash_never_becomes_confirmed_fill(self):
        matched = adapt_trade(self.trade("TRADE_STATUS_MATCHED"), expected=self.expected)
        self.assertEqual(matched["events"], [])
        self.assertIn("TRADE_NOT_CONFIRMED", {issue["code"] for issue in matched["issues"]})

        missing_hash = adapt_trade(self.trade(tx=""), expected=self.expected)
        self.assertEqual(missing_hash["events"], [])
        self.assertIn("MISSING_TRANSACTION_HASH", {issue["code"] for issue in missing_hash["issues"]})

    def test_public_data_api_row_is_not_an_account_receipt(self):
        public_row = {
            "proxy_wallet": "account-1",
            "condition_id": "market-1",
            "token_id": "token-1",
            "side": "BUY",
            "size": "5",
            "price": "0.5",
            "transaction_hash": "0xtx",
        }
        result = adapt_trade(public_row, expected=self.expected)
        self.assertEqual(result["events"], [])
        self.assertIn("MISSING_OWNER_SCOPE", {issue["code"] for issue in result["issues"]})

    def test_identity_mismatch_is_rejected_before_event(self):
        bad = self.trade()
        bad["asset_id"] = "other-token"
        result = adapt_trade(bad, expected=self.expected)
        self.assertEqual(result["events"], [])
        self.assertIn("IDENTITY_MISMATCH", {issue["code"] for issue in result["issues"]})

    def test_confirmed_adapter_events_fit_receipt_state(self):
        result = adapt_account_snapshot(
            order=self.order("MATCHED"), trades=[self.trade()], expected=self.expected
        )
        state = new_episode(
            local_id="ep-1", token_id="token-1", min_sell_size="5", expected_cash_asset="USDC"
        )
        for event in result["events"]:
            state = apply_event(state, event)
        self.assertEqual(state["entry_trade_ids"], ["trade-1"])
        self.assertEqual(state["position_net_shares"], "5")
        self.assertFalse(state["cash_reusable"])

    def test_maker_receipt_requires_role_and_zero_fee_evidence(self):
        maker = self.trade()
        maker.update(trader_side="MAKER", fee_rate_bps="0")
        result = adapt_account_snapshot(
            order=self.order("MATCHED"),
            trades=[maker],
            expected=self.expected,
            required_trader_side="MAKER",
        )
        self.assertEqual(result["issues"], [])
        fill = result["events"][-1]
        self.assertEqual(fill["trader_side"], "MAKER")
        self.assertEqual(fill["fee_rate_bps"], "0")

    def test_maker_requirement_rejects_taker_trade(self):
        result = adapt_trade(
            self.trade(), expected=self.expected, required_trader_side="MAKER"
        )
        self.assertEqual(result["events"], [])
        self.assertIn("TRADER_SIDE_MISMATCH", {issue["code"] for issue in result["issues"]})

    def test_maker_requirement_rejects_nonzero_fee(self):
        maker = self.trade()
        maker.update(trader_side="MAKER", fee_rate_bps="7")
        result = adapt_trade(
            maker, expected=self.expected, required_trader_side="MAKER"
        )
        self.assertEqual(result["events"], [])
        self.assertIn("MAKER_FEE_NOT_ZERO", {issue["code"] for issue in result["issues"]})


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


if __name__ == "__main__":
    unittest.main()

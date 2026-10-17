import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from canary_receipt_state import apply_event, new_episode
from polymarket_receipt_adapter import adapt_account_snapshot, adapt_order, adapt_trade


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
        self.assertEqual([event["kind"] for event in result["events"]], ["entry_order_accepted"])

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


if __name__ == "__main__":
    unittest.main()


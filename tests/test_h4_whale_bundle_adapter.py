import unittest

from analysis.h4_whale_bundle_adapter import assemble_h4_whale_row


def groups():
    return {
        "market": {
            "token_id": "token-1", "condition_id": "condition-1", "market_slug": "btc-updown-5m-1",
            "outcome_index": 0, "outcome_name": "Up", "source_version": "gamma-v2",
            "identity_hash": "identity-1", "market_join_verified": True,
        },
        "fill": {
            "owner": "owner-1", "transaction_hash": "tx-1", "block_number": 1, "log_index": 0,
            "trade_id": "trade-1", "order_hash": "order-1", "maker_address": "maker-1",
            "taker_address": "owner-1", "trader_side": "TAKER", "tx_status": "CONFIRMED",
            "filled_shares": "10", "fill_price": "0.5", "fee": "0.1",
            "fee_schedule_source": "clob-market-info-v2", "fee_schedule_observed_ts": 950, "entry_ts": 1000,
        },
        "whale": {
            "whale_address": "whale-1", "whale_trade_id": "whale-trade-1",
            "whale_transaction_hash": "whale-tx-1", "whale_notional_usdc": "1000000",
            "whale_observed_ts": 900, "whale_observation_verified": True,
        },
        "settlement": {
            "settlement_receipt_id": "settlement-1", "settlement_transaction_hash": "settle-tx-1",
            "settlement_status": "CONFIRMED", "cash_release_receipt_id": "release-1",
            "cash_release_status": "CONFIRMED",
        },
        "ledger": {
            "sequence_no": 1, "ledger_id": "ledger-1", "chain_id": "chain-1",
            "previous_checkpoint_id": "opening", "balance_checkpoint_id": "balance-1",
        },
    }


class H4WhaleBundleAdapterTests(unittest.TestCase):
    def test_assembles_canonical_row(self):
        row = assemble_h4_whale_row(**groups())
        self.assertEqual(row["trader_side"], "TAKER")
        self.assertEqual(row["owner"], "owner-1")
        self.assertEqual(row["market_slug"], "btc-updown-5m-1")
        self.assertTrue(row["market_join_verified"])

    def test_rejects_maker_side(self):
        evidence = groups()
        evidence["fill"]["trader_side"] = "MAKER"
        with self.assertRaisesRegex(ValueError, "fill_trader_side_must_be_taker"):
            assemble_h4_whale_row(**evidence)

    def test_rejects_unverified_market_join(self):
        evidence = groups()
        evidence["market"]["market_join_verified"] = False
        with self.assertRaisesRegex(ValueError, "market_join_must_be_verified"):
            assemble_h4_whale_row(**evidence)

    def test_rejects_reserved_field_in_ledger(self):
        evidence = groups()
        evidence["ledger"]["token_id"] = "wrong-token"
        with self.assertRaisesRegex(ValueError, "ledger_contains_reserved_field:token_id"):
            assemble_h4_whale_row(**evidence)

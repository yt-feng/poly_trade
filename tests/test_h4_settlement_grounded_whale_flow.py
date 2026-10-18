from decimal import Decimal
import unittest

from analysis.h4_settlement_grounded_whale_flow import audit_h4_source_schema, validate_h4_chain


def bundle():
    rows = []
    previous_after = Decimal("10")
    for i in range(1, 11):
        released = Decimal("0") if i == 1 else Decimal("5.6")
        before = previous_after + released
        after = before - Decimal("5") - Decimal("0.1")
        ts = i * 1000
        row = {
            "sequence_no": i,
            "ledger_id": "ledger-1",
            "chain_id": "chain-1",
            "owner": "owner-1",
            "token_id": f"token-{i}",
            "condition_id": f"condition-{i}",
            "market_slug": f"btc-updown-5m-{i}",
            "outcome_index": 0,
            "outcome_name": "Up",
            "source_version": "gamma-v2",
            "identity_hash": f"identity-{i}",
            "market_join_verified": True,
            "whale_address": "whale-1",
            "whale_trade_id": f"whale-trade-{i}",
            "whale_transaction_hash": f"whale-tx-{i}",
            "whale_notional_usdc": "1000000",
            "whale_observed_ts": ts - 100,
            "whale_observation_verified": True,
            "transaction_hash": f"tx-{i}",
            "block_number": i,
            "log_index": 0,
            "trade_id": f"trade-{i}",
            "order_hash": f"order-{i}",
            "maker_address": f"maker-{i}",
            "taker_address": "owner-1",
            "trader_side": "TAKER",
            "tx_status": "CONFIRMED",
            "filled_shares": "10",
            "fill_price": "0.50",
            "settlement_receipt_id": f"settlement-{i}",
            "settlement_transaction_hash": f"settlement-tx-{i}",
            "settlement_status": "CONFIRMED",
            "resolved_outcome": "Up",
            "cash_release_receipt_id": f"release-{i}",
            "cash_release_status": "CONFIRMED",
            "entry_ts": ts,
            "cash_available_ts": ts + 100,
            "entry_cost": "5",
            "fee": "0.1",
            "payout": "5.6",
            "balance_before": str(before),
            "balance_after": str(after),
            "released_cash_since_previous_entry": str(released),
            "balance_checkpoint_id": f"balance-{i}",
            "previous_checkpoint_id": "opening" if i == 1 else f"balance-{i-1}",
        }
        rows.append(row)
        previous_after = after
    return rows


class H4SettlementGroundedWhaleFlowTests(unittest.TestCase):
    def validate(self, rows):
        return validate_h4_chain(
            rows,
            owner="owner-1",
            opening_checkpoint_id="opening",
            closing_checkpoint_id="closing",
            closing_balance="15.0",
        )

    def test_h4_accepts_only_offline_consistent_taker_whale_chain(self):
        result = self.validate(bundle())
        self.assertIs(result["offline_consistency_passed"], True)
        self.assertEqual(result["same_ledger_compounding_rows"], 10)
        self.assertEqual(result["actual_fills"], 0)
        self.assertEqual(result["verified_canary_fills"], 0)
        self.assertIs(result["gate_passed"], False)

    def test_source_schema_preflight_rejects_orderfilled_without_market_join(self):
        result = audit_h4_source_schema([{
            "tx_hash": "tx-1", "log_index": 0, "block_number": 1,
            "maker": "maker", "taker": "taker", "size": "10", "price": "0.5",
            "token_id": "token-1", "side": "BUY",
        }])
        self.assertEqual(result["record_count"], 1)
        self.assertIs(result["token_market_join_ready"], False)
        self.assertIs(result["h4_source_ready"], False)
        self.assertIn("condition_id", result["missing_by_group"]["token_market_join"])
        self.assertEqual(result["same_ledger_compounding_rows"], 0)


    def test_h4_rejects_quote_only_row(self):
        rows = bundle()
        rows[0]["transaction_hash"] = ""
        result = self.validate(rows)
        self.assertEqual(result["failure_reason"], "h4_event_contract_failed")
        self.assertTrue(any(item["reason"] == "transaction_hash_required" for item in result["issues"]))

    def test_h4_rejects_missing_settlement_status(self):
        rows = bundle()
        rows[0].pop("settlement_status")
        result = self.validate(rows)
        self.assertEqual(result["failure_reason"], "h4_event_contract_failed")
        self.assertTrue(any(item["reason"] == "settlement_status_required" for item in result["issues"]))


    def test_h4_rejects_maker_side_and_owner_mismatch(self):
        rows = bundle()
        rows[2]["trader_side"] = "MAKER"
        result = self.validate(rows)
        self.assertEqual(result["failure_reason"], "h4_event_contract_failed")
        self.assertTrue(any(item["reason"] == "trader_side_must_be_taker" for item in result["issues"]))


    def test_h4_rejects_duplicate_or_reordered_rows(self):
        rows = bundle()
        rows[1]["transaction_hash"] = rows[0]["transaction_hash"]
        rows[1]["sequence_no"] = 3
        result = self.validate(rows)
        self.assertEqual(result["failure_reason"], "h4_event_contract_failed")
        self.assertTrue(any(item["reason"] == "duplicate_transaction_hash" for item in result["issues"]))
        self.assertTrue(any(item["reason"] == "sequence_no_must_be_exactly_one_to_ten" for item in result["issues"]))

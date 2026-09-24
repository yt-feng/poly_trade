import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from execution_reason_codes import classify, summarize


def base(**changes):
    r = {
        "signal_qualified": True,
        "planned_shares": "5",
        "min_order_size": "5",
        "planned_all_in_usd": "2.6",
        "available_cash_usd": "10",
        "total_cash_usd": "10",
        "reserved_cash_usd": "0",
        "decision_to_submit_ms": "50",
        "max_decision_to_submit_ms": "250",
        "quote_age_ms": "100",
        "max_quote_age_ms": "1500",
        "current_executable_ask": "0.50",
        "entry_price_ceiling": "0.51",
        "displayed_ask_depth_shares": "10",
        "order_status": "ACCEPTED",
        "confirmed_filled_shares": "0",
        "requested_shares": "5",
        "order_terminal": False,
        "entry_order_terminal": False,
    }
    r.update(changes)
    return r


class ReasonCodes(unittest.TestCase):
    def code(self, **changes):
        return classify(base(**changes))["reason_code"]

    def test_no_opportunity_is_not_no_fill(self):
        self.assertEqual(self.code(signal_qualified=False), "NO_QUALIFIED_OPPORTUNITY")

    def test_algo_below_exchange_minimum(self):
        self.assertEqual(self.code(planned_shares="4.99"), "ALGO_SIZE_BELOW_EXCHANGE_MINIMUM")

    def test_total_cash_shortage(self):
        self.assertEqual(self.code(planned_all_in_usd="12"), "INSUFFICIENT_TOTAL_CASH")

    def test_reserved_cash_is_separate(self):
        self.assertEqual(
            self.code(planned_all_in_usd="6", total_cash_usd="20", available_cash_usd="4", reserved_cash_usd="16"),
            "AVAILABLE_CASH_RESERVED_OR_FROZEN",
        )

    def test_available_cash_shortage_without_reservation(self):
        self.assertEqual(
            self.code(planned_all_in_usd="6", total_cash_usd="20", available_cash_usd="4"),
            "INSUFFICIENT_AVAILABLE_CASH",
        )

    def test_compute_send_delay(self):
        self.assertEqual(self.code(decision_to_submit_ms="251"), "COMPUTE_OR_SEND_DELAY_EXCEEDED")

    def test_stale_quote(self):
        self.assertEqual(self.code(quote_age_ms="1501"), "QUOTE_STALE_OR_CLOCK_INVALID")

    def test_limit_ran_away(self):
        self.assertEqual(self.code(current_executable_ask="0.52"), "LIMIT_PRICE_RAN_AWAY")

    def test_depth_insufficient(self):
        self.assertEqual(self.code(displayed_ask_depth_shares="4.9"), "DISPLAYED_DEPTH_INSUFFICIENT")

    def test_rejection(self):
        self.assertEqual(self.code(order_status="REJECTED"), "ORDER_REJECTED")

    def test_accepted_is_not_fill(self):
        r = classify(base(order_status="ACCEPTED"))
        self.assertEqual(r["reason_code"], "ORDER_ACCEPTED_NOT_CONFIRMED_FILLED")
        self.assertFalse(r["final_for_episode"])

    def test_matched_is_not_confirmed_fill(self):
        self.assertEqual(self.code(order_status="MATCHED"), "ORDER_ACCEPTED_NOT_CONFIRMED_FILLED")

    def test_terminal_unfilled(self):
        self.assertEqual(
            self.code(order_status="", order_terminal=True),
            "ORDER_TERMINAL_UNFILLED",
        )

    def test_partial_confirmed_fill(self):
        self.assertEqual(
            self.code(confirmed_filled_shares="2", order_terminal=True),
            "PARTIAL_CONFIRMED_FILL",
        )

    def test_confirmed_entry_waits_for_order_terminal(self):
        self.assertEqual(
            self.code(confirmed_filled_shares="5"),
            "CONFIRMED_FILL_ORDER_NOT_TERMINAL",
        )

    def test_open_position(self):
        self.assertEqual(
            self.code(confirmed_filled_shares="5", entry_order_terminal=True),
            "CONFIRMED_ENTRY_POSITION_OPEN",
        )

    def test_expired_unresolved(self):
        self.assertEqual(
            self.code(confirmed_filled_shares="5", entry_order_terminal=True, market_expired=True),
            "EXPIRED_AWAITING_OFFICIAL_RESOLUTION",
        )

    def test_redeemable_not_requested(self):
        self.assertEqual(
            self.code(
                confirmed_filled_shares="5",
                entry_order_terminal=True,
                market_expired=True,
                official_resolution_observed=True,
                redeemable=True,
            ),
            "REDEEMABLE_NOT_REQUESTED",
        )

    def test_redeem_pending(self):
        self.assertEqual(
            self.code(
                confirmed_filled_shares="5",
                entry_order_terminal=True,
                market_expired=True,
                official_resolution_observed=True,
                redeemable=True,
                redeem_requested=True,
            ),
            "REDEEM_REQUEST_PENDING_CONFIRMATION",
        )

    def test_cash_pending_after_redeem(self):
        self.assertEqual(
            self.code(
                confirmed_filled_shares="5",
                entry_order_terminal=True,
                market_expired=True,
                official_resolution_observed=True,
                redeem_confirmed=True,
            ),
            "REDEEM_CONFIRMED_CASH_CHECKPOINT_PENDING",
        )

    def test_cash_pending_after_exit(self):
        self.assertEqual(
            self.code(
                confirmed_filled_shares="5",
                entry_order_terminal=True,
                exit_trade_confirmed=True,
            ),
            "EXIT_CONFIRMED_CASH_CHECKPOINT_PENDING",
        )

    def test_unknown_reconciliation_is_distinct(self):
        self.assertEqual(
            self.code(
                confirmed_filled_shares="5",
                entry_order_terminal=True,
                reconciliation_unknown=True,
            ),
            "RECONCILIATION_UNKNOWN",
        )

    def test_complete_cash_roundtrip(self):
        self.assertEqual(
            self.code(
                confirmed_filled_shares="5",
                entry_order_terminal=True,
                cash_reconciled=True,
                position_flat=True,
            ),
            "CASH_RECONCILED_ROUNDTRIP_COMPLETE",
        )

    def test_summary_does_not_merge_reasons(self):
        s = summarize([
            base(signal_qualified=False),
            base(planned_shares="4"),
            base(current_executable_ask="0.52"),
            base(order_status="REJECTED"),
        ])
        self.assertEqual(s["episodes"], 4)
        self.assertEqual(len(s["by_reason_code"]), 4)
        self.assertEqual(s["by_stage"]["signal"], 1)
        self.assertEqual(s["by_stage"]["planning"], 1)
        self.assertEqual(s["by_stage"]["quote"], 1)
        self.assertEqual(s["by_stage"]["order"], 1)


if __name__ == "__main__":
    unittest.main()

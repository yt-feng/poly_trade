from datetime import datetime, timedelta, timezone

import pytest

from analysis.causal_cash_ledger import replay_cash_ledger


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candidate(index=0, **kwargs):
    start = START + timedelta(seconds=1800 * index)
    row = {
        "slug": f"market-{index}", "run_name": f"run-{index}",
        "decision_ts": start + timedelta(seconds=120),
        "entry_ts": start + timedelta(seconds=124),
        "market_end_ts": start + timedelta(seconds=300),
        "cash_release_ts": start + timedelta(seconds=1200),
        "side": "up", "fill_price": 0.5, "depth": 1000,
        "slippage": 0.0, "outcome_up": 1,
        "label_available_ts": start + timedelta(seconds=310),
    }
    row.update(kwargs)
    return row


def quote(index=0, seconds=150, bid=60, depth=1000):
    return {
        "ts_iso": START + timedelta(seconds=1800 * index + seconds),
        "sell_up_cents": bid, "sell_up_size": depth,
        "sell_down_cents": bid, "sell_down_size": depth,
    }


def test_expired_or_cutoff_bid_never_rescues_entered_loser():
    row = candidate(outcome_up=0)
    for seconds in (270, 300, 350, 570):
        report = replay_cash_ledger([row], {row["slug"]: [quote(seconds=seconds, bid=99)]})
        assert report["entries"] == report["resolved_fills"] == 1
        assert report["chain"][0]["resolution_kind"] == "observed_label_settlement"
        assert report["ending_cash"] == pytest.approx(4.3075)
        assert report["chain"][0]["payout"] == 0


def test_missing_exit_and_label_retains_locked_entry():
    row = candidate(outcome_up=None, label_available_ts=None)
    report = replay_cash_ledger([row, candidate(1)], {})
    assert report["entries"] == 1
    assert report["resolved_fills"] == 0
    assert report["unresolved_fills"] == 1
    assert report["locked_position_cost"] == pytest.approx(5.6925)
    assert report["ending_cash"] == pytest.approx(4.3075)
    assert report["skipped"]["position_cash_not_released"] == 1


def test_future_tail_does_not_change_first_entry_or_reservation():
    row = candidate()
    exited = replay_cash_ledger([row], {row["slug"]: [quote()]})
    losing = replay_cash_ledger([dict(row, outcome_up=0)], {})
    unknown = replay_cash_ledger([dict(row, outcome_up=None)], {})
    for report in (exited, losing, unknown):
        assert report["entries"] == 1
        first = report["chain"][0]
        assert first["reserved"] == pytest.approx(5.6925)
        assert first["cash_before_entry"] == 10
        assert first["entry_ts"] == row["entry_ts"].isoformat()


def test_known_ending_cash_includes_settlement_on_short_chain():
    report = replay_cash_ledger([candidate()], {})
    assert report["ending_cash"] == pytest.approx(15.3075)
    assert report["ending_cash"] != report["chain"][0]["cash_after_entry"]
    assert report["first_ten_passed"] is False
    assert report["cash_conservation_error"] == pytest.approx(0, abs=1e-10)


def test_ten_entries_can_include_loss_and_still_pass_net_cash_target():
    rows = [candidate(index, outcome_up=0 if index == 8 else 1) for index in range(10)]
    report = replay_cash_ledger(rows, {})
    assert report["entries"] == report["resolved_fills"] == 10
    assert any(position["realized_pnl"] < 0 for position in report["chain"])
    assert report["first_ten_passed"] is True
    assert report["ending_cash"] > 10
    assert report["actual_fills"] == 0


def test_boost_uses_only_strictly_prior_released_profit():
    first = candidate()
    blocked = candidate(1, entry_ts=START + timedelta(seconds=500))
    second = candidate(2)
    report = replay_cash_ledger([first, blocked, second], {}, boost_fraction=0.5875)
    assert [position["slug"] for position in report["chain"]] == [first["slug"], second["slug"]]
    assert [position["fraction"] for position in report["chain"]] == [0.55, 0.5875]
    assert report["chain"][1]["released_consecutive_profits_before_entry"] == 1
    assert report["skipped"]["position_cash_not_released"] == 1


def test_nonprofit_release_resets_boost_without_erasing_trade():
    rows = [candidate(index, outcome_up=0 if index == 2 else 1) for index in range(4)]
    report = replay_cash_ledger(rows, {}, boost_fraction=0.5875)
    assert report["entries"] == 4
    assert [position["fraction"] for position in report["chain"]] == [0.55, 0.5875, 0.5875, 0.55]


def test_depth_is_checked_before_entry_and_on_exit():
    low_entry = candidate(depth=20)
    rejected = replay_cash_ledger([low_entry], {}, depth_haircut=0.25)
    assert rejected["entries"] == 0
    assert rejected["ending_cash"] == 10
    row = candidate(outcome_up=0)
    shallow_exit = replay_cash_ledger([row], {row["slug"]: [quote(depth=20)]}, depth_haircut=0.25)
    assert shallow_exit["entries"] == 1
    assert shallow_exit["chain"][0]["payout"] == 0


def test_first_eligible_quote_uses_chronology_and_both_fees():
    row = candidate()
    report = replay_cash_ledger([row], {row["slug"]: [quote(seconds=200, bid=80), quote(seconds=150, bid=60)]})
    position = report["chain"][0]
    assert position["resolution_ts"] == quote()["ts_iso"].isoformat()
    assert position["payout"] == pytest.approx(11 * 0.6 - 11 * 0.07 * 0.6 * 0.4)
    assert report["ending_cash"] == pytest.approx(10 - position["reserved"] + position["payout"])
    assert report["elapsed_seconds"] == 1076


def test_evidence_horizon_does_not_invent_cash_release():
    report = replay_cash_ledger([candidate()], {}, evidence_end_ts=START + timedelta(seconds=900))
    assert report["entries"] == 1
    assert report["resolved_fills"] == 0
    assert report["first_ten_passed"] is False
    assert report["failure_reason"] == "entered_position_not_released"


def test_empty_candidates_and_bad_configuration():
    report = replay_cash_ledger([], {})
    assert report["entries"] == 0
    assert report["ending_cash"] == 10
    assert not report["first_ten_passed"]
    with pytest.raises(ValueError):
        replay_cash_ledger([], {}, depth_haircut=float("nan"))
    with pytest.raises(ValueError):
        replay_cash_ledger([], {}, base_fraction=0)


def test_release_at_same_timestamp_is_conservatively_blocked():
    first = candidate()
    tied = candidate(1, entry_ts=first["cash_release_ts"])
    report = replay_cash_ledger([first, tied], {})
    assert report["entries"] == 1
    assert report["skipped"]["position_cash_not_released"] == 1


def test_minimum_order_affordability_applies_to_entry_not_leftover_balance():
    report = replay_cash_ledger([candidate()], {})
    assert report["chain"][0]["cash_after_entry"] < 5
    assert report["entries"] == 1
    assert report["chain"][0]["cost"] >= 5
    assert report["chain"][0]["shares"] >= 5


def test_future_label_availability_blocks_next_entry_until_observed():
    first = candidate(label_available_ts=START + timedelta(seconds=2500))
    report = replay_cash_ledger([first, candidate(1), candidate(2)], {})
    assert [position["slug"] for position in report["chain"]] == ["market-0", "market-2"]
    assert report["chain"][0]["cash_reusable_ts"] == (START + timedelta(seconds=2500)).isoformat()


@pytest.mark.parametrize("extra", [
    {"quote_conflict": True},
    {"buy_up_cents": 50},
    {"buy_up_cents": float("nan")},
    {"buy_up_cents": 101},
    {"sell_up_cents": 101},
])
def test_invalid_or_conflicting_bid_cannot_rescue_loss(extra):
    row = candidate(outcome_up=0)
    bad = {**quote(), **extra}
    report = replay_cash_ledger([row], {row["slug"]: [bad]})
    assert report["entries"] == 1
    assert report["chain"][0]["payout"] == 0
    assert report["chain"][0]["resolution_kind"] == "observed_label_settlement"


def test_valid_ask_checked_and_nonofficial_label_basis_remains_explicit():
    proxy = "last_observed_btc_price_proxy_not_official_resolution"
    row = candidate(label_basis=proxy)
    settled = replay_cash_ledger([row], {})["chain"][0]
    assert settled["label_basis"] == proxy
    assert settled["resolution_kind"] == "proxy_label_settlement"
    exited = replay_cash_ledger([row], {row["slug"]: [{**quote(), "buy_up_cents": 65}]})["chain"][0]
    assert exited["resolution_kind"] == "pre_expiry_bid_exit"
    assert exited["label_basis"] == proxy


def test_frozen_sizing_matches_inherited_cash_fraction_rule():
    from types import SimpleNamespace
    from analysis.causal_cash_ledger import _entry_terms
    from analysis.h452_old_clean_cash_fraction import _size_from_cash

    for fraction in (0.55, 0.5875):
        for cash in (4.99, 5.0, 5.0000000000005, 5.2, 5.3, 10.0, 100.0):
            for price in (0.05, 0.5, 0.8, 0.99):
                old = _size_from_cash(SimpleNamespace(fill_price=price), cash, fraction)
                current = _entry_terms(cash, price, fraction, fee_rate=0.07, minimum_notional=5, minimum_shares=5)
                if old is None:
                    assert current is None
                else:
                    assert current == {
                        "shares": old["shares"], "cost": old["cost"],
                        "entry_fee": old["fee"], "reserved": old["reserved"],
                    }


def test_fleeting_profit_disappearing_at_arrival_does_not_fill_exit():
    row = candidate(outcome_up=0)
    observed = [quote(seconds=150, bid=60), quote(seconds=152, bid=50), quote(seconds=153, bid=65)]
    report = replay_cash_ledger([row], {row["slug"]: observed}, exit_latency_seconds=2)
    assert report["entries"] == 1
    assert report["chain"][0]["resolution_kind"] == "observed_label_settlement"
    assert report["chain"][0]["payout"] == 0
    assert report["exit_order_attempts"] == 2
    assert report["exit_order_unfilled_attempts"] == 2


def test_delayed_exit_revalidates_first_arrival_and_retries_only_later():
    row = candidate(outcome_up=0)
    observed = [quote(seconds=150, bid=60), quote(seconds=152, bid=50),
                quote(seconds=153, bid=65), quote(seconds=155, bid=62)]
    report = replay_cash_ledger([row], {row["slug"]: observed}, exit_latency_seconds=2)
    position = report["chain"][0]
    assert position["resolution_ts"] == quote(seconds=155)["ts_iso"].isoformat()
    assert position["exit_order_trigger_ts"] == quote(seconds=153)["ts_iso"].isoformat()
    assert position["payout"] == pytest.approx(11 * 0.62 - 11 * 0.07 * 0.62 * 0.38)
    assert report["exit_order_attempts"] == 2
    assert report["exit_order_unfilled_attempts"] == 1
    assert report["exit_latency_seconds"] == 2


def test_exit_arrival_at_expiry_cutoff_is_rejected():
    row = candidate(outcome_up=0)
    observed = [quote(seconds=268, bid=60), quote(seconds=270, bid=70), quote(seconds=301, bid=99)]
    report = replay_cash_ledger([row], {row["slug"]: observed}, exit_latency_seconds=2)
    assert report["chain"][0]["payout"] == 0
    assert report["exit_order_unfilled_attempts"] == 1


def test_missing_arrival_times_out_instead_of_using_late_profitable_quote():
    row = candidate(outcome_up=0)
    observed = [quote(seconds=150, bid=60), quote(seconds=160, bid=70)]
    report = replay_cash_ledger([row], {row["slug"]: observed}, exit_latency_seconds=2, max_exit_quote_wait_seconds=5)
    assert report["chain"][0]["resolution_kind"] == "observed_label_settlement"
    assert report["chain"][0]["payout"] == 0
    assert report["exit_order_attempts"] == 2


def test_pending_diagnostics_distinguish_missing_evidence_from_release_boundary():
    row = candidate(label_status="estimated_close_time_sample_proxy")
    before_release = row["cash_release_ts"] - timedelta(microseconds=1)
    delayed = replay_cash_ledger([row], {}, evidence_end_ts=before_release)
    missing = replay_cash_ledger(
        [dict(row, outcome_up=None, label_available_ts=None, label_status=None)],
        {}, evidence_end_ts=before_release,
    )
    released = replay_cash_ledger([row], {}, evidence_end_ts=row["cash_release_ts"])
    delayed_position = delayed["chain"][0]
    assert delayed_position["unresolved_reason"] == "cash_release_after_evidence_horizon"
    assert delayed_position["outcome_up"] == 1.0
    assert delayed_position["label_available_ts"] == row["label_available_ts"].isoformat()
    assert delayed_position["label_status"] == row["label_status"]
    assert delayed_position["evidence_end_ts"] == before_release.isoformat()
    assert missing["chain"][0]["unresolved_reason"] == "settlement_evidence_missing"
    assert missing["chain"][0]["outcome_up"] is None
    assert missing["chain"][0]["label_available_ts"] is None
    assert missing["chain"][0]["label_status"] is None
    assert released["chain"][0]["unresolved_reason"] is None
    assert delayed["entries"] == missing["entries"] == released["entries"] == 1
    assert delayed["ending_cash"] == missing["ending_cash"]
    assert released["ending_cash"] == pytest.approx(15.3075)

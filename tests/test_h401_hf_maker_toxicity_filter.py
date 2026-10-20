from types import SimpleNamespace
from decimal import Decimal

from analysis.h401_hf_maker_toxicity_filter import filter_windows, signed_flow_imbalance


def _p(ts, outcome, side, shares):
    return SimpleNamespace(ts=ts, outcome=outcome, side=side, shares=shares)


def _canonical_p(ts, outcome, side, size):
    return SimpleNamespace(ts=ts, outcome=outcome, side=side, size=size)


def test_pre_entry_flow_imbalance_is_causal_and_signed():
    rows = [_p(100, "Up", "BUY", 9), _p(101, "Down", "BUY", 1), _p(500, "Up", "BUY", 100)]
    assert signed_flow_imbalance(rows, 100, horizon_seconds=60) == 0.8


def test_missing_pre_entry_tape_is_unknown_not_neutral():
    assert signed_flow_imbalance([], 100) is None


def test_canonical_spork30_print_uses_size_field():
    rows = [_canonical_p(100, "Up", "BUY", 9), _canonical_p(101, "Down", "BUY", 1)]
    assert signed_flow_imbalance(rows, 100, horizon_seconds=60) == 0.8


def test_filter_rejects_toxic_window_and_keeps_balanced_window():
    windows = [
        SimpleNamespace(start=100, prints=[_p(101, "Up", "BUY", 9), _p(102, "Down", "BUY", 1)]),
        SimpleNamespace(start=200, prints=[_p(201, "Up", "BUY", 5), _p(202, "Down", "BUY", 5)]),
    ]
    kept, audit = filter_windows(windows, Decimal("0.20"))
    assert [window.start for window in kept] == [200]
    assert audit[0]["accepted"] is False
    assert audit[1]["accepted"] is True

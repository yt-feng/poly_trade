from decimal import Decimal
from types import SimpleNamespace

from analysis.h403_whale_event_gate_maker import filter_windows, whale_dominance


def p(ts, size):
    return SimpleNamespace(ts=ts, size=size)


def test_whale_dominance_uses_only_causal_pre_entry_prints():
    rows = [p(100, 3), p(101, 1), p(500, 100)]
    assert whale_dominance(rows, 100) == 0.75


def test_missing_pre_entry_prints_are_rejected_not_neutral():
    assert whale_dominance([], 100) is None


def test_event_gate_rejects_single_large_print():
    windows = [
        SimpleNamespace(start=100, prints=[p(101, 8), p(102, 2)]),
        SimpleNamespace(start=200, prints=[p(201, 3), p(202, 3), p(203, 3), p(204, 1)]),
    ]
    kept, audit = filter_windows(windows, Decimal("0.30"))
    assert [x.start for x in kept] == [200]
    assert audit[0]["accepted"] is False
    assert audit[1]["accepted"] is True

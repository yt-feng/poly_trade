import importlib.util
from pathlib import Path


MODULE = Path(__file__).parents[1] / "analysis" / "h387_maker_replay.py"
SPEC = importlib.util.spec_from_file_location("h387_maker_replay", MODULE)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def _rows(n, pnl="0.50"):
    return [{"start": i * 300, "pnl": pnl, "fills": 1} for i in range(n)]


def test_ten_rows_are_one_cash_ledger_without_reset_or_concat():
    result = MOD.replay_series(_rows(10))
    assert result["ten_row_same_ledger_gate"] is True
    assert result["same_ledger_rows"] == 10
    assert result["ledger_reset_count"] == 0
    assert result["concatenation_count"] == 0
    assert result["paper_chain_passed"] is True
    # The second window overlaps the first redeem by 66 seconds; entry must
    # still be funded by the remaining free balance, not by future proceeds.
    assert result["ledger_rows"][1]["cash_before"] == "5"


def test_insufficient_free_cash_blocks_a_row_and_never_mints_cash():
    result = MOD.replay_series(_rows(3, pnl="-5"))
    assert result["same_ledger_rows"] == 2
    assert result["skipped_insufficient_cash"] == 1
    assert result["ledger_rows"][2]["reason"] == "insufficient_free_cash"
    assert result["ending_cash_after_pending_release"] == "0"


def test_report_marks_paper_only_and_no_canary():
    report = MOD.build_report(
        {"windows": [0], "per_window": {"v1,queue=0": _rows(1)}},
        source_commit="abc123",
        protocol="post_only_paired_complete_set",
    )
    assert report["paper_only"] is True
    assert report["live_orders_enabled"] is False
    assert report["actual_fills"] == 0
    assert report["verified_canary_fills"] == 0
    assert report["cash_ledger_protocol"]["fixed_split_size"] == "5"

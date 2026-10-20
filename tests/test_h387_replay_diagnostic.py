import json
from pathlib import Path

from tools.h387_replay_diagnostic import build_report


def test_replay_failure_keeps_bounded_private_diagnostic(tmp_path: Path):
    log = tmp_path / "source.log"
    log.write_text("line-1\nline-2\n", encoding="utf-8")
    report = build_report(
        stage="public_tape_replay",
        exit_code=7,
        log_path=log,
        backtest_path=tmp_path / "missing.json",
    )
    assert report["status"] == "source_replay_failed"
    assert report["exit_code"] == 7
    assert report["backtest_json_present"] is False
    assert report["source_log_tail"] == ["line-1", "line-2"]
    json.dumps(report)


def test_clone_and_pin_reports_do_not_claim_strategy_evaluation():
    for stage in ("clone", "pin"):
        report = build_report(stage=stage, exit_code=128)
        assert report["status"] == "source_unavailable"
        assert report["live_orders_enabled"] is False
        assert "source_log_tail" not in report

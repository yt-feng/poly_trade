from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h387-complete-set-maker-replay.yml"


def test_h387_is_pinned_small_cash_and_sealed():
    text = WORKFLOW.read_text()
    assert "accd1511a08748302c8116ab65575930e1240068" in text
    assert 'new = \'split_size: Decimal = Decimal("5")\'' in text
    assert "analysis/h387_maker_replay.py" in text
    assert "publish_quality_first_journal.py" in text
    assert "'live_orders_enabled': False" in text
    assert "report.get('format') == 'h387-complete-set-maker-replay-v1'" in text
    assert "'source_replay_status': report.get('status', 'paper_report')" in text
    assert "POLYMARKET_PRIVATE_KEY" not in text


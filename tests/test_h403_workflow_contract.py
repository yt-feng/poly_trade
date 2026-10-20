from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h403-whale-event-gate-maker.yml"


def test_h403_freezes_one_causal_whale_event_cap():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "H403-whale-event-gate-inventory-balanced-maker" in text
    assert '--whale-dominance-cap "0.30"' in text
    assert "new_market_data_collected': False" in text
    assert "live_orders_enabled': False" in text


def test_h403_publishes_only_encrypted_results():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "research_vault.seal" in text
    assert "publish_quality_first_journal.py" in text
    assert "plaintext_published': False" in text

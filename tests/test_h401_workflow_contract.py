from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h401-hf-maker-toxicity-filter.yml"


def test_h401_freezes_two_causal_toxicity_caps_and_reuses_fixed_tape():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "H401-causal-maker-toxicity-filter" in text
    assert '--toxicity-cap "0.20,0.40"' in text
    assert "pre-entry" in text
    assert "new_market_data_collected" in text
    assert "live_orders_enabled': False" in text


def test_h401_seals_and_publishes_only_encrypted_results():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "research_vault.seal" in text
    assert "publish_quality_first_journal.py" in text
    assert "plaintext_published': False" in text

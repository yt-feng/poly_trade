from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h402-inventory-balanced-maker.yml"


def test_h402_is_a_single_new_protocol_on_the_fixed_tape():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "H402-inventory-balanced-paired-maker" in text
    script = (WORKFLOW.parents[2] / "analysis" / "h402_inventory_balanced_maker.py").read_text(encoding="utf-8")
    assert 'max_lean_shares=Decimal("1")' in script
    assert 'improve_spread_ticks=1' in script
    assert "analysis/h402_inventory_balanced_maker.py" in text
    assert "new_market_data_collected': False" in text
    assert "live_orders_enabled': False" in text


def test_h402_seals_and_publishes_only_encrypted_results():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "research_vault.seal" in text
    assert "publish_quality_first_journal.py" in text
    assert "plaintext_published': False" in text

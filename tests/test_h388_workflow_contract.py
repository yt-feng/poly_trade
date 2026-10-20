from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h388-pair-cost-public-wallet-audit.yml"


def test_h388_is_pinned_observational_and_sealed():
    text = WORKFLOW.read_text()
    assert "56484ca7e5fe15e0a573ee0c232d2281e56a35e0" in text
    assert "h388_pair_cost_public_wallet.py" in text
    assert "target_activity.json" in text
    assert "publish_quality_first_journal.py" in text
    assert "POLYMARKET_PRIVATE_KEY" not in text
    assert "live_orders_enabled': False" in text

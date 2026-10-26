from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "h534-time-regime-gate.yml"


def test_time_regime_workflow_is_cloud_only_and_sealed():
    text = WORKFLOW.read_text()
    assert "repository: yt-feng/poly" in text
    assert "H534_UTC_OVERNIGHT_REGIME_GATE" in text
    assert "--source sealed-delivery" in text
    assert "live_orders_enabled': False" in text
    assert "plaintext_published': False" in text
    assert "POLYMARKET_PRIVATE_KEY" not in text
    assert "order_submission" not in text.lower()

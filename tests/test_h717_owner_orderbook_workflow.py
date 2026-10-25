from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "h717-owner-orderbook-join.yml"


def test_h717_workflow_is_read_only_and_cloud_bound():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "BTC5M_POLYMARKET_PRIVATE_KEY" in text
    assert "analysis/h279_authenticated_lifecycle_capture.py" in text
    assert "analysis/h717_owner_orderbook_join.py" in text
    assert "r2v2.pmxt.dev" in text
    assert "live_orders_enabled':False" in text or "live_orders_enabled': False" in text
    assert "orders_submitted" not in text
    assert "submit_order" not in text
    assert "cancel_order" not in text
    assert "sealed-delivery/part-0000.vault" not in text


def test_h717_workflow_does_not_publish_plaintext():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "publish_quality_first_journal.py" in text
    assert "plaintext_published':False" in text or "plaintext_published': False" in text

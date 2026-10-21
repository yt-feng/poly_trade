from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_h465_workflow_freezes_h453_fraction_and_strict_cutoff():
    text = (ROOT / ".github/workflows/h465-old-clean-fraction-strict-cutoff.yml").read_text()
    assert "H465_H453_FULL_CASH_STRICT_60S_CUTOFF" in text
    assert "source_commit" in text
    assert "no new market capture" in text.lower()
    assert "live_orders_enabled': False" in text or "live_orders_enabled':False" in text

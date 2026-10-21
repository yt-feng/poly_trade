from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_h453_workflow_is_one_predeclared_upper_fraction_run():
    text = (ROOT / ".github/workflows/h453-old-clean-upper-cash-fraction.yml").read_text()
    assert "--gross-cash-fractions 0.95 1.0" in text
    assert "H453_H452_UPPER_CASH_FRACTION_CEILING" in text
    assert "new market capture" in text.lower()
    assert "live_orders_enabled': False" in text or "live_orders_enabled':False" in text

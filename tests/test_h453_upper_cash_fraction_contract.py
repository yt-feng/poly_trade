from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h452_old_clean_cash_fraction import _decision_label


ROOT = Path(__file__).resolve().parents[1]


def test_h453_workflow_is_one_predeclared_upper_fraction_run():
    text = (ROOT / ".github/workflows/h453-old-clean-upper-cash-fraction.yml").read_text()
    assert "--gross-cash-fractions 0.95 1.0" in text
    assert "H453_H452_UPPER_CASH_FRACTION_CEILING" in text
    assert "new market capture" in text.lower()
    assert "live_orders_enabled': False" in text or "live_orders_enabled':False" in text


def test_derived_h453_run_gets_its_own_decision_label():
    assert _decision_label("H452_H447_CASH_AWARE_FRACTIONAL_SIZING") == "h452"
    assert _decision_label("H453_H452_UPPER_CASH_FRACTION_CEILING") == "h453"

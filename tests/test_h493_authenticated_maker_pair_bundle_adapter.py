from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h493_authenticated_maker_pair_bundle_adapter import audit


def test_h493_adapter_audit_is_complete_and_offline_only():
    result = audit()
    assert result["all_checks_passed"] is True
    assert result["offline_consistency_passed"] is True
    assert result["paper_pairs"] == 10
    assert result["paper_ending_cash"] == "60"
    assert result["gate_passed"] is False
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0


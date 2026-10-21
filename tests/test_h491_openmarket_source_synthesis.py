from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from h491_openmarket_source_synthesis import synthesize


def test_h491_rejects_duplicate_lead_lag_alpha_without_counting_canary():
    result = synthesize()
    assert result["all_checks_passed"] is True
    assert result["decision"] == "reject_binance_lead_lag_as_standalone_alpha_and_do_not_replay_duplicate_signal"
    assert result["reported_results"]["model_oos_beats_mid_prior"] is False
    assert result["reported_results"]["simulated_pnl_per_attempted_trade"] < 0
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["same_ledger_compounding_rows"] == 0


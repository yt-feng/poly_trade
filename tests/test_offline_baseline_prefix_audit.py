import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "analysis" / "offline_baseline_prefix_audit.py"
SPEC = importlib.util.spec_from_file_location("offline_baseline_prefix_audit", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _ledger():
    return {
        "scope": "pooled_single_ledger",
        "live_orders_enabled": False,
        "chain": [
            {
                "decision_ts": "2026-01-01T00:00:00+00:00",
                "cash_before_entry": 10.0,
                "cash_after_entry": 0.0,
                "entry_cost": 9.0,
                "payout": 10.5,
                "exit_gross_proceeds": 10.6,
                "exit_fee": 0.1,
                "realized_pnl": 0.5,
                "status": "timed_quote_exit",
            },
            {
                "decision_ts": "2026-01-01T00:01:00+00:00",
                "cash_before_entry": 10.5,
                "cash_after_entry": 0.0,
                "entry_cost": 10.0,
                "payout": 11.0,
                "exit_gross_proceeds": 11.1,
                "exit_fee": 0.1,
                "realized_pnl": 0.5,
                "status": "timed_quote_exit",
            },
        ],
    }


def test_audit_checks_same_cash_ledger():
    result = MODULE.audit(_ledger(), {"protocol": {}, "economic_readout": {}} , expected_fills=2)
    assert result["status"] == "valid_historical_simulated_continuous_prefix"
    assert result["continuous_sequence_complete"] is True
    assert result["ending_cash"] == 11.0
    assert result["net_cash_change"] == 1.0


def test_audit_rejects_spliced_cash():
    ledger = _ledger()
    ledger["chain"][1]["cash_before_entry"] = 10.25
    result = MODULE.audit(ledger, {"protocol": {}, "economic_readout": {}}, expected_fills=2)
    assert result["status"] == "invalid_prefix_invariants"
    assert result["continuous_sequence_complete"] is False
    assert any("cash_before_not_previous_payout" in error for error in result["errors"])

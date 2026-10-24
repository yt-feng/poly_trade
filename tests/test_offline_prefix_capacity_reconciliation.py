import importlib.util


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "analysis" / "offline_prefix_capacity_reconciliation.py"
SPEC = importlib.util.spec_from_file_location("offline_prefix_capacity_reconciliation", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _ledger():
    return {
        "scope": "pooled_single_ledger",
        "chain": [{
            "market_id": "m1",
            "shares": 20.0,
            "cash_before_entry": 10.0,
            "entry_cost": 9.0,
            "cash_release_ts": "2026-01-01T00:20:00+00:00",
            "exit_retry": {"trace": [{"status": "thin", "available_shares": 10.0, "depth_haircut": 0.1}, {"status": "full_size", "available_shares": 250.0, "depth_haircut": 0.1, "arrival_ts": "2026-01-01T00:03:00+00:00"}]},
        }],
    }


def _domain():
    return {"m1": {
        "entry_effective_available_shares": "30",
        "exit_effective_available_shares": "1",
        "maximum_pair_capacity_shares": "1",
        "minimum_entry_shares": "5",
        "minimum_entry_cash": "5",
        "simulation_cash_domain_observed": "True",
        "minimum_order_has_pair_capacity": "False",
        "assumed_cash_release_ts": "2026-01-01T00:20:00+00:00",
        "cash_release_basis": "declared",
    }}


def test_retry_rescues_initial_capacity():
    result = MODULE.reconcile(_ledger(), _domain(), expected_fills=1)
    assert result["status"] == "valid_historical_retry_reconciled_prefix"
    assert result["initial_exit_pair_capacity_pass_count"] == 0
    assert result["retry_used_count"] == 1
    assert result["retry_rescued_initial_capacity_count"] == 1
    assert result["final_exit_capacity_pass_count"] == 1


def test_missing_cash_release_is_rejected():
    domains = _domain()
    domains["m1"]["assumed_cash_release_ts"] = "2026-01-01T00:19:00+00:00"
    result = MODULE.reconcile(_ledger(), domains, expected_fills=1)
    assert result["status"] == "invalid_saved_capacity_reconciliation"
    assert result["cash_release_match_count"] == 0

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import h508_h301_forward_adapter as adapter
from h508_h301_forward_adapter import ContractError, validate_document


def _source():
    digest = "a" * 64
    return {
        "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
        "h508_summary_sha256": digest,
        "h301_projection_sha256": digest,
        "market_constraints_sha256": digest,
        "market_metadata_sha256": digest,
        "depth_source_sha256": digest,
        "fee_source_sha256": digest,
        "latency_source_sha256": digest,
    }


def _protocol(minimum_order_usd=5.0):
    return {
        "initial_cash_usd": 10.0,
        "minimum_order_usd": minimum_order_usd,
        "minimum_order_shares": 5.0,
        "required_fills": 10,
        "same_ledger": True,
        "no_cross_window_concatenation": True,
        "paper_only": True,
        "fee_rate": 0.07,
        "cash_release_delay_seconds": 900.0,
        "exit_latency_seconds": 1.0,
        "max_exit_quote_wait_seconds": 5.0,
        "depth_haircut": 1.0,
    }


def _row():
    return {
        "slug": "btc-up-1",
        "entry_ts": "2026-01-01T00:00:01+00:00",
        "market_end_ts": "2026-01-01T00:05:00+00:00",
        "side": "up",
        "fill_price": 0.50,
        "entry_depth": 20.0,
        "slippage": 0.0,
        "label_basis": "official_resolution",
        "label_available_ts": "2026-01-01T00:05:01+00:00",
        "outcome_up": 1.0,
        "paper_fill_admitted": True,
        "owner_scope_verified": False,
    }


def _document():
    document = {
        "format": "btc5m-h508-h301-forward-input-v1",
        "source": _source(),
        "protocol": _protocol(),
        "rows": [_row()],
        "quotes": {"btc-up-1": [{"ts_iso": "2026-01-01T00:01:00+00:00", "sell_up_cents": 55, "sell_up_size": 20}]},
        "market_metadata": {"btc-up-1": {"min_order_size": 5.0, "tick_size": 0.01, "itode": False, "observed_ts": "2026-01-01T00:00:00+00:00"}},
    }
    return _rehash_metadata(document)


def _rehash_metadata(document):
    canonical = json.dumps(document["market_metadata"], sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("utf-8")
    document["source"]["market_metadata_sha256"] = hashlib.sha256(canonical).hexdigest()
    return document


def test_complete_bundle_passes_contract():
    document = _document()
    validated = validate_document(document)
    assert validated["protocol"]["minimum_order_shares"] == 5.0
    assert validated["market_metadata"] == document["market_metadata"]
    assert validated["metadata_validation"][0]["tick_execution_applicability"] == "unverified_fill_vwap_only"


def test_legacy_zero_notional_floor_is_rejected():
    with pytest.raises(ContractError, match="minimum_order_usd_not_currently_verified"):
        validate_document({**_document(), "protocol": _protocol(0.0)})


def test_missing_execution_provenance_is_rejected():
    document = _document()
    source = document["source"]
    source.pop("depth_source_sha256")
    with pytest.raises(ContractError, match="missing_or_invalid_digest:depth_source_sha256"):
        validate_document(document)


def test_missing_market_metadata_is_rejected():
    document = _document()
    document.pop("market_metadata")
    with pytest.raises(ContractError, match="market_metadata_missing"):
        validate_document(document)


def test_market_metadata_requires_explicit_delay_flag_and_tick():
    document = _document()
    document["market_metadata"]["btc-up-1"]["itode"] = "false"
    _rehash_metadata(document)
    with pytest.raises(ContractError, match="market_metadata_itode_invalid"):
        validate_document(document)


def test_metadata_content_is_bound_to_declared_digest():
    document = _document()
    document["market_metadata"]["btc-up-1"]["tick_size"] = 0.02
    with pytest.raises(ContractError, match="market_metadata_digest_mismatch"):
        validate_document(document)


@pytest.mark.parametrize("observed", ["invalid", "2026-01-01T00:00:00", True, 1])
def test_metadata_timestamp_must_be_timezone_aware(observed):
    document = _document()
    document["market_metadata"]["btc-up-1"]["observed_ts"] = observed
    _rehash_metadata(document)
    with pytest.raises(ContractError, match="market_metadata_observed_ts_invalid"):
        validate_document(document)


def test_metadata_after_entry_is_rejected():
    document = _document()
    document["market_metadata"]["btc-up-1"]["observed_ts"] = "2026-01-01T00:00:02Z"
    _rehash_metadata(document)
    with pytest.raises(ContractError, match="market_metadata_observed_after_cutoff"):
        validate_document(document)


def test_decision_timestamp_is_the_cutoff_when_supplied():
    document = _document()
    document["rows"][0]["decision_ts"] = "2025-12-31T23:59:59Z"
    with pytest.raises(ContractError, match="market_metadata_observed_after_cutoff"):
        validate_document(document)


@pytest.mark.parametrize("cutoff", [None, "", "invalid", "2026-01-01T00:00:00"])
def test_invalid_supplied_decision_timestamp_does_not_fall_back_to_entry(cutoff):
    document = _document()
    document["rows"][0]["decision_ts"] = cutoff
    with pytest.raises(ContractError, match="row_metadata_cutoff_invalid"):
        validate_document(document)


def test_decision_after_entry_cannot_admit_future_metadata():
    document = _document()
    document["rows"][0]["decision_ts"] = "2026-01-01T00:00:03Z"
    document["market_metadata"]["btc-up-1"]["observed_ts"] = "2026-01-01T00:00:02Z"
    _rehash_metadata(document)
    with pytest.raises(ContractError, match="row_decision_after_entry"):
        validate_document(document)


def test_timezone_offsets_are_compared_as_instants():
    document = _document()
    document["rows"][0]["decision_ts"] = "2026-01-01T00:00:00Z"
    document["market_metadata"]["btc-up-1"]["observed_ts"] = "2026-01-01T01:00:00+01:00"
    _rehash_metadata(document)
    validated = validate_document(document)
    assert validated["metadata_validation"][0]["cutoff_field"] == "decision_ts"


def test_old_metadata_is_not_claimed_fresh_without_a_registered_age_limit():
    document = _document()
    document["market_metadata"]["btc-up-1"]["observed_ts"] = "2025-01-01T00:00:00Z"
    _rehash_metadata(document)
    validated = validate_document(document)
    assert validated["metadata_validation"][0]["freshness"] == "unverified_no_registered_max_age"


@pytest.mark.parametrize("field", ["min_order_size", "tick_size"])
@pytest.mark.parametrize("value", [True, False, 0, -1])
def test_metadata_numeric_fields_reject_booleans_and_nonpositive_values(field, value):
    document = _document()
    document["market_metadata"]["btc-up-1"][field] = value
    _rehash_metadata(document)
    with pytest.raises(ContractError, match=f"market_metadata_{field}_invalid"):
        validate_document(document)


def test_market_floor_above_frozen_shares_is_rejected():
    document = _document()
    document["market_metadata"]["btc-up-1"]["min_order_size"] = 10
    _rehash_metadata(document)
    with pytest.raises(ContractError, match="market_minimum_exceeds_frozen_shares"):
        validate_document(document)


def test_lower_market_floor_preserves_frozen_strategy_floor():
    document = _document()
    document["market_metadata"]["btc-up-1"]["min_order_size"] = 1
    _rehash_metadata(document)
    assert validate_document(document)["protocol"]["minimum_order_shares"] == 5.0


def test_entry_delay_is_rejected_before_replay(monkeypatch):
    document = _document()
    document["market_metadata"]["btc-up-1"]["itode"] = True
    _rehash_metadata(document)
    monkeypatch.setattr(adapter, "replay_cash_ledger", lambda *args, **kwargs: pytest.fail("unsupported delay replayed"))
    with pytest.raises(ContractError, match="market_entry_delay_unsupported"):
        adapter.evaluate(document)


@pytest.mark.parametrize("field", ["order_price", "limit_price"])
def test_concrete_order_price_must_align_to_tick(field):
    document = _document()
    document["rows"][0][field] = 0.505
    with pytest.raises(ContractError, match="row_order_price_off_tick"):
        validate_document(document)
    document["rows"][0][field] = 0.51
    validated = validate_document(document)
    assert validated["metadata_validation"][0]["tick_execution_applicability"] == "order_price_alignment_checked"


def test_fill_vwap_is_not_mistaken_for_submitted_order_price():
    document = _document()
    document["rows"][0]["fill_price"] = 0.505
    validated = validate_document(document)
    assert validated["metadata_validation"][0]["tick_execution_applicability"] == "unverified_fill_vwap_only"


def test_successful_paper_evaluation_retains_metadata_and_frozen_parameters(monkeypatch):
    calls = []

    def replay(rows, quotes, **kwargs):
        calls.append(kwargs)
        return {"paper_only": True}

    monkeypatch.setattr(adapter, "replay_cash_ledger", replay)
    document = _document()
    result = adapter.evaluate(document)
    assert result["market_metadata"] == document["market_metadata"]
    assert result["metadata_validation"][0]["tick_execution_applicability"] == "unverified_fill_vwap_only"
    assert [call["base_fraction"] for call in calls] == [0.5, 0.75]
    assert all(call["minimum_shares"] == 5.0 and call["exit_latency_seconds"] == 1.0 for call in calls)
    assert result["verified_canary_fills"] == 0
    assert result["promotion_eligible"] is False


def test_push_ci_exercises_adapter_and_ledger_regressions():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/compound-readiness-contract.yml"
    execution_job = workflow.read_text().split("  entry-execution:", 1)[1].split("  contract:", 1)[0]
    assert "tests/test_h508_h301_forward_adapter.py tests/test_causal_cash_ledger.py" in execution_job


def test_unverified_owner_row_cannot_enter_paper_adapter():
    row = _row()
    row["owner_scope_verified"] = True
    with pytest.raises(ContractError, match="row_owner_scope_must_be_false_for_paper_adapter"):
        validate_document({**_document(), "rows": [row]})

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
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
    return {
        "format": "btc5m-h508-h301-forward-input-v1",
        "source": _source(),
        "protocol": _protocol(),
        "rows": [_row()],
        "quotes": {"btc-up-1": [{"ts_iso": "2026-01-01T00:01:00+00:00", "sell_up_cents": 55, "sell_up_size": 20}]},
        "market_metadata": {"btc-up-1": {"min_order_size": 5.0, "tick_size": 0.01, "itode": False, "observed_ts": "2026-01-01T00:00:00+00:00"}},
    }


def test_complete_bundle_passes_contract():
    validated = validate_document(_document())
    assert validated["protocol"]["minimum_order_shares"] == 5.0


def test_legacy_zero_notional_floor_is_rejected():
    with pytest.raises(ContractError, match="minimum_order_usd_not_currently_verified"):
        validate_document({**_document(), "protocol": _protocol(0.0)})


def test_missing_execution_provenance_is_rejected():
    source = _source()
    source.pop("depth_source_sha256")
    with pytest.raises(ContractError, match="missing_or_invalid_digest:depth_source_sha256"):
        validate_document({**_document(), "source": source})


def test_missing_market_metadata_is_rejected():
    document = _document()
    document.pop("market_metadata")
    with pytest.raises(ContractError, match="market_metadata_missing"):
        validate_document(document)


def test_market_metadata_requires_explicit_delay_flag_and_tick():
    document = _document()
    document["market_metadata"]["btc-up-1"]["itode"] = "false"
    with pytest.raises(ContractError, match="market_metadata_itode_invalid"):
        validate_document(document)


def test_unverified_owner_row_cannot_enter_paper_adapter():
    row = _row()
    row["owner_scope_verified"] = True
    with pytest.raises(ContractError, match="row_owner_scope_must_be_false_for_paper_adapter"):
        validate_document({**_document(), "rows": [row]})

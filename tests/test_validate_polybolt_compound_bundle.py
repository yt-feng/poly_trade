import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from validate_polybolt_compound_bundle import validate_bundle_document


def account_event(i: int, market_id: str = "m-1") -> dict[str, object]:
    return {
        "owner": "owner-1",
        "market_id": market_id,
        "status": "confirmed",
        "trade_id": f"trade-{i}",
        "transaction_ref": f"tx-{i}",
        "settlement_receipt_id": f"settlement-{i}",
        "cash_release_receipt_id": f"cash-release-{i}",
        "balance_checkpoint_id": f"balance-{i}",
        "cash_reconciled": True,
        "independent_checkpoint": True,
        "entry_ts": str(i * 100),
        "cash_available_ts": str(i * 100 + 10),
        "entry_cost": "5",
        "fee": "0",
        "payout": "5.50",
    }


def source_event(seq: int, market_id: str = "m-1") -> dict[str, object]:
    ts = 1_800_000_000_100 + (seq - 1) * 1000
    return {
        "market_id": market_id,
        "topic": "prices.crypto.twap",
        "type": "update",
        "timestamp": ts,
        "seq": seq,
        "dropped": 0,
        "authenticated": True,
        "received_ms": ts + 100,
        "connection_id": "conn-a",
        "sequence_scope": "capture-a/conn-a",
        "capture_id": "capture-a",
        "raw_payload_sha256": hashlib.sha256(f"event-{seq}".encode()).hexdigest(),
        "payload": {"symbol": "btcusd", "timestamp": ts, "value": "100.0", "windowSeconds": 60},
    }


def bundle(events=None, source=None):
    return {
        "schema": "authenticated-polybolt-compound-bundle-v1",
        "owner": "owner-1",
        "market_windows": [{"market_id": "m-1", "asset": "btc", "slug": "btc-updown-5m-1800000000", "window_start_ms": 1_800_000_000_000, "window_end_ms": 1_800_000_300_000}],
        "source_events": [source_event(i) for i in range(1, 4)] if source is None else source,
        "events": [account_event(i) for i in range(10)] if events is None else events,
    }


def test_polybolt_source_and_authenticated_cash_chain_admit_together():
    result = validate_bundle_document(bundle())
    assert result["gate_passed"] is True
    assert result["source_continuity_ok"] is True
    assert result["fills"] == 10
    assert result["market_window_count"] == 1
    assert "owner-1" not in str(result)


def test_source_gap_blocks_even_when_account_chain_is_valid():
    source = [source_event(1), source_event(3)]
    result = validate_bundle_document(bundle(source=source))
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "source_continuity_not_admissible"
    assert result["source_continuity_ok"] is False


def test_non_btc_five_minute_market_cannot_enter():
    doc = bundle()
    doc["market_windows"][0]["slug"] = "btc-updown-15m-1800000000"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "btc_five_minute_slug_required"


def test_missing_source_or_market_identity_fails_closed():
    doc = bundle()
    del doc["source_events"]
    assert validate_bundle_document(doc)["failure_reason"] == "source_events_required"
    doc = bundle()
    doc["events"][0]["market_id"] = "other"
    assert validate_bundle_document(doc)["failure_reason"] == "account_event_market_identity_mismatch"


def test_source_must_cover_every_account_market():
    doc = bundle()
    doc["market_windows"].append({"market_id": "m-2", "asset": "btc", "slug": "btc-updown-5m-1800000300", "window_start_ms": 1_800_000_300_000, "window_end_ms": 1_800_000_600_000})
    doc["events"][0]["market_id"] = "m-2"
    result = validate_bundle_document(doc)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "source_account_market_coverage_mismatch"

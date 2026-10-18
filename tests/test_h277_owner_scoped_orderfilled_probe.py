import analysis.h277_owner_scoped_orderfilled_probe as h277
from analysis.h277_owner_scoped_orderfilled_probe import normalize_owner, run, summarize_records
from pathlib import Path


OWNER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def test_summary_counts_taker_and_maker_without_exposing_owner_text():
    result = summarize_records(
        [
            {"event_id": "a", "taker": OWNER, "maker": OTHER, "tx_hash": "0xa"},
            {"event_id": "b", "taker": OTHER, "maker": OWNER, "tx_hash": "0xb"},
        ],
        [{"label": "key-a", "address": OWNER}],
    )[0]
    assert result["public_orderfilled_taker_events"] == 1
    assert result["public_orderfilled_maker_events"] == 1
    assert result["owner_scoped_account_receipt"] is False
    assert result["settlement_evidence"] is False
    assert OWNER not in result


def test_normalize_owner_rejects_non_address():
    try:
        normalize_owner("wallet-sample")
    except ValueError as exc:
        assert "20-byte" in str(exc)
    else:
        raise AssertionError("invalid owner accepted")


def test_summary_requires_bounded_owner_count():
    try:
        summarize_records([], [])
    except ValueError as exc:
        assert "one to four" in str(exc)
    else:
        raise AssertionError("empty owner set accepted")


def test_run_uses_owner_indexed_taker_filter(monkeypatch):
    seen = {}

    def fake_probe(**kwargs):
        seen.update(kwargs)
        return {"records": [], "account_scope": "public_taker_filtered; not a private wallet receipt"}

    monkeypatch.setattr(h277, "probe", fake_probe)
    result = run(
        owners=[{"label": "key-a", "address": OWNER}],
        rpc_url="https://rpc.example",
        window_blocks=100,
        chunk_blocks=50,
        max_logs=10,
    )
    assert seen["taker_addresses"] == [OWNER]
    assert result["taker_topic_filter_applied"] is True
    assert result["new_data_collected"] is False


def test_workflow_allows_bounded_rpc_failover():
    workflow = Path(__file__).parents[1].joinpath(
        ".github", "workflows", "h277-owner-scoped-orderfilled-probe.yml"
    ).read_text(encoding="utf-8")
    assert "rpc_url:" in workflow
    assert "RPC_URL: ${{ inputs.rpc_url }}" in workflow
    assert "--rpc-url \"$RPC_URL\"" in workflow

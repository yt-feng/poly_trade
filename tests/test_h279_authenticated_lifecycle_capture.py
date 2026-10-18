import json

import analysis.h279_authenticated_lifecycle_capture as h279


class FakePaginator:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        yield from self.items


def test_bounded_items_marks_truncation_without_dropping_order():
    rows, truncated = h279._bounded_items(FakePaginator(range(5)), limit=3)
    assert rows == [0, 1, 2]
    assert truncated is True


def test_capture_is_fail_closed_for_canary_even_with_synthetic_account_rows(monkeypatch):
    def fake_capture(label, private_key, sdk_commit):
        return {
            "label": label,
            "signer_address_sha256": "a" * 64,
            "wallet_address_sha256": "b" * 64,
            "sdk_commit": sdk_commit,
            "auth_flow": "GET /auth/derive-api-key; no credential creation",
            "account_trades": [{"id": "trade-1"}],
            "account_trades_count": 1,
            "account_trades_truncated": False,
            "public_activity": [],
            "public_activity_count": 0,
            "public_activity_truncated": False,
            "balance_checkpoint": {"balance": "10"},
            "balance_checkpoint_id": "c" * 64,
            "authenticated_account_receipt": True,
            "settlement_receipt_present": False,
            "cash_release_receipt_present": False,
            "independent_balance_checkpoint_present": True,
        }

    monkeypatch.setattr(h279, "_capture_one", fake_capture)
    result = h279.capture([{"label": "x", "private_key": "secret"}], sdk_commit="sdk")
    assert result["receipt_boundary"] == {
        "authenticated_trade_rows": 1,
        "settlement_receipt_rows": 0,
        "cash_release_receipt_rows": 0,
        "balance_checkpoint_rows": 1,
        "canary_eligible": False,
    }
    assert result["actual_fills"] == 0
    assert result["verified_canary_fills"] == 0
    assert result["live_orders_enabled"] is False


def test_jsonable_handles_sdk_models_without_serializing_private_key():
    class Model:
        def model_dump(self, mode=None):
            return {"status": "CONFIRMED", "size": "5"}

    value = h279._jsonable(Model())
    assert value == {"status": "CONFIRMED", "size": "5"}
    assert "private_key" not in json.dumps(value)


def test_failure_code_is_bounded_and_non_sensitive():
    error = RuntimeError("HTTP 401 unauthorized for https://example.invalid/account")
    assert h279._failure_code(error) == "authentication_rejected"
    assert "example.invalid" not in h279._failure_code(error)


def test_public_data_wallet_prefers_authenticated_trade_address():
    rows = [
        {"maker_address": "0x" + "a" * 40},
        {"maker_address": "0x" + "a" * 40},
        {"maker_address": "0x" + "b" * 40},
    ]
    wallet, source = h279._public_data_wallet(rows, "0x" + "c" * 40)
    assert wallet == "0x" + "a" * 40
    assert source == "account_trade_address"


def test_public_data_wallet_falls_back_to_sdk_wallet():
    wallet, source = h279._public_data_wallet(
        [{"owner": "internal-id"}], "0x" + "c" * 40
    )
    assert wallet == "0x" + "c" * 40
    assert source == "client_wallet_fallback"


def test_public_data_wallet_accepts_sdk_like_model():
    class Trade:
        maker_address = "0x" + "d" * 40

        def model_dump(self, mode=None):
            return {"maker_address": self.maker_address}

    wallet, source = h279._public_data_wallet([Trade()], "0x" + "c" * 40)
    assert wallet == "0x" + "d" * 40
    assert source == "account_trade_address"

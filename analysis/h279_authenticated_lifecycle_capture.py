"""Read-only authenticated lifecycle capture for the frozen Canary gate.

The runner derives existing CLOB credentials with the official SDK's GET-only
``/auth/derive-api-key`` path, then reads account trades, public activity and a
balance/allowance checkpoint.  It never creates credentials, signs an order,
posts, cancels, redeems, merges, transfers, or changes allowances.  The
workflow seals the complete normalized bundle; only a redacted status is
printed to CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable


MAX_ROWS = 400
ADDRESS_LENGTH = 42


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json"))
        except TypeError:
            return _jsonable(model_dump())
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        return _jsonable(as_dict())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _failure_code(error: BaseException) -> str:
    """Return a bounded, non-sensitive class for an encrypted failure report."""
    text = str(error).lower()
    name = type(error).__name__.lower()
    if "429" in text or "rate" in text and "limit" in text:
        return "provider_rate_limit"
    if "401" in text or "403" in text or "unauthor" in text or "signature" in text:
        return "authentication_rejected"
    if "timeout" in text or "timed out" in text:
        return "provider_timeout"
    if "connection" in text or "dns" in text or "network" in text:
        return "provider_connection"
    if "parse" in name or "validation" in name or "shape" in text:
        return "response_shape_or_validation"
    if "import" in name or "module" in text:
        return "sdk_import"
    return "capture_exception"


def _bounded_items(paginator: Any, limit: int = MAX_ROWS) -> tuple[list[Any], bool]:
    items: list[Any] = []
    iterator = paginator.iter_items()
    for item in iterator:
        items.append(item)
        if len(items) >= limit:
            return items, True
    return items, False


def _derive_credentials(private_key: str) -> Any:
    """Derive existing credentials without the SDK's POST create fallback."""
    from eth_account import Account
    from polymarket._internal.actions.auth import derive_api_key_sync
    from polymarket._internal.environment import get_environment_config
    from polymarket._internal.l1_auth import sign_api_key_auth
    from polymarket.clients._transport import SyncTransport
    from polymarket.environments import PRODUCTION

    config = get_environment_config(PRODUCTION)
    signer = Account.from_key(private_key)
    signature = sign_api_key_auth(
        signer,
        chain_id=config.chain_id,
        timestamp=int(time.time()),
        nonce=0,
    )
    transport = SyncTransport(base_url=config.clob_url)
    try:
        return derive_api_key_sync(transport, signature)
    finally:
        transport.close()


def _capture_one(label: str, private_key: str, sdk_commit: str) -> dict[str, Any]:
    from eth_account import Account
    from polymarket import SecureClient
    from polymarket.models import AssetType

    signer = Account.from_key(private_key)
    credentials = _derive_credentials(private_key)
    # Supplying the signer address avoids any wallet-deployment or relayer
    # action during client bootstrap; this workflow is strictly read-only.
    client = SecureClient.create(
        private_key=private_key,
        wallet=signer.address,
        credentials=credentials,
    )
    try:
        account_trades, trades_truncated = _bounded_items(client.list_account_trades())
        activity, activity_truncated = _bounded_items(client.list_activity(full_history=True))
        balance = client.get_balance_allowance(asset_type=AssetType.COLLATERAL)
        raw_balance = _jsonable(balance)
        return {
            "label": label,
            "signer_address_sha256": _digest(signer.address.lower()),
            "wallet_address_sha256": _digest(str(client.wallet).lower()),
            "sdk_commit": sdk_commit,
            "auth_flow": "GET /auth/derive-api-key; no credential creation",
            "account_trades": [_jsonable(item) for item in account_trades],
            "account_trades_count": len(account_trades),
            "account_trades_truncated": trades_truncated,
            "public_activity": [_jsonable(item) for item in activity],
            "public_activity_count": len(activity),
            "public_activity_truncated": activity_truncated,
            "balance_checkpoint": raw_balance,
            "balance_checkpoint_id": _digest(json.dumps(raw_balance, sort_keys=True)),
            "authenticated_account_receipt": bool(account_trades),
            "settlement_receipt_present": False,
            "cash_release_receipt_present": False,
            "independent_balance_checkpoint_present": raw_balance is not None,
        }
    finally:
        client.close()


def capture(keys: Iterable[dict[str, str]], *, sdk_commit: str) -> dict[str, Any]:
    rows = list(keys)
    if not rows or len(rows) > 4:
        raise ValueError("one to four account keys are required")
    captures = []
    for row in rows:
        label = str(row.get("label") or "").strip()
        private_key = str(row.get("private_key") or "").strip()
        if not label or not private_key:
            raise ValueError("account label and private key are required")
        captures.append(_capture_one(label, private_key, sdk_commit))
    return {
        "format": "h279-authenticated-lifecycle-capture-v1",
        "sdk_commit": sdk_commit,
        "accounts": captures,
        "scope": "read-only authenticated CLOB trade/activity/balance capture",
        "orders_submitted": False,
        "orders_cancelled": False,
        "redemptions_or_merges_submitted": False,
        "allowances_changed": False,
        "strategy_evaluated": False,
        "new_data_collected": False,
        "model_refit": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
        "receipt_boundary": {
            "authenticated_trade_rows": sum(row["account_trades_count"] for row in captures),
            "settlement_receipt_rows": 0,
            "cash_release_receipt_rows": 0,
            "balance_checkpoint_rows": sum(int(row["independent_balance_checkpoint_present"]) for row in captures),
            "canary_eligible": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys-file", type=Path, required=True)
    parser.add_argument("--sdk-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        keys = json.loads(args.keys_file.read_text(encoding="utf-8"))
        result = capture(keys, sdk_commit=args.sdk_commit)
    except Exception as error:
        result = {
            "format": "h279-authenticated-lifecycle-capture-failure-v1",
            "failure_code": _failure_code(error),
            "failure_type": type(error).__name__,
            "failure_digest": _digest(f"{type(error).__name__}:{error}"),
            "sdk_commit": args.sdk_commit,
            "strategy_evaluated": False,
            "new_data_collected": False,
            "model_refit": False,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "same_ledger_compounding_rows": 0,
            "live_orders_enabled": False,
        }
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "failed", "failure_code": result["failure_code"]}, sort_keys=True))
        raise
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "computed",
        "format": result["format"],
        "authenticated_trade_rows": result["receipt_boundary"]["authenticated_trade_rows"],
        "canary_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()

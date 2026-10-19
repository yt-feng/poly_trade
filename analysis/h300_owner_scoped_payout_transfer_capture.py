"""Bounded, read-only settlement and collateral-cash capture.

H279/H280 already preserve authenticated account activity and transaction-level
``OrderFilled`` joins.  This module fills the next evidence boundary by
looking only at the transaction hashes in that activity set and decoding
``PayoutRedemption`` plus collateral ``Transfer`` logs.  It never signs,
submits, cancels, redeems, merges, changes allowances, or reads an account
balance.  The resulting rows are input to the existing H284 cash reconciler;
they are not Canary fills by themselves.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

try:
    from .h284_owner_scoped_cash_reconciliation import reconcile_observed_cash
except ImportError:  # direct ``python analysis/h300_*.py`` execution
    from h284_owner_scoped_cash_reconciliation import reconcile_observed_cash


MAX_ACCOUNTS = 4
MAX_ACTIVITY_ROWS = 400
MAX_TRANSACTIONS = 200
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")

# keccak256 event signatures from the canonical ConditionalTokens and ERC-20
# ABIs.  Keeping constants here avoids a runtime dependency on web3 in the
# local contract tests; the cloud workflow installs the official SDK anyway.
PAYOUT_REDEMPTION_TOPIC = "0x2682012a4a4f1973119f1c9b90745d1bd91fa2bab387344f044cb3586864d18d"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Polygon collateral and ConditionalTokens addresses.  The caller can extend
# these lists for a separately registered contract version, but unknown
# contracts are never silently treated as settlement evidence.
DEFAULT_COLLATERAL_TOKENS = frozenset({
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC.e
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # native USDC
})
DEFAULT_CTF_CONTRACTS = frozenset({
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _lower_address(value: Any) -> str:
    text = _text(value).lower()
    if not ADDRESS.fullmatch(text):
        return ""
    return text


def _tx(value: Any) -> str:
    text = _text(value).lower()
    return text if TX_HASH.fullmatch(text) else ""


def _owner_digest(owner: str) -> str:
    return hashlib.sha256(owner.encode("ascii")).hexdigest()


def _first(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        number = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not number.is_finite() or number < 0:
        return ""
    return format(number, "f")


def _timestamp(value: Any) -> int | None:
    try:
        result = int(str(value))
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def normalize_activity_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one SDK/Data API activity row without inferring settlement."""
    if not isinstance(raw, dict):
        return None
    kind = _text(_first(raw, ("type", "event_type", "eventType", "activity_type"))).upper()
    side = _text(_first(raw, ("side", "trade_side"))).upper()
    if kind in {"BUY", "SELL"}:
        side = kind
        kind = "TRADE"
    if kind in {"REDEEMED", "REDEMPTION", "PAYOUT_REDEMPTION"}:
        kind = "REDEEM"
        side = ""
    if kind == "TRADE" and side not in {"BUY", "SELL"}:
        return None
    if kind == "REDEEM":
        side = ""
    if kind not in {"TRADE", "REDEEM"}:
        return None
    tx = _tx(_first(raw, ("transaction_ref", "transactionHash", "transaction_hash", "tx_hash", "txHash")))
    ts = _timestamp(_first(raw, ("timestamp", "timestamp_ms", "created_at")))
    amount = _number(_first(raw, ("usdc_size", "usdcSize", "amount", "payout", "cash_amount")))
    if not amount and kind == "TRADE":
        size = _number(_first(raw, ("size", "shares")))
        price = _number(_first(raw, ("price", "entry_price")))
        if size and price:
            amount = format(Decimal(size) * Decimal(price), "f")
    slug = _text(_first(raw, ("slug", "event_slug", "eventSlug", "market_slug", "marketSlug"))).lower()
    condition_id = _text(_first(raw, ("condition_id", "conditionId", "market"))).lower()
    if not tx or ts is None or not amount:
        return None
    return {
        "type": kind,
        "side": side,
        "transaction_ref": tx,
        "timestamp": ts,
        "usdc_size": amount,
        "slug": slug,
        "condition_id": condition_id,
    }


def is_btc_five_minute(row: dict[str, Any]) -> bool:
    """Accept only explicit BTC Up/Down five-minute market slugs."""
    slug = _text(row.get("slug")).lower()
    return bool(re.search(r"(?:^|[-_])(btc|bitcoin)[-_](?:updown|up_down)[-_]5m(?:[-_]|$)", slug))


def _word(value: str) -> int:
    text = _text(value)
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != 64:
        raise ValueError("log_word_must_be_32_bytes")
    try:
        return int(text, 16)
    except ValueError as exc:
        raise ValueError("log_word_must_be_hex") from exc


def _hex_number(value: Any) -> int | None:
    text = _text(value).lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or not re.fullmatch(r"[0-9a-f]+", text):
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _topic_address(value: str) -> str:
    text = _text(value)
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != 64:
        return ""
    return "0x" + text[-40:].lower()


def _data_words(value: Any) -> list[str]:
    text = _text(value)
    if text.startswith("0x"):
        text = text[2:]
    if len(text) % 64:
        return []
    return [text[i:i + 64] for i in range(0, len(text), 64)]


def decode_receipt_logs(
    receipt: dict[str, Any],
    *,
    owner: str,
    collateral_tokens: Iterable[str] = DEFAULT_COLLATERAL_TOKENS,
    ctf_contracts: Iterable[str] = DEFAULT_CTF_CONTRACTS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Decode owner-directed collateral transfers and CTF payout events."""
    expected_owner = _lower_address(owner)
    if not expected_owner:
        raise ValueError("owner_address_required")
    allowed_tokens = {_lower_address(x) for x in collateral_tokens if _lower_address(x)}
    allowed_ctf = {_lower_address(x) for x in ctf_contracts if _lower_address(x)}
    status = _text(receipt.get("status")).lower()
    confirmed = status in {"0x1", "1"}
    transfers: list[dict[str, Any]] = []
    payouts: list[dict[str, Any]] = []
    tx_hash = _tx(receipt.get("transactionHash") or receipt.get("transaction_hash"))
    if not tx_hash:
        raise ValueError("receipt_transaction_hash_required")
    block_number = _hex_number(receipt.get("blockNumber"))
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics") or []
        if not topics:
            continue
        topic0 = _text(topics[0]).lower()
        contract = _lower_address(log.get("address"))
        if topic0 == TRANSFER_TOPIC and contract in allowed_tokens and len(topics) >= 3:
            source = _topic_address(topics[1])
            target = _topic_address(topics[2])
            words = _data_words(log.get("data"))
            if not source or not target or len(words) != 1:
                continue
            try:
                amount = _word(words[0])
            except ValueError:
                continue
            if confirmed and (source == expected_owner or target == expected_owner):
                transfers.append({
                    "transaction_ref": tx_hash,
                    "token": contract,
                    "from": source,
                    "to": target,
                    "amount_base_units": amount,
                    "confirmed_nonreverted": confirmed,
                    "block_number": block_number,
                })
        elif topic0 == PAYOUT_REDEMPTION_TOPIC and contract in allowed_ctf and len(topics) >= 2:
            redeemer = _topic_address(topics[1])
            collateral = _topic_address(topics[2]) if len(topics) >= 3 else ""
            words = _data_words(log.get("data"))
            if redeemer != expected_owner or collateral not in allowed_tokens or len(words) < 3:
                continue
            try:
                offset = _word(words[1])
                payout = _word(words[2])
                if offset % 32 or offset // 32 >= len(words):
                    continue
                start = offset // 32
                count = _word(words[start])
                if start + 1 + count > len(words):
                    continue
            except ValueError:
                continue
            if not confirmed:
                continue
            payouts.append({
                "transaction_ref": tx_hash,
                "amount_base_units": payout,
                "collateral_token": collateral,
                "condition_id": "0x" + words[0].lower(),
                "redeemer_address_sha256": _owner_digest(expected_owner),
                "confirmed_nonreverted": confirmed,
                "block_number": block_number,
            })
    return transfers, payouts, confirmed


def _rpc_fetch(rpc_url: str, transaction_hash: str) -> dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [transaction_hash]}).encode()
    request = Request(rpc_url, data=body, headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "poly-trade-h300/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error") or not isinstance(payload.get("result"), dict):
        raise RuntimeError("receipt_rpc_error")
    return payload["result"]


def capture_account(
    account: dict[str, Any],
    *,
    receipt_fetcher: Callable[[str], dict[str, Any]],
    max_transactions: int = MAX_TRANSACTIONS,
) -> dict[str, Any]:
    owner = _lower_address(account.get("owner"))
    if not owner:
        raise ValueError("account_owner_required")
    raw_activity = account.get("activity") or []
    normalized = [item for raw in raw_activity if (item := normalize_activity_row(raw))]
    btc_rows = [item for item in normalized if is_btc_five_minute(item)]
    transactions = list(dict.fromkeys(item["transaction_ref"] for item in btc_rows))
    if len(transactions) > max_transactions:
        transactions = transactions[:max_transactions]
    transfer_rows: list[dict[str, Any]] = []
    payout_rows: list[dict[str, Any]] = []
    confirmed_receipts = 0
    fetch_failures = 0
    for transaction_hash in transactions:
        try:
            transfers, payouts, confirmed = decode_receipt_logs(receipt_fetcher(transaction_hash), owner=owner)
        except Exception:
            fetch_failures += 1
            continue
        transfer_rows.extend(transfers)
        payout_rows.extend(payouts)
        confirmed_receipts += int(confirmed)
    reconciliations: dict[str, dict[str, Any]] = {}
    if btc_rows:
        h284_rows = [{key: row[key] for key in ("type", "side", "transaction_ref", "timestamp", "usdc_size")} for row in btc_rows]
        tokens = sorted({row["token"] for row in transfer_rows} | {row["collateral_token"] for row in payout_rows})
        for token in tokens:
            try:
                reconciliations[token] = reconcile_observed_cash(
                    h284_rows,
                    transfer_rows,
                    owner=owner,
                    collateral_token=token,
                    payout_receipts=[row for row in payout_rows if row["collateral_token"] == token],
                )
            except Exception as exc:
                reconciliations[token] = {"error": type(exc).__name__}
    single_reconciliation = next(iter(reconciliations.values())) if len(reconciliations) == 1 else None
    return {
        "label": _text(account.get("label")),
        "owner_address_sha256": _owner_digest(owner),
        "normalized_activity_rows": len(normalized),
        "btc_five_minute_activity_rows": len(btc_rows),
        "btc_five_minute_market_count": len({row["slug"] for row in btc_rows if row["slug"]}),
        "transaction_hashes_considered": len(transactions),
        "confirmed_receipts": confirmed_receipts,
        "receipt_fetch_failures": fetch_failures,
        "collateral_transfer_rows": len(transfer_rows),
        "payout_redemption_rows": len(payout_rows),
        "cash_reconciliation": single_reconciliation,
        "cash_reconciliation_by_collateral_token": reconciliations,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def capture(accounts: Iterable[dict[str, Any]], *, receipt_fetcher: Callable[[str], dict[str, Any]], max_transactions: int = MAX_TRANSACTIONS) -> dict[str, Any]:
    rows = list(accounts)
    if not 1 <= len(rows) <= MAX_ACCOUNTS:
        raise ValueError("one to four accounts are required")
    reports = [capture_account(row, receipt_fetcher=receipt_fetcher, max_transactions=max_transactions) for row in rows]
    return {
        "format": "h300-owner-scoped-payout-transfer-capture-v1",
        "accounts": reports,
        "scope": "read-only transaction-scoped payout and collateral transfer decoding",
        "settlement_receipt_rows": sum(item["payout_redemption_rows"] for item in reports),
        "cash_release_receipt_rows": sum(
            sum(reconciliation.get("cash_release_transaction_rows", 0)
                for reconciliation in (item.get("cash_reconciliation_by_collateral_token") or {}).values())
            for item in reports
        ),
        "owner_scoped_full_settlement_cash_bundle": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--max-transactions", type=int, default=MAX_TRANSACTIONS)
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    if not 1 <= args.max_transactions <= MAX_TRANSACTIONS:
        raise SystemExit(f"max-transactions must be between 1 and {MAX_TRANSACTIONS}")
    result = capture(
        document.get("accounts") or [],
        receipt_fetcher=lambda tx: _rpc_fetch(args.rpc_url, tx),
        max_transactions=args.max_transactions,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "computed", "format": result["format"], "settlement_receipt_rows": result["settlement_receipt_rows"]}, sort_keys=True))


if __name__ == "__main__":
    main()

"""Cloud-only join of one PendulumFlow hour to public Polygon receipts.

The join is deliberately an evidence probe, not a strategy run.  It downloads
one manifest-selected hour, extracts bounded identity sets, queries finalized
Polygon ``OrderFilled`` logs and (only for matched transaction hashes) receipt
topic evidence.  Raw identifiers stay in runner memory and are never emitted
in the report.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from polygon_orderfilled_probe import (  # noqa: E402
    DEFAULT_EXCHANGE_ADDRESSES,
    ORDER_FILLED_TOPICS,
    POLYGON_RPC_URL,
    V1_ORDER_FILLED_TOPIC,
    V2_ORDER_FILLED_TOPIC,
    _hex_int,
    _rpc,
    decode_v1_log,
    decode_v2_log,
    probe as orderfilled_probe,
)


MAX_BYTES = 256 * 1024 * 1024
MAX_IDENTIFIERS = 250_000
MAX_RECEIPTS = 200
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEX_WORD = re.compile(r"^0x[0-9a-fA-F]{64}$")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PAYOUT_REDEMPTION_TOPIC = "0x2682012a4a4f1973119f1c9b90745d1bd91fa2bab387344f044cb3586864d18d"
POSITION_REDEEMED_TOPIC = "0x4fd474b703f46b453536518b4cbc159dfbea6fd663233763d321a9bc8ccdf97f"
POSITIONS_MERGED_TOPIC = "0xf233f7be4ce81130a78e8eb2fa626497586c56094d27bfec2e196471e9c60301"
SETTLEMENT_TOPICS = {PAYOUT_REDEMPTION_TOPIC, POSITION_REDEEMED_TOPIC, POSITIONS_MERGED_TOPIC}
CASH_CONTRACTS = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC.e
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # native USDC
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",  # pUSD
}

TX_FIELDS = ("transaction_hash", "tx_hash", "transactionHash", "txHash")
OWNER_FIELDS = ("owner", "owner_id", "maker", "taker", "wallet", "maker_address", "taker_address")
TOKEN_FIELDS = ("asset_id", "assetId", "token_id", "tokenId", "maker_asset_id", "taker_asset_id")
MARKET_FIELDS = ("market", "market_id", "condition_id", "conditionId")


def _fetch(url: str, *, max_bytes: int) -> tuple[int, bytes]:
    import requests

    with requests.get(url, stream=True, timeout=(10, 90), headers={"User-Agent": "poly-trade-h717-chain-join/1"}) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("CONTENT_LENGTH_EXCEEDS_BOUND")
        total = 0
        chunks: list[bytes] = []
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("RESPONSE_EXCEEDS_BOUND")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks)


def _download(url: str, path: Path, *, expected_bytes: int, expected_sha: str) -> int:
    import requests

    if expected_bytes <= 0 or expected_bytes > MAX_BYTES:
        raise ValueError("MANIFEST_BYTES_OUT_OF_BOUND")
    digest = hashlib.sha256()
    total = 0
    with requests.get(url, stream=True, timeout=(10, 180), headers={"User-Agent": "poly-trade-h717-chain-join/1"}) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_BYTES:
            raise ValueError("CONTENT_LENGTH_EXCEEDS_BOUND")
        with path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("DOWNLOAD_EXCEEDS_BOUND")
                digest.update(chunk)
                handle.write(chunk)
    if total != expected_bytes or digest.hexdigest() != expected_sha:
        raise ValueError("ARCHIVE_INTEGRITY_MISMATCH")
    return total


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return "" if value is None else str(value).strip()


def _tx(value: Any) -> str:
    text = _text(value).lower()
    return text if TX_HASH.fullmatch(text) else ""


def _address(value: Any) -> str:
    text = _text(value).lower()
    return text if ADDRESS.fullmatch(text) else ""


def _token(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        return ""
    try:
        if text.startswith("0x"):
            return str(int(text, 16))
        return str(int(text))
    except (TypeError, ValueError):
        return ""


def _pick(names: Iterable[str], available: set[str]) -> list[str]:
    return [name for name in names if name in available]


def _collect_identity(path: Path) -> dict[str, Any]:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    names = set(parquet.schema_arrow.names)
    tx_cols = _pick(TX_FIELDS, names)
    owner_cols = _pick(OWNER_FIELDS, names)
    token_cols = _pick(TOKEN_FIELDS, names)
    market_cols = _pick(MARKET_FIELDS, names)
    # Market labels are useful provenance, but are not needed for the first
    # chain join.  Do not pay to materialize them across the whole hour.
    selected = list(dict.fromkeys(tx_cols + owner_cols + token_cols))
    report: dict[str, Any] = {
        "parquet_rows": parquet.metadata.num_rows,
        "parquet_row_groups": parquet.num_row_groups,
        "schema_name_count": len(names),
        "selected_columns": selected,
        "transaction_columns": tx_cols,
        "owner_columns": owner_cols,
        "token_columns": token_cols,
        "market_columns": market_cols,
        "rows_scanned": 0,
        "transaction_hash_count": 0,
        "owner_count": 0,
        "token_id_count": 0,
        "market_id_count": 0,
        "market_id_collection_skipped": True,
        "identifier_caps_hit": False,
    }
    tx_hashes: set[str] = set()
    owners: set[str] = set()
    token_ids: set[str] = set()
    market_ids: set[str] = set()
    if not selected:
        return {"report": report, "tx_hashes": tx_hashes, "owners": owners, "token_ids": token_ids, "market_ids": market_ids}
    for batch in parquet.iter_batches(columns=selected, batch_size=250_000):
        columns = batch.to_pydict()
        row_count = batch.num_rows
        report["rows_scanned"] += row_count
        for index in range(row_count):
            if len(tx_hashes) < MAX_IDENTIFIERS:
                for column in tx_cols:
                    value = _tx(columns[column][index])
                    if value:
                        tx_hashes.add(value)
                        break
            if len(owners) < MAX_IDENTIFIERS:
                for column in owner_cols:
                    value = _address(columns[column][index])
                    if value:
                        owners.add(value)
            if len(token_ids) < MAX_IDENTIFIERS:
                for column in token_cols:
                    value = _token(columns[column][index])
                    if value:
                        token_ids.add(value)
            # Market IDs are deliberately not materialized in this first join.
            if len(tx_hashes) >= MAX_IDENTIFIERS or len(owners) >= MAX_IDENTIFIERS or len(token_ids) >= MAX_IDENTIFIERS:
                report["identifier_caps_hit"] = True
                break
        if report["identifier_caps_hit"]:
            # A capped identity set cannot support exhaustive admission. Stop
            # early and preserve the explicit incompleteness in the sealed log.
            break
    report.update({
        "transaction_hash_count": len(tx_hashes),
        "owner_count": len(owners),
        "token_id_count": len(token_ids),
        "market_id_count": len(market_ids),
    })
    return {"report": report, "tx_hashes": tx_hashes, "owners": owners, "token_ids": token_ids, "market_ids": market_ids}


def _block_timestamp(rpc_url: str, block_number: int) -> int:
    result = _rpc(rpc_url, "eth_getBlockByNumber", [hex(block_number), False], block_number)
    if not isinstance(result, dict):
        raise RuntimeError("block_header_missing")
    return _hex_int(result.get("timestamp"), "block_timestamp")


def _first_block_at_or_after(rpc_url: str, target: int, latest: int) -> int:
    low, high = 0, latest
    while low < high:
        middle = (low + high) // 2
        if _block_timestamp(rpc_url, middle) < target:
            low = middle + 1
        else:
            high = middle
    return low


def _topic_owner_match(topics: Any, owners: set[str]) -> bool:
    if not isinstance(topics, list):
        return False
    return any(_address("0x" + str(topic)[-40:]) in owners for topic in topics[1:] if HEX_WORD.fullmatch(str(topic)))


def _receipt_evidence(rpc_url: str, tx_hashes: list[str], owners: set[str]) -> dict[str, Any]:
    queried = 0
    present = 0
    orderfilled_receipts = 0
    settlement_receipts = 0
    cash_transfer_receipts = 0
    owner_evidence_receipts = 0
    for tx_hash in tx_hashes[:MAX_RECEIPTS]:
        queried += 1
        receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], queried)
        if not isinstance(receipt, dict):
            continue
        present += 1
        has_orderfilled = False
        has_settlement = False
        has_cash_transfer = False
        has_owner = False
        for log in receipt.get("logs") or []:
            if not isinstance(log, dict):
                continue
            address = _address(log.get("address"))
            topics = log.get("topics")
            topic0 = str(topics[0]).lower() if isinstance(topics, list) and topics else ""
            if topic0 in ORDER_FILLED_TOPICS:
                has_orderfilled = True
            if topic0 in SETTLEMENT_TOPICS:
                has_settlement = True
            if topic0 == TRANSFER_TOPIC and address in CASH_CONTRACTS:
                has_cash_transfer = True
            if _topic_owner_match(topics, owners):
                has_owner = True
        orderfilled_receipts += int(has_orderfilled)
        settlement_receipts += int(has_settlement)
        cash_transfer_receipts += int(has_cash_transfer)
        owner_evidence_receipts += int(has_owner)
    return {
        "receipt_query_count": queried,
        "receipt_present_count": present,
        "receipt_query_capped": len(tx_hashes) > MAX_RECEIPTS,
        "orderfilled_receipt_count": orderfilled_receipts,
        "settlement_topic_receipt_count": settlement_receipts,
        "cash_transfer_receipt_count": cash_transfer_receipts,
        "owner_topic_evidence_receipt_count": owner_evidence_receipts,
    }


def probe(base: str, hour: str, *, rpc_url: str = POLYGON_RPC_URL, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    import pyarrow.parquet as pq  # noqa: F401 - validates cloud dependency before network work

    prefix = f"{base.rstrip('/')}/v3/{hour}"
    manifest_url = f"{prefix}/manifest.json"
    parquet_url = f"https://dl.pendulumflow.com/v3/{hour}/{hour.replace('/', 'T')}.parquet"
    report: dict[str, Any] = {
        "format": "h717-pendulumflow-chain-join-probe-v1",
        "status": "complete",
        "hour": hour,
        "strategy_evaluated": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "parquet_bytes_downloaded": 0,
        "manifest_url": manifest_url,
        "parquet_url": parquet_url,
        "rpc_url": rpc_url,
        "raw_identifiers_published": False,
    }
    try:
        manifest_status, manifest_body = _fetch(manifest_url, max_bytes=256 * 1024)
        report["manifest_status"] = manifest_status
        if manifest_status != 200:
            raise RuntimeError("MANIFEST_HTTP_ERROR")
        manifest = json.loads(manifest_body)
        expected_bytes = manifest.get("bytes")
        expected_sha = manifest.get("sha256")
        if type(expected_bytes) is not int or not isinstance(expected_sha, str):
            raise ValueError("MANIFEST_INTEGRITY_FIELDS_MISSING")
        with tempfile.TemporaryDirectory(prefix="h717-pendulumflow-join-") as directory:
            path = Path(directory) / "hour.parquet"
            report["parquet_bytes_downloaded"] = _download(
                parquet_url, path, expected_bytes=expected_bytes, expected_sha=expected_sha
            )
            identity = _collect_identity(path)
            report.update(identity["report"])
            if not identity["tx_hashes"]:
                report["decision"] = "no_transaction_identity_in_hour_no_chain_join"
                return report
            latest = _hex_int(_rpc(rpc_url, "eth_blockNumber", [], 1), "latest_block")
            start_ts = int(datetime.fromisoformat(hour.replace("/", "T") + ":00:00+00:00").timestamp())
            end_ts = start_ts + 3600
            from_block = _first_block_at_or_after(rpc_url, start_ts, latest)
            end_boundary = _first_block_at_or_after(rpc_url, end_ts, latest)
            to_block = max(from_block, end_boundary - 1)
            report.update({"from_block": from_block, "to_block": to_block, "latest_block": latest})
            logs = orderfilled_probe(
                rpc_url=rpc_url,
                from_block=from_block,
                to_block=to_block,
                chunk_blocks=200,
                max_logs=100_000,
                addresses=DEFAULT_EXCHANGE_ADDRESSES,
            )
            records = logs.get("records") or []
            tx_matches = [record for record in records if str(record.get("tx_hash", "")).lower() in identity["tx_hashes"]]
            token_matches = []
            for record in tx_matches:
                values = []
                for key in ("maker_asset_id", "taker_asset_id", "token_id"):
                    if record.get(key) is not None:
                        values.append(_token(record[key]))
                if any(value in identity["token_ids"] for value in values if value):
                    token_matches.append(record)
            owner_matches = [
                record for record in token_matches
                if _address(record.get("maker")) in identity["owners"] or _address(record.get("taker")) in identity["owners"]
            ]
            report.update({
                "orderfilled_record_count": len(records),
                "transaction_join_match_count": len(tx_matches),
                "token_join_match_count": len(token_matches),
                "owner_join_match_count": len(owner_matches),
                "distinct_join_transaction_count": len({str(row.get("tx_hash")).lower() for row in token_matches}),
                "orderfilled_log_chunks": logs.get("rpc_log_chunk_count"),
            })
            receipt_hashes = list(dict.fromkeys(str(row.get("tx_hash")).lower() for row in token_matches if row.get("tx_hash")))
            report.update(_receipt_evidence(rpc_url, receipt_hashes, identity["owners"]))
            report["cash_ledger_proven"] = False
            if (
                report.get("token_join_match_count", 0) > 0
                and report.get("receipt_present_count", 0) == report.get("receipt_query_count", 0)
                and report.get("orderfilled_receipt_count", 0) > 0
                and (report.get("settlement_topic_receipt_count", 0) > 0 or report.get("cash_transfer_receipt_count", 0) > 0)
            ):
                report["decision"] = "chain_identity_and_settlement_candidates_observed_cash_ledger_still_unproven_no_h508_admission"
            else:
                report["decision"] = "chain_join_or_settlement_incomplete_no_h508_admission"
    except Exception as exc:  # sealed bounded diagnostic; workflow remains publishable
        report["status"] = "incomplete"
        report["error_type"] = type(exc).__name__
        report["decision"] = "chain_join_incomplete_no_h508_admission"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://archive.pendulumflow.com")
    parser.add_argument("--hour", default="2026-09-03/17")
    parser.add_argument("--rpc-url", default=POLYGON_RPC_URL)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = probe(args.base, args.hour, rpc_url=args.rpc_url, max_bytes=args.max_bytes)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "decision", "transaction_join_match_count", "settlement_topic_receipt_count", "cash_ledger_proven")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

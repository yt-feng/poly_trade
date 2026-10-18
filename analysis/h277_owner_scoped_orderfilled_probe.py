"""Bounded public-chain OrderFilled join for signer-scoped evidence.

The caller supplies one or more wallet addresses that were derived in a
private runner step.  This module only reads the already public Polygon
OrderFilled log stream and joins the supplied addresses to the taker/maker
fields.  It never signs, submits, cancels, redeems, reads a balance, or treats
an on-chain event as a confirmed reusable-cash fill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

try:  # package import for tests, direct import for the Actions script entrypoint
    from .polygon_orderfilled_probe import POLYGON_RPC_URL, probe
except ImportError:  # pragma: no cover
    from polygon_orderfilled_probe import POLYGON_RPC_URL, probe


ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
MAX_OWNERS = 4
MAX_WINDOW_BLOCKS = 1_000_000


def normalize_owner(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not ADDRESS.fullmatch(text):
        raise ValueError("owner address must be a 20-byte hexadecimal address")
    return text


def _owner_digest(address: str) -> str:
    return hashlib.sha256(address.encode("ascii")).hexdigest()


def summarize_records(records: Iterable[dict[str, Any]], owners: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return counts and public event identities without exposing owner text."""
    rows = [row for row in records if isinstance(row, dict)]
    specs = list(owners)
    if not 1 <= len(specs) <= MAX_OWNERS:
        raise ValueError("one to four owner specifications are required")
    output: list[dict[str, Any]] = []
    for spec in specs:
        label = str(spec.get("label") or "").strip()
        address = normalize_owner(spec.get("address"))
        taker = [row for row in rows if str(row.get("taker") or "").lower() == address]
        maker = [row for row in rows if str(row.get("maker") or "").lower() == address]
        # Keep only public chain identity fields in the encrypted report.  The
        # address itself is represented by a digest; no wallet sample enters a
        # public step summary or repository file.
        def event_identity(row: dict[str, Any]) -> dict[str, Any]:
            return {key: row.get(key) for key in (
                "event_id", "tx_hash", "log_index", "block_number", "block_timestamp",
                "exchange_version", "exchange_address", "maker", "taker",
                "maker_asset_id", "taker_asset_id", "maker_amount_filled",
                "taker_amount_filled", "fee", "side", "token_id",
            ) if key in row}
        output.append({
            "label": label,
            "owner_address_sha256": _owner_digest(address),
            "public_orderfilled_taker_events": len(taker),
            "public_orderfilled_maker_events": len(maker),
            "taker_records": [event_identity(row) for row in taker],
            "maker_records": [event_identity(row) for row in maker],
            "owner_scoped_account_receipt": False,
            "settlement_evidence": False,
            "cash_release_evidence": False,
        })
    return output


def run(
    *,
    owners: Iterable[dict[str, Any]],
    rpc_url: str,
    window_blocks: int,
    chunk_blocks: int,
    max_logs: int,
) -> dict[str, Any]:
    if not 1 <= window_blocks <= MAX_WINDOW_BLOCKS:
        raise ValueError("window_blocks outside bounded range")
    if chunk_blocks <= 0 or max_logs <= 0:
        raise ValueError("probe bounds must be positive")
    owner_specs = list(owners)
    # Validate before network access so a malformed private-runner handoff
    # cannot consume a public RPC budget.
    for spec in owner_specs:
        normalize_owner(spec.get("address"))
    public = probe(
        rpc_url=rpc_url,
        window_blocks=window_blocks,
        chunk_blocks=chunk_blocks,
        max_logs=max_logs,
    )
    return {
        "format": "h277-owner-scoped-orderfilled-probe-v1",
        "source": public,
        "owner_summaries": summarize_records(public.get("records", []), owner_specs),
        "evidence_boundary": "public Polygon OrderFilled join only; no authenticated user-stream, settlement, cash-release or balance receipt",
        "strategy_evaluated": False,
        "new_data_collected": False,
        "model_refit": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "live_orders_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owners-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rpc-url", default=POLYGON_RPC_URL)
    parser.add_argument("--window-blocks", type=int, default=100_000)
    parser.add_argument("--chunk-blocks", type=int, default=200)
    parser.add_argument("--max-logs", type=int, default=2_000)
    args = parser.parse_args()
    owners = json.loads(args.owners_file.read_text(encoding="utf-8"))
    result = run(
        owners=owners,
        rpc_url=args.rpc_url,
        window_blocks=args.window_blocks,
        chunk_blocks=args.chunk_blocks,
        max_logs=args.max_logs,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "computed", "format": result["format"]}, sort_keys=True))


if __name__ == "__main__":
    main()

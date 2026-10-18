"""Bounded public Polymarket position/activity capture.

The CLOB SDK activity paginator and the public Data API are different serving
surfaces.  This helper reads the latter with the already-derived proxy wallet
address and keeps its evidence separate from authenticated account receipts.
It has no wallet, signing, order, redemption, or balance capability.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen


MAX_ROWS = 400
BASE_URL = "https://data-api.polymarket.com"


def _wallet(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 42 or not text.startswith("0x"):
        raise ValueError("wallet_address_must_be_20_byte_hex")
    try:
        int(text[2:], 16)
    except ValueError as exc:
        raise ValueError("wallet_address_must_be_20_byte_hex") from exc
    return text


def _fetch(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "poly-trade-public-lifecycle/1.0"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _bounded(value: Any) -> tuple[list[Any], bool]:
    if not isinstance(value, list):
        raise ValueError("public_data_response_must_be_list")
    return value[:MAX_ROWS], len(value) > MAX_ROWS


def capture(wallet_address: Any, *, fetcher: Callable[[str], Any] = _fetch) -> dict[str, Any]:
    wallet = _wallet(wallet_address)
    encoded = quote(wallet, safe="")
    positions_raw = fetcher(f"{BASE_URL}/positions?user={encoded}")
    activity_raw = fetcher(f"{BASE_URL}/activity?user={encoded}&limit={MAX_ROWS}")
    positions, positions_truncated = _bounded(positions_raw)
    activity, activity_truncated = _bounded(activity_raw)
    return {
        "format": "h283-public-position-activity-capture-v1",
        "wallet_address_sha256": hashlib.sha256(wallet.encode("ascii")).hexdigest(),
        "positions": positions,
        "positions_count": len(positions),
        "positions_truncated": positions_truncated,
        "activity": activity,
        "activity_count": len(activity),
        "activity_truncated": activity_truncated,
        "evidence_boundary": "public Data API positions/activity only; not an authenticated cash ledger or settlement receipt",
    }

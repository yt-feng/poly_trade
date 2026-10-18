"""Strict, future-only adapter for authenticated Polymarket PolyBolt BTC TWAP events.

This module deliberately does not alter the legacy settlement audit.  It accepts
only an explicitly authenticated, provenance-complete raw event and emits a
small canonical record for a later source-coverage audit.  It does not label a
strategy as ready and does not contain order, fill, settlement, or cash logic.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


TOPIC = "prices.crypto.twap"
SYMBOL = "btcusd"
WINDOW_SECONDS = 60
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class PolyBoltContractError(ValueError):
    """Raised when a raw PolyBolt event fails the source contract."""


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolyBoltContractError(f"{name} must be an object")
    return value


def _require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolyBoltContractError(f"{name} must be a non-empty string")
    return value


def _require_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise PolyBoltContractError(f"{name} must be a non-negative integer")
    return value


def _require_timestamp(value: Any, name: str) -> int:
    # Raw JSON timestamps are required to be explicit epoch milliseconds.  The
    # adapter must not guess seconds, milliseconds, or ISO timestamp units.
    return _require_integer(value, name)


def _require_decimal_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolyBoltContractError(f"{name} must be an exact decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PolyBoltContractError(f"{name} must be an exact decimal string") from None
    if not parsed.is_finite() or parsed <= 0:
        raise PolyBoltContractError(f"{name} must be finite and positive")
    return value


def _require_hash(value: Any, name: str) -> str:
    value = _require_nonempty_string(value, name)
    if not _HEX64.fullmatch(value):
        raise PolyBoltContractError(f"{name} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _window(payload: dict[str, Any]) -> int:
    # The official JSON examples use windowSeconds while the raw subscription
    # filter uses window_seconds.  Both are accepted only when they agree.
    if "window_s" in payload:
        raise PolyBoltContractError("legacy window_s is not accepted by the PolyBolt contract")
    camel = payload.get("windowSeconds")
    snake = payload.get("window_seconds")
    if camel is None and snake is None:
        raise PolyBoltContractError("payload window_seconds is required")
    if camel is not None and snake is not None and camel != snake:
        raise PolyBoltContractError("payload window_seconds fields conflict")
    value = camel if camel is not None else snake
    if type(value) is not int or value != WINDOW_SECONDS:
        raise PolyBoltContractError("payload window_seconds must be exactly 60")
    return value


def _common(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], int, int, int, str, str]:
    if raw.get("topic") != TOPIC:
        raise PolyBoltContractError("topic must be prices.crypto.twap")
    if raw.get("authenticated") is not True:
        raise PolyBoltContractError("authenticated must be explicitly true")
    seq = _require_integer(raw.get("seq"), "seq")
    dropped = raw.get("dropped")
    # Optional in the SDK type, but absence is not evidence that no messages
    # were dropped.  A quality-first capture must record an explicit zero.
    if type(dropped) is not int or dropped != 0:
        raise PolyBoltContractError("dropped must be explicitly recorded as integer zero")
    frame_timestamp_ms = _require_timestamp(raw.get("timestamp"), "timestamp")
    received_ms = _require_timestamp(raw.get("received_ms"), "received_ms")
    if frame_timestamp_ms > received_ms:
        raise PolyBoltContractError("timestamp cannot be after received_ms")
    connection_id = _require_nonempty_string(raw.get("connection_id"), "connection_id")
    sequence_scope = _require_nonempty_string(raw.get("sequence_scope"), "sequence_scope")
    _require_nonempty_string(raw.get("capture_id"), "capture_id")
    _require_hash(raw.get("raw_payload_sha256"), "raw_payload_sha256")
    payload = _require_dict(raw.get("payload"), "payload")
    if payload.get("symbol") != SYMBOL:
        raise PolyBoltContractError("payload.symbol must be btcusd")
    _window(payload)
    return raw, payload, frame_timestamp_ms, received_ms, seq, connection_id, sequence_scope


def normalize_polybolt_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one raw authenticated PolyBolt event.

    ``update`` events return one canonical price point.  ``subscribe`` events
    return a bounded historical snapshot and are marked as such; callers must
    not treat a snapshot alone as a continuous live stream.
    """
    raw, payload, frame_timestamp_ms, received_ms, seq, connection_id, sequence_scope = _common(_require_dict(raw, "event"))
    kind = raw.get("type")
    if kind not in {"update", "subscribe"}:
        raise PolyBoltContractError("type must be update or subscribe")
    base = {
        "protocol": "polybolt_crypto_twap_60",
        "topic": TOPIC,
        "symbol": SYMBOL,
        "window_seconds": WINDOW_SECONDS,
        "type": kind,
        "seq": seq,
        "dropped": 0,
        "frame_timestamp_ms": frame_timestamp_ms,
        "received_ms": received_ms,
        "connection_id": connection_id,
        "sequence_scope": sequence_scope,
        "capture_id": raw["capture_id"],
        "raw_payload_sha256": raw["raw_payload_sha256"].lower(),
        "authenticated": True,
        "reconnect_boundary": raw.get("reconnect_boundary") is True,
        "history_snapshot_boundary": kind == "subscribe",
    }
    if kind == "update":
        timestamp_ms = _require_timestamp(payload.get("timestamp"), "payload.timestamp")
        if timestamp_ms > received_ms:
            raise PolyBoltContractError("payload.timestamp cannot be after received_ms")
        value = _require_decimal_string(payload.get("value"), "payload.value")
        # PolyBolt values are already decimal values.  Keep the exact string;
        # no E18 conversion or float round-trip is allowed.
        base.update({"kind": "update", "event_ms": timestamp_ms, "value": value})
        return base

    points = payload.get("data")
    if not isinstance(points, list) or not points:
        raise PolyBoltContractError("subscribe payload.data must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    previous = None
    for index, point in enumerate(points):
        point = _require_dict(point, f"payload.data[{index}]")
        timestamp_ms = _require_timestamp(point.get("timestamp"), f"payload.data[{index}].timestamp")
        if timestamp_ms > received_ms:
            raise PolyBoltContractError("snapshot point cannot be after received_ms")
        if previous is not None and timestamp_ms <= previous:
            raise PolyBoltContractError("snapshot timestamps must be strictly increasing")
        previous = timestamp_ms
        normalized.append({"timestamp_ms": timestamp_ms, "value": _require_decimal_string(point.get("value"), f"payload.data[{index}].value")})
    base.update({"kind": "snapshot", "event_ms": normalized[-1]["timestamp_ms"], "points": normalized})
    return base


def build_continuity_manifest(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build a continuity manifest without silently repairing event order."""
    records = list(events)
    updates = [record for record in records if record.get("kind") == "update"]
    if not updates:
        raise PolyBoltContractError("at least one normalized update is required")
    scopes = {record.get("sequence_scope") for record in updates}
    connections = {record.get("connection_id") for record in updates}
    sequences = [record.get("seq") for record in updates]
    duplicate_count = len(sequences) - len(set(sequences))
    out_of_order_count = sum(1 for left, right in zip(sequences, sequences[1:]) if right <= left)
    gaps = 0
    if len(scopes) == 1 and len(connections) == 1 and sequences:
        gaps = sum(max(0, right - left - 1) for left, right in zip(sequences, sequences[1:]))
    reconnect_segments = len(connections)
    hashes = [record.get("raw_payload_sha256") for record in records]
    manifest = {
        "sequence_scope": sorted(str(scope) for scope in scopes),
        "first_seq": min(sequences),
        "last_seq": max(sequences),
        "gap_count": gaps,
        "duplicate_count": duplicate_count,
        "out_of_order_count": out_of_order_count,
        "reconnect_segments": reconnect_segments,
        "history_snapshot_boundary": any(record.get("history_snapshot_boundary") is True for record in records),
        "raw_payload_sha256": hashes,
        "capture_ids": sorted({str(record.get("capture_id")) for record in records}),
        "authenticated": all(record.get("authenticated") is True for record in records),
        "dropped_zero": all(record.get("dropped") == 0 for record in records),
    }
    manifest["continuity_ok"] = (
        len(scopes) == 1
        and len(connections) == 1
        and manifest["gap_count"] == 0
        and manifest["duplicate_count"] == 0
        and manifest["out_of_order_count"] == 0
        and manifest["authenticated"]
        and manifest["dropped_zero"]
    )
    return manifest


def require_continuity(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return a manifest only if a single gap-free authenticated segment exists."""
    manifest = build_continuity_manifest(events)
    if not manifest["continuity_ok"]:
        raise PolyBoltContractError("PolyBolt continuity manifest is not admissible")
    return manifest

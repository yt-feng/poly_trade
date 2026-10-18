"""Assemble one H4 row from already normalized evidence groups.

This adapter is intentionally offline.  It does not fetch a market, inspect a
wallet, or infer a whale trade from a public print.  Every group must already
be normalized and independently identified before it can be joined.  The
result still requires :func:`h4_settlement_grounded_whale_flow.validate_h4_chain`.
"""
from __future__ import annotations

from typing import Any, Mapping


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _required(mapping: Mapping[str, Any], field: str) -> Any:
    value = mapping.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field}_required")
    return value


def _copy(target: dict[str, Any], source: Mapping[str, Any], *, reserved: set[str], name: str) -> None:
    for key, value in source.items():
        if key in reserved:
            raise ValueError(f"{name}_contains_reserved_field:{key}")
        target[key] = value


def assemble_h4_whale_row(
    *,
    market: Mapping[str, Any],
    fill: Mapping[str, Any],
    whale: Mapping[str, Any],
    settlement: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Join one canonical market, taker fill, whale observation and ledger row."""
    groups = {"market": market, "fill": fill, "whale": whale, "settlement": settlement, "ledger": ledger}
    if any(not isinstance(value, Mapping) for value in groups.values()):
        raise ValueError("h4_evidence_groups_must_be_objects")

    owner = _text(_required(fill, "owner"))
    if _text(fill.get("trader_side")).upper() != "TAKER":
        raise ValueError("fill_trader_side_must_be_taker")
    if _text(fill.get("taker_address")).lower() != owner.lower():
        raise ValueError("fill_taker_owner_mismatch")
    if _text(fill.get("tx_status")).upper() not in {"CONFIRMED", "SUCCESS", "MINED"}:
        raise ValueError("fill_transaction_must_be_confirmed")
    if market.get("market_join_verified") is not True:
        raise ValueError("market_join_must_be_verified")
    if whale.get("whale_observation_verified") is not True:
        raise ValueError("whale_observation_must_be_verified")

    required_market = ("token_id", "condition_id", "market_slug", "outcome_index", "outcome_name", "source_version", "identity_hash")
    required_fill = ("transaction_hash", "block_number", "log_index", "trade_id", "order_hash", "maker_address", "taker_address", "filled_shares", "fill_price", "fee", "fee_schedule_source", "fee_schedule_observed_ts", "entry_ts")
    required_whale = ("whale_address", "whale_trade_id", "whale_transaction_hash", "whale_notional_usdc", "whale_observed_ts")
    for name, source, fields in (
        ("market", market, required_market),
        ("fill", fill, required_fill),
        ("whale", whale, required_whale),
    ):
        for field in fields:
            _required(source, field)

    protected = set(required_market) | set(required_fill) | set(required_whale) | {
        "owner", "trader_side", "tx_status", "market_join_verified", "whale_observation_verified",
    }
    row: dict[str, Any] = {}
    _copy(row, ledger, reserved=protected, name="ledger")
    _copy(row, settlement, reserved=protected, name="settlement")
    # These three groups are canonicalized before this adapter is called.  A
    # later update is intentional: their fields are the row's source of truth.
    row.update(market)
    row.update(fill)
    row.update(whale)
    for field in ("sequence_no", "ledger_id", "chain_id", "previous_checkpoint_id", "balance_checkpoint_id"):
        _required(row, field)
    row.update({
        "owner": owner,
        "trader_side": "TAKER",
        "tx_status": _text(fill["tx_status"]).upper(),
        "market_join_verified": True,
        "whale_observation_verified": True,
    })
    return row


__all__ = ["assemble_h4_whale_row"]

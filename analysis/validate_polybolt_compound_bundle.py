"""Offline consistency checks for a supplied PolyBolt compound bundle.

This is a stricter v2 boundary than the legacy authenticated bundle validator.
It accepts an already captured artifact only; it never connects to Polymarket,
reads credentials, submits orders, or turns public observations into fills.
Supplied authentication flags, identities and hashes are claims, not independent
receipt authentication.  This entrypoint cannot pass the actual canary gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from authenticated_compound_reconciler import reconcile_authenticated_chain
from polybolt_twap_adapter import (
    PolyBoltContractError,
    build_continuity_manifest,
    normalize_polybolt_event,
)


SCHEMA = "authenticated-polybolt-compound-bundle-v2"
OFFLINE_VERIFICATION = {
    "evidence_scope": "offline_bundle_consistency_only",
    "receipt_authenticity_verified": False,
    "actual_fills": 0,
    "verified_canary_fills": 0,
    "gate_passed": False,
}
MARKET_SLUG = re.compile(r"^btc-updown-5m-[0-9]+$")
REQUIRED_FILLS = 10
FROZEN_EXECUTION_ROLE = "TAKER"
# A public print or retrospective sign rule is not enough to identify the
# executed taker leg.  The authenticated bundle boundary therefore requires
# the settled fill event's side provenance on every row.  This is an evidence
# binding check only; it does not authenticate the supplied receipt.
FROZEN_TRADER_SIDE = "TAKER"
FROZEN_TRADER_SIDE_SOURCE = "SETTLED_TAKER_LEG"
FROZEN_TAKER_FEE_RATE = Decimal("0.07")
FEE_AMOUNT_TOLERANCE = Decimal("0.000001")
# The CLOB book's ``min_order_size`` is a quantity of shares.  Keep that venue
# field separate from the frozen H30 strategy protocol: every admitted H30
# fill must also carry at least $5 of entry notional, matching the paper
# compound replay.  This is a strategy-level execution floor, not a claim
# about a universal venue minimum or the unit of Gamma's orderMinSize field.
FROZEN_MINIMUM_ORDER_SIZE_UNIT = "shares"
FROZEN_MINIMUM_ORDER_SIZE = Decimal("5")
FROZEN_MINIMUM_ORDER_SHARES = Decimal("5")
FROZEN_MINIMUM_ENTRY_NOTIONAL_USD = Decimal("5")
MINIMUM_ENTRY_COST_FOR_LEDGER = Decimal("0.0000001")
DEFAULT_STRATEGY_PROFILE = "h30_legacy"
H530_STRATEGY_PROFILE = "h530_fixed_half_cash"
H534_STRATEGY_PROFILE = "h534_fixed_fraction_055"
H544_STRATEGY_PROFILE = "h544_adaptive_two_profit"
H546_STRATEGY_PROFILE = "h546_adaptive_one_profit"
H549_STRATEGY_PROFILE = "h549_adaptive_one_profit"
H561_STRATEGY_PROFILE = "h561_online_depth_proxy"
# H578 is a separately registered paper challenger.  It is not the default
# profile; the explicit thresholds prevent an owner bundle from being read as
# H561 by accident.
H578_STRATEGY_PROFILE = "h578_online_depth_proxy"
# H600 adds an entry-visible favorite-side ask floor to the H578 depth map.
# Keep it separate so a H578 bundle cannot be relabelled as H600 by changing
# only the top-level strategy id.
H600_STRATEGY_PROFILE = "h600_favorite_side_depth"
PRIVATE_STRATEGY_PROFILE = "private_exact"
TRACE_PROFILES = {
    H530_STRATEGY_PROFILE,
    H534_STRATEGY_PROFILE,
    H544_STRATEGY_PROFILE,
    H546_STRATEGY_PROFILE,
    H549_STRATEGY_PROFILE,
    H561_STRATEGY_PROFILE,
    H578_STRATEGY_PROFILE,
    H600_STRATEGY_PROFILE,
}
FROZEN_STRATEGY_ID = "h30_move_uncertainty"
FROZEN_STRATEGY_VERSION = "h54-execution-contract-prefix-v1"
FROZEN_STRATEGY_CONFIG = {
    "candidate": FROZEN_STRATEGY_ID,
    "score": "btc_move_2m * (1 - 2*abs(mid_up_prob_2m - 0.5))",
    "low_threshold": "-12.702899999999412",
    "high_threshold": "13.257220000004239",
    "entry_minute": 2,
    "fraction": "0.04",
    "required_fills": 10,
}
H530_STRATEGY_ID = "h530_fixed_half_cash"
H530_STRATEGY_VERSION = "h530-old-clean-fixed-half-cash-v1"
H530_STRATEGY_CONFIG = {
    "candidate": "H434_H452_FIXED_SIGNAL_CASH_FRACTION",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "gross_cash_fraction": "0.5",
    "policy": "fixed fraction of currently available cash",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}
H534_STRATEGY_ID = "h534_fixed_fraction_055"
H534_STRATEGY_VERSION = "h534-old-clean-fixed-055-v1"
H534_STRATEGY_CONFIG = {
    "candidate": "H534_DENSE_FIXED_CASH_FRACTION_GRID",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "signal": "H434_H452_FIXED_SIGNAL_UNDER_H506_STRESS",
    "gross_cash_fraction": "0.55",
    "policy": "fixed fraction of currently available cash",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "settlement_seconds": "300",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}
H544_STRATEGY_ID = "h544_adaptive_two_profit"
H544_STRATEGY_VERSION = "h544-old-clean-adaptive-two-profit-v1"
H544_STRATEGY_CONFIG = {
    "candidate": "H544_INTERMEDIATE_CASH_BOOST_AFTER_TWO_PROFITS",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "base_gross_cash_fraction": "0.55",
    "boost_gross_cash_fraction": "0.575",
    "boost_after_profitable_released_exits": 2,
    "reset_after_nonprofit": True,
    "policy": "boost only after two consecutive strictly profitable released exits; next entry remains on the same evolving cash ledger",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "settlement_seconds": "300",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}
H546_STRATEGY_ID = "h546_adaptive_one_profit"
H546_STRATEGY_VERSION = "h546-old-clean-adaptive-one-profit-v1"
H546_STRATEGY_CONFIG = {
    "candidate": "H546_BOOST_AFTER_ONE_PROFIT",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "base_gross_cash_fraction": "0.55",
    "boost_gross_cash_fraction": "0.575",
    "boost_after_profitable_released_exits": 1,
    "reset_after_nonprofit": True,
    "policy": "boost only after one strictly profitable released exit; next entry remains on the same evolving cash ledger",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "settlement_seconds": "300",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}
H549_STRATEGY_ID = "h549_adaptive_one_profit"
H549_STRATEGY_VERSION = "h549-old-clean-adaptive-one-profit-v1"
H549_STRATEGY_CONFIG = {
    "candidate": "H549_BOOST_05875_AFTER_ONE_PROFIT",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "base_gross_cash_fraction": "0.55",
    "boost_gross_cash_fraction": "0.5875",
    "boost_after_profitable_released_exits": 1,
    "reset_after_nonprofit": True,
    "policy": "boost only after one strictly profitable released exit; next entry remains on the same evolving cash ledger",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "settlement_seconds": "300",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}
H561_STRATEGY_ID = "h561_online_depth_proxy"
H561_STRATEGY_VERSION = "h561-old-clean-online-depth-proxy-v1"
H561_STRATEGY_CONFIG = {
    "candidate": "H561_ONLINE_ENTRY_DEPTH_PROXY_SIZING",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "depth_capacity_thresholds": ["4", "6", "8"],
    "gross_cash_fractions": ["0.5", "0.75", "0.9", "1.0"],
    "depth_source": "side-specific sell depth visible in the first two minutes",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "settlement_seconds": "300",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}
H578_STRATEGY_ID = "h578_online_depth_proxy"
H578_STRATEGY_VERSION = "h578-old-clean-online-depth-proxy-v1"
H578_STRATEGY_CONFIG = {
    "candidate": "H578_ENTRY_DEPTH_THRESHOLD_GRID_3_5_7",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "depth_capacity_thresholds": ["3", "5", "7"],
    "gross_cash_fractions": ["0.5", "0.75", "0.9", "1.0"],
    "depth_source": "side-specific sell depth visible in the first two minutes",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "settlement_seconds": "300",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}
H600_STRATEGY_ID = "h600_favorite_side_depth"
H600_STRATEGY_VERSION = "h600-old-clean-favorite-side-depth-v1"
H600_STRATEGY_CONFIG = {
    "candidate": "H600_FAVORITE_SIDE_ALIGNMENT_H578_DEPTH",
    "source_commit": "bbda5f2050685f63153621a54641ebdfab943487",
    "favorite_price_floor": "0.50",
    "depth_capacity_thresholds": ["3", "5", "7"],
    "gross_cash_fractions": ["0.5", "0.75", "0.9", "1.0"],
    "depth_source": "selected-side sell depth visible in the first two minutes",
    "favorite_price_source": "selected-side ask visible in the first two minutes",
    "entry_policy_is_online_observable": True,
    "taker_fee_rate": "0.07",
    "settlement_seconds": "300",
    "cash_release_delay_seconds": "900",
    "minimum_order_usd": "5",
    "minimum_order_shares": "5",
    "required_fills": 10,
}

STRATEGY_DECISION_TRACE_KEYS = {
    "decision_ref",
    "sequence_no",
    "cash_fraction",
    "profitable_released_exits_before",
}
H561_STRATEGY_DECISION_TRACE_KEYS = {
    "decision_ref",
    "sequence_no",
    "cash_fraction",
    "position_side",
    "entry_sell_depth_shares",
    "depth_capacity_ratio",
}
H578_STRATEGY_DECISION_TRACE_KEYS = H561_STRATEGY_DECISION_TRACE_KEYS
H600_STRATEGY_DECISION_TRACE_KEYS = H561_STRATEGY_DECISION_TRACE_KEYS | {"favorite_ask_price"}
DEPTH_TRACE_PROFILES = {H561_STRATEGY_PROFILE, H578_STRATEGY_PROFILE, H600_STRATEGY_PROFILE}
FAVORITE_DEPTH_TRACE_PROFILES = {H600_STRATEGY_PROFILE}
STRATEGY_SIZE_TOLERANCE = Decimal("0.000001")
FAVORITE_PRICE_FLOOR = Decimal("0.50")


def _strategy_profile(
    profile: str,
    *,
    private_strategy_config: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    if profile == PRIVATE_STRATEGY_PROFILE:
        if not isinstance(private_strategy_config, dict):
            raise ValueError("private_strategy_profile_unavailable")
        if set(private_strategy_config) != {"id", "version", "config"}:
            raise ValueError("private_strategy_profile_invalid")
        profile_id = private_strategy_config.get("id")
        profile_version = private_strategy_config.get("version")
        profile_config = private_strategy_config.get("config")
        if (
            not isinstance(profile_id, str)
            or not profile_id.strip()
            or not isinstance(profile_version, str)
            or not profile_version.strip()
            or not isinstance(profile_config, dict)
        ):
            raise ValueError("private_strategy_profile_invalid")
        return profile_id.strip(), profile_version.strip(), profile_config
    profiles = {
        DEFAULT_STRATEGY_PROFILE: (FROZEN_STRATEGY_ID, FROZEN_STRATEGY_VERSION, FROZEN_STRATEGY_CONFIG),
        H530_STRATEGY_PROFILE: (H530_STRATEGY_ID, H530_STRATEGY_VERSION, H530_STRATEGY_CONFIG),
        H534_STRATEGY_PROFILE: (H534_STRATEGY_ID, H534_STRATEGY_VERSION, H534_STRATEGY_CONFIG),
        H544_STRATEGY_PROFILE: (H544_STRATEGY_ID, H544_STRATEGY_VERSION, H544_STRATEGY_CONFIG),
        H546_STRATEGY_PROFILE: (H546_STRATEGY_ID, H546_STRATEGY_VERSION, H546_STRATEGY_CONFIG),
        H549_STRATEGY_PROFILE: (H549_STRATEGY_ID, H549_STRATEGY_VERSION, H549_STRATEGY_CONFIG),
        H561_STRATEGY_PROFILE: (H561_STRATEGY_ID, H561_STRATEGY_VERSION, H561_STRATEGY_CONFIG),
        H578_STRATEGY_PROFILE: (H578_STRATEGY_ID, H578_STRATEGY_VERSION, H578_STRATEGY_CONFIG),
        H600_STRATEGY_PROFILE: (H600_STRATEGY_ID, H600_STRATEGY_VERSION, H600_STRATEGY_CONFIG),
    }
    try:
        return profiles[profile]
    except KeyError as exc:
        raise ValueError("unknown_strategy_profile") from exc


FROZEN_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(FROZEN_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H530_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H530_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H534_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H534_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H544_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H544_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H546_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H546_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H549_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H549_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H561_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H561_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H578_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H578_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
H600_STRATEGY_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(H600_STRATEGY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def _fail(reason: str, *, event_count: int = 0, source_event_count: int = 0) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "required_fills": 10,
        "event_count": int(event_count),
        "source_event_count": int(source_event_count),
        "source_continuity_ok": False,
        **OFFLINE_VERIFICATION,
        "offline_consistency_passed": False,
        "failure_reason": reason,
        "capital_floor_diagnostic": {
            "protocol": "h643_capital_buffer_loss_ladder_v1",
            "same_evolving_ledger": True,
            "status": "not_evaluated",
        },
    }


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field}_must_be_finite_decimal")
    return parsed


def _expected_entry_terms(
    balance_before: Decimal,
    price: Decimal,
    fraction: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Mirror the frozen cash-fraction sizing rule using Decimal arithmetic."""
    gross_target = max(FROZEN_MINIMUM_ENTRY_NOTIONAL_USD, fraction * balance_before)
    shares = max(FROZEN_MINIMUM_ORDER_SHARES, gross_target / price)
    per_share = price + FROZEN_TAKER_FEE_RATE * price * (Decimal("1") - price)
    reserved = shares * per_share
    if reserved > balance_before:
        shares = min(shares, balance_before / per_share)
    cost = shares * price
    fee = shares * FROZEN_TAKER_FEE_RATE * price * (Decimal("1") - price)
    return shares, cost, fee


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_required")
    return value.strip()


def _hash(value: Any, field: str) -> str:
    """Require a canonical lowercase SHA-256 reference for bound evidence."""
    digest = _text(value, field).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{field}_invalid")
    return digest


def _validate_strategy_binding(
    document: dict[str, Any],
    *,
    strategy_profile: str = DEFAULT_STRATEGY_PROFILE,
    private_strategy_config: dict[str, Any] | None = None,
) -> str | None:
    try:
        expected_id, expected_version, expected_config = _strategy_profile(
            strategy_profile, private_strategy_config=private_strategy_config
        )
    except ValueError as exc:
        return str(exc)
    strategy = document.get("strategy")
    if not isinstance(strategy, dict):
        return "strategy_binding_required"
    if strategy.get("id") != expected_id:
        return "strategy_id_not_frozen"
    if strategy.get("version") != expected_version:
        return "strategy_version_not_frozen"
    try:
        digest = _text(strategy.get("config_sha256"), "strategy_config_sha256").lower()
    except ValueError as exc:
        return str(exc)
    expected_digest = hashlib.sha256(
        json.dumps(expected_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if digest != expected_digest:
        return "strategy_config_digest_mismatch"
    return None


def _market_windows(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
    windows = document.get("market_windows")
    if not isinstance(windows, list) or not windows:
        return {}, "market_windows_required"
    out: dict[str, dict[str, Any]] = {}
    for item in windows:
        if not isinstance(item, dict):
            return {}, "invalid_market_window"
        market_id = item.get("market_id")
        slug = item.get("slug")
        start = item.get("window_start_ms")
        end = item.get("window_end_ms")
        if not isinstance(market_id, str) or not market_id.strip():
            return {}, "market_id_required"
        if not isinstance(slug, str) or not MARKET_SLUG.fullmatch(slug):
            return {}, "btc_five_minute_slug_required"
        if not _integer(start) or not _integer(end) or end - start != 300_000:
            return {}, "btc_five_minute_window_required"
        if item.get("asset") != "btc":
            return {}, "btc_asset_required"
        if item.get("min_order_size_unit") != FROZEN_MINIMUM_ORDER_SIZE_UNIT:
            return {}, "minimum_order_size_unit_must_be_shares"
        try:
            min_order_size = _decimal(item.get("min_order_size"), "min_order_size")
            min_order_shares = _decimal(item.get("min_order_shares"), "min_order_shares")
            tick_size = _decimal(item.get("tick_size"), "tick_size")
        except ValueError as exc:
            return {}, str(exc)
        if min_order_size < FROZEN_MINIMUM_ORDER_SIZE:
            return {}, "market_minimum_order_size_below_frozen_floor"
        if min_order_shares < FROZEN_MINIMUM_ORDER_SHARES:
            return {}, "market_minimum_order_shares_below_frozen_floor"
        if tick_size <= 0:
            return {}, "tick_size_must_be_positive"
        try:
            slug_epoch_ms = int(slug.rsplit("-", 1)[1]) * 1000
        except (ValueError, IndexError):
            return {}, "btc_slug_epoch_invalid"
        if slug_epoch_ms != start:
            return {}, "btc_slug_window_start_mismatch"
        if market_id in out:
            return {}, "duplicate_market_id"
        out[market_id] = {
            "market_id": market_id,
            "slug": slug,
            "window_start_ms": start,
            "window_end_ms": end,
            "min_order_size": min_order_size,
            "min_order_shares": min_order_shares,
            "tick_size": tick_size,
        }
    return out, None


def _depth_snapshots(
    document: dict[str, Any],
    windows: dict[str, dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
    """Index ten authenticated pre-entry depth observations.

    A TWAP event is a price source, not an executable quote. The compound gate
    therefore requires a separate CLOB depth observation for every fill, with a
    stable snapshot id and payload digest that can be audited against the
    private capture. This function validates shape and timing only; it never
    fetches a book or infers depth from a TWAP value.
    """
    snapshots = document.get("depth_snapshots")
    if not isinstance(snapshots, list) or len(snapshots) != REQUIRED_FILLS:
        return {}, "exactly_ten_depth_snapshots_required"
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            return {}, "invalid_depth_snapshot"
        try:
            market_id = _text(snapshot.get("market_id"), "depth_market_id")
            snapshot_id = _text(snapshot.get("snapshot_id"), "depth_snapshot_id")
            capture_id = _text(snapshot.get("capture_id"), "depth_capture_id")
            seq = snapshot.get("seq")
            if type(seq) is not int or seq < 0:
                return {}, "depth_snapshot_seq_invalid"
            payload_hash = _hash(snapshot.get("raw_payload_sha256"), "depth_raw_payload_sha256")
            event_ms = snapshot.get("event_ms")
            received_ms = snapshot.get("received_ms")
            if not _integer(event_ms) or not _integer(received_ms) or received_ms < event_ms:
                return {}, "depth_snapshot_timestamp_invalid"
            available_shares = _decimal(snapshot.get("available_shares"), "depth_available_shares")
            ask_price = _decimal(snapshot.get("ask_price"), "depth_ask_price")
        except ValueError as exc:
            return {}, str(exc)
        window = windows.get(market_id)
        if window is None:
            return {}, "depth_snapshot_market_unknown"
        if not (window["window_start_ms"] <= event_ms <= window["window_end_ms"]):
            return {}, "depth_snapshot_outside_market_window"
        if available_shares < 0:
            return {}, "depth_available_shares_negative"
        if ask_price <= 0 or ask_price >= 1:
            return {}, "depth_ask_price_must_be_between_zero_and_one"
        tick_multiple = ask_price / window["tick_size"]
        if tick_multiple != tick_multiple.to_integral_value():
            return {}, "depth_ask_price_not_on_tick_size"
        key = (market_id, snapshot_id)
        if key in out:
            return {}, "duplicate_depth_snapshot_id"
        out[key] = {
            "market_id": market_id,
            "snapshot_id": snapshot_id,
            "capture_id": capture_id,
            "seq": seq,
            "raw_payload_sha256": payload_hash,
            "event_ms": event_ms,
            "received_ms": received_ms,
            "available_shares": available_shares,
            "ask_price": ask_price,
        }
        # H561/H578/H600 additionally bind the side-specific sell depth used by
        # their online sizing rules.  Keep these fields optional here so
        # legacy profiles retain their existing depth contract; both depth
        # profiles fail closed if either field is absent or malformed during
        # trace validation.
        if "position_side" in snapshot or "sell_available_shares_2m" in snapshot:
            position_side = snapshot.get("position_side")
            if position_side not in {"up", "down"}:
                return {}, "depth_position_side_invalid"
            try:
                sell_depth = _decimal(snapshot.get("sell_available_shares_2m"), "sell_available_shares_2m")
            except ValueError as exc:
                return {}, str(exc)
            if sell_depth < 0:
                return {}, "sell_available_shares_2m_negative"
            out[key]["position_side"] = position_side
            out[key]["sell_available_shares_2m"] = sell_depth
    return out, None


def _settlement_receipts(
    document: dict[str, Any],
    windows: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Check supplied settlement references; hashes do not authenticate origin.

    A terminal account event is still not enough to establish reusable cash.
    The receipt must carry the settlement transaction identity and an explicit
    non-reverted terminal status so an off-chain match or reverted execution
    cannot be promoted by matching only market, timestamp and payout.
    """
    receipts = document.get("settlement_receipts")
    if not isinstance(receipts, list) or len(receipts) != REQUIRED_FILLS:
        return {}, "exactly_ten_settlement_receipts_required"
    out: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return {}, "invalid_settlement_receipt"
        try:
            receipt_id = _text(receipt.get("receipt_id"), "settlement_receipt_id")
            sequence = receipt.get("sequence_no")
            if type(sequence) is not int or not 1 <= sequence <= REQUIRED_FILLS:
                return {}, "settlement_receipt_sequence_invalid"
            market_id = _text(receipt.get("market_id"), "settlement_market_id")
            settled_ms = receipt.get("settled_ts_ms")
            if not _integer(settled_ms):
                return {}, "settlement_timestamp_invalid"
            payload_hash = _hash(receipt.get("raw_payload_sha256"), "settlement_raw_payload_sha256")
            payout = _decimal(receipt.get("payout"), "settlement_payout")
            transaction_hash = _text(
                receipt.get("settlement_transaction_hash"), "settlement_transaction_hash"
            )
            settlement_status = _text(receipt.get("settlement_status"), "settlement_status").upper()
            settlement_reverted = receipt.get("settlement_reverted")
        except ValueError as exc:
            return {}, str(exc)
        window = windows.get(market_id)
        if window is None:
            return {}, "settlement_receipt_market_unknown"
        if payout < 0:
            return {}, "settlement_payout_negative"
        if settlement_status != "CONFIRMED":
            return {}, "settlement_status_must_be_confirmed"
        if type(settlement_reverted) is not bool:
            return {}, "settlement_reversion_status_required"
        if settlement_reverted:
            return {}, "settlement_must_not_be_reverted"
        if receipt_id in out:
            return {}, "duplicate_settlement_receipt_id"
        out[receipt_id] = {
            "receipt_id": receipt_id,
            "sequence_no": sequence,
            "market_id": market_id,
            "settled_ts_ms": settled_ms,
            "raw_payload_sha256": payload_hash,
            "payout": payout,
            "settlement_transaction_hash": transaction_hash,
            "settlement_status": settlement_status,
            "settlement_reverted": settlement_reverted,
        }
    if {row["sequence_no"] for row in out.values()} != set(range(1, REQUIRED_FILLS + 1)):
        return {}, "settlement_receipt_sequence_coverage_mismatch"
    return out, None


def _orderfilled_records(
    document: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Validate normalized canonical OrderFilled evidence for H600.

    The row's event id is only a reference. H600 carries the normalized event
    records alongside the ledger rows so a direct bundle caller cannot pass an
    arbitrary id without binding transaction, market, owner, side, quantity,
    price and collateral amount.
    """
    records = document.get("orderfilled_records")
    if not isinstance(records, list) or len(records) != REQUIRED_FILLS:
        return {}, "exactly_ten_orderfilled_records_required"
    out: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            return {}, "invalid_orderfilled_record"
        try:
            event_id = _text(record.get("event_id"), "orderfilled_event_id")
            tx_hash = _text(record.get("tx_hash"), "orderfilled_tx_hash")
            log_index = record.get("log_index")
            block_number = record.get("block_number")
            market_id = _text(record.get("market_id"), "orderfilled_market_id")
            taker = _text(record.get("taker"), "orderfilled_taker").lower()
            side = _text(record.get("side"), "orderfilled_side").upper()
            sign = record.get("sign")
            size = _decimal(record.get("size"), "orderfilled_size")
            price = _decimal(record.get("price"), "orderfilled_price")
            cash_amount = _decimal(record.get("cash_amount"), "orderfilled_cash_amount")
        except ValueError as exc:
            return {}, str(exc)
        if not _integer(log_index) or not _integer(block_number):
            return {}, "orderfilled_identity_invalid"
        if event_id != f"{tx_hash}:{log_index}":
            return {}, "orderfilled_event_id_mismatch"
        if side != "BUY" or sign != 1:
            return {}, "orderfilled_not_buy_entry"
        if size <= 0 or cash_amount <= 0 or not (Decimal("0") < price < Decimal("1")):
            return {}, "orderfilled_amount_or_price_invalid"
        if event_id in out:
            return {}, "duplicate_orderfilled_event_id"
        out[event_id] = {
            "event_id": event_id,
            "tx_hash": tx_hash,
            "market_id": market_id,
            "taker": taker,
            "size": size,
            "price": price,
            "cash_amount": cash_amount,
        }
    return out, None


def _validate_orderfilled_bindings(
    document: dict[str, Any],
    events: list[dict[str, Any]],
    orderfilled_by_id: dict[str, dict[str, Any]],
) -> str | None:
    owner = _text(document.get("owner"), "bundle_owner")
    used: set[str] = set()
    for row in events:
        event_id = _text(row.get("orderfilled_event_id"), "orderfilled_event_id")
        if event_id in used:
            return "orderfilled_event_id_reused"
        used.add(event_id)
        fill = orderfilled_by_id.get(event_id)
        if fill is None:
            return "orderfilled_event_not_in_bundle"
        if fill["tx_hash"] != _text(row.get("transaction_ref"), "transaction_ref"):
            return "orderfilled_transaction_mismatch"
        if fill["market_id"] != _text(row.get("market_id"), "event_market_id"):
            return "orderfilled_market_mismatch"
        if fill["taker"] != owner.lower():
            return "orderfilled_taker_owner_mismatch"
        try:
            if abs(fill["size"] - _decimal(row.get("shares"), "shares")) > FEE_AMOUNT_TOLERANCE:
                return "orderfilled_size_mismatch"
            if abs(fill["price"] - _decimal(row.get("price"), "price")) > FEE_AMOUNT_TOLERANCE:
                return "orderfilled_price_mismatch"
            if abs(fill["cash_amount"] - _decimal(row.get("entry_cost"), "entry_cost")) > FEE_AMOUNT_TOLERANCE:
                return "orderfilled_cash_amount_mismatch"
        except ValueError as exc:
            return str(exc)
    if used != set(orderfilled_by_id):
        return "orderfilled_record_coverage_mismatch"
    return None


def _validate_same_ledger(
    document: dict[str, Any],
    events: list[dict[str, Any]],
    windows: dict[str, dict[str, Any]],
    source_by_ref: dict[tuple[str, str, int], dict[str, Any]],
    depth_by_ref: dict[tuple[str, str], dict[str, Any]],
    settlement_by_id: dict[str, dict[str, Any]],
    *,
    required_fills: int,
    require_order_identity: bool = False,
) -> str | None:
    """Require a literal ten-row, linked, amount-reconciled cash chain.

    The older v1 boundary replayed rows from a fresh initial balance and could
    therefore accept two independently reset ledgers concatenated together.
    v2 carries the ledger/chain identity, sequence links, numeric balance
    checkpoints and per-fill PolyBolt references needed to reject that case.
    """
    if required_fills != REQUIRED_FILLS:
        return "required_fills_fixed_at_ten"
    try:
        ledger_id = _text(document.get("ledger_id"), "ledger_id")
        chain_id = _text(document.get("chain_id"), "chain_id")
        starting_checkpoint = _text(document.get("starting_checkpoint_id"), "starting_checkpoint_id")
        initial_cash = _decimal(document.get("initial_cash"), "initial_cash")
        closing_balance = _decimal(document.get("closing_balance"), "closing_balance")
        _text(document.get("closing_balance_checkpoint_id"), "closing_balance_checkpoint_id")
    except ValueError as exc:
        return str(exc)
    if initial_cash != Decimal("10"):
        return "initial_cash_must_equal_ten"
    if len(events) != REQUIRED_FILLS:
        return "exactly_ten_account_events_required"
    # Check sequence types before sorting.  Mixed JSON types (for example a
    # string and a list) are not orderable in Python and must be rejected as a
    # malformed bundle rather than escaping as a TypeError.
    if any(type(row.get("sequence_no")) is not int for row in events):
        return "sequence_no_invalid"
    ordered = sorted(events, key=lambda row: row.get("sequence_no", -1))
    sequence_values = [row.get("sequence_no") for row in ordered]
    if sequence_values != list(range(1, REQUIRED_FILLS + 1)):
        return "sequence_no_must_be_exactly_one_to_ten"
    seen_source_refs: set[tuple[str, str, int]] = set()
    seen_checkpoints: set[str] = set()
    seen_receipts: set[str] = set()
    seen_strategy_refs: set[str] = set()
    seen_depth_refs: set[tuple[str, str]] = set()
    # The account event is only one leg of the execution join.  Keep the
    # trade, transaction and canonical OrderFilled identities distinct across
    # all ten rows so a direct validator caller cannot silently replay one
    # execution under several sequence numbers.
    seen_trade_ids: set[str] = set()
    seen_transaction_refs: set[str] = set()
    seen_orderfilled_event_ids: set[str] = set()
    seen_order_ids: set[str] = set()
    previous_after = initial_cash
    previous_checkpoint = starting_checkpoint
    previous_entry_ms: int | None = None
    released_receipt_ids: set[str] = set()
    # The receipts are the authoritative release records.  A receipt can only
    # be consumed once, and its amount must equal the corresponding payout.
    receipts = document.get("cash_release_receipts")
    if not isinstance(receipts, list) or len(receipts) != REQUIRED_FILLS:
        return "exactly_ten_cash_release_receipts_required"
    receipt_by_id: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return "invalid_cash_release_receipt"
        try:
            rid = _text(receipt.get("receipt_id"), "cash_release_receipt_id")
            seq = receipt.get("sequence_no")
            if type(seq) is not int or seq < 1 or seq > REQUIRED_FILLS:
                return "cash_release_receipt_sequence_invalid"
            amount = _decimal(receipt.get("amount"), "cash_release_amount")
            release_ms = receipt.get("release_ts_ms")
            if not _integer(release_ms):
                return "cash_release_timestamp_invalid"
        except ValueError as exc:
            return str(exc)
        if rid in receipt_by_id:
            return "duplicate_cash_release_receipt_id"
        receipt_by_id[rid] = {"receipt_id": rid, "sequence_no": seq, "amount": amount, "release_ts_ms": release_ms}
    for row in ordered:
        try:
            if _text(row.get("ledger_id"), "event_ledger_id") != ledger_id:
                return "ledger_id_mismatch"
            if _text(row.get("chain_id"), "event_chain_id") != chain_id:
                return "chain_id_mismatch"
            # Only the terminal account-event confirmation is admissible here.
            # Polymarket's order lifecycle also exposes intermediate states
            # such as matched, delayed and mined; none proves a settled fill
            # or reusable cash, so they must fail closed at this boundary.
            if row.get("status") != "confirmed":
                return "authenticated_event_not_confirmed"
            if row.get("cash_reconciled") is not True:
                return "cash_reconciliation_not_confirmed"
            if row.get("independent_checkpoint") is not True:
                return "independent_checkpoint_not_confirmed"
            sequence = row.get("sequence_no")
            if type(sequence) is not int or sequence < 1 or sequence > REQUIRED_FILLS:
                return "sequence_no_invalid"
            entry_ms = row.get("entry_ts_ms")
            release_ms = row.get("cash_available_ts_ms")
            if not _integer(entry_ms) or not _integer(release_ms) or release_ms < entry_ms:
                return "account_timestamp_invalid"
            if previous_entry_ms is not None and entry_ms <= previous_entry_ms:
                return "entry_timestamps_must_increase"
            previous_entry_ms = entry_ms
            market_id = _text(row.get("market_id"), "event_market_id")
            window = windows.get(market_id)
            if window is None or not (window["window_start_ms"] <= entry_ms <= window["window_end_ms"]):
                return "entry_outside_market_window"
            trade_id = _text(row.get("trade_id"), "trade_id")
            if trade_id in seen_trade_ids:
                return "trade_id_reused"
            seen_trade_ids.add(trade_id)
            transaction_ref = _text(row.get("transaction_ref"), "transaction_ref")
            if transaction_ref in seen_transaction_refs:
                return "transaction_ref_reused"
            seen_transaction_refs.add(transaction_ref)
            orderfilled_event_id = _text(row.get("orderfilled_event_id"), "orderfilled_event_id")
            if orderfilled_event_id in seen_orderfilled_event_ids:
                return "orderfilled_event_id_reused"
            seen_orderfilled_event_ids.add(orderfilled_event_id)
            if require_order_identity:
                order_id = _text(row.get("order_id"), "order_id")
                if order_id in seen_order_ids:
                    return "order_id_reused"
                seen_order_ids.add(order_id)
                order_status = _text(row.get("order_status"), "order_status").upper()
                if order_status not in {"MATCHED", "ORDER_STATUS_MATCHED"}:
                    return "order_status_must_be_matched"
            source_ref = row.get("source_ref")
            if not isinstance(source_ref, dict):
                return "source_ref_required_per_fill"
            capture_id = _text(source_ref.get("capture_id"), "source_capture_id")
            source_seq = source_ref.get("seq")
            source_hash = _text(source_ref.get("raw_payload_sha256"), "source_raw_payload_sha256").lower()
            source_event_ms = source_ref.get("event_ms")
            if type(source_seq) is not int or source_seq < 0 or not _integer(source_event_ms):
                return "source_ref_invalid"
            # Validate the sequence type before using it in a set/dict key.
            # Malformed JSON values such as a list are otherwise unhashable
            # and would escape the fail-closed validator as a TypeError.
            key = (market_id, capture_id, source_seq)
            if key in seen_source_refs:
                return "source_ref_reused"
            seen_source_refs.add(key)
            source = source_by_ref.get(key)
            if source is None or source.get("raw_payload_sha256") != source_hash or source.get("event_ms") != source_event_ms:
                return "source_ref_not_in_bundle"
            if not (window["window_start_ms"] <= source_event_ms <= window["window_end_ms"]):
                return "source_event_outside_market_window"
            if source_event_ms > entry_ms:
                return "source_event_after_entry"
            depth_ref = row.get("depth_ref")
            if not isinstance(depth_ref, dict):
                return "depth_ref_required_per_fill"
            depth_snapshot_id = _text(depth_ref.get("snapshot_id"), "depth_snapshot_id_ref")
            depth_capture_id = _text(depth_ref.get("capture_id"), "depth_capture_id_ref")
            depth_seq = depth_ref.get("seq")
            depth_hash = _hash(depth_ref.get("raw_payload_sha256"), "depth_raw_payload_sha256_ref")
            depth_event_ms = depth_ref.get("event_ms")
            if type(depth_seq) is not int or depth_seq < 0 or not _integer(depth_event_ms):
                return "depth_ref_invalid"
            depth_key = (market_id, depth_snapshot_id)
            if depth_key in seen_depth_refs:
                return "depth_ref_reused"
            seen_depth_refs.add(depth_key)
            depth = depth_by_ref.get(depth_key)
            if (
                depth is None
                or depth["capture_id"] != depth_capture_id
                or depth["seq"] != depth_seq
                or depth["raw_payload_sha256"] != depth_hash
                or depth["event_ms"] != depth_event_ms
            ):
                return "depth_ref_not_in_bundle"
            if depth["event_ms"] > entry_ms or depth["received_ms"] > entry_ms:
                return "depth_snapshot_after_entry"
            checkpoint = _text(row.get("balance_checkpoint_id"), "balance_checkpoint_id")
            if checkpoint in seen_checkpoints:
                return "duplicate_balance_checkpoint_id"
            seen_checkpoints.add(checkpoint)
            strategy_ref = _text(row.get("strategy_decision_ref"), "strategy_decision_ref")
            strategy_digest = _text(row.get("strategy_decision_sha256"), "strategy_decision_sha256").lower()
            if strategy_ref in seen_strategy_refs:
                return "strategy_decision_ref_reused"
            if not re.fullmatch(r"[0-9a-f]{64}", strategy_digest):
                return "strategy_decision_hash_invalid"
            seen_strategy_refs.add(strategy_ref)
            if _text(row.get("previous_checkpoint_id"), "previous_checkpoint_id") != previous_checkpoint:
                return "previous_checkpoint_link_mismatch"
            receipt_id = _text(row.get("cash_release_receipt_id"), "cash_release_receipt_id")
            if receipt_id in seen_receipts or receipt_id not in receipt_by_id:
                return "cash_release_receipt_link_invalid"
            seen_receipts.add(receipt_id)
            receipt = receipt_by_id[receipt_id]
            if receipt["sequence_no"] != sequence:
                return "cash_release_receipt_sequence_mismatch"
            # A fill cannot release its own payout before (or at the same
            # instant as) its entry.  Only receipts from earlier fills may
            # fund the current balance; the current receipt is available for
            # a later entry or for the closing-balance reconciliation.
            if receipt["release_ts_ms"] <= entry_ms:
                return "cash_release_before_entry"
            settlement_id = _text(row.get("settlement_receipt_id"), "settlement_receipt_id")
            settlement = settlement_by_id.get(settlement_id)
            if settlement is None:
                return "settlement_receipt_not_in_bundle"
            if settlement["sequence_no"] != sequence or settlement["market_id"] != market_id:
                return "settlement_receipt_link_mismatch"
            if settlement["settlement_transaction_hash"] != transaction_ref:
                return "settlement_transaction_mismatch"
            if settlement["settled_ts_ms"] < entry_ms:
                return "settlement_before_entry"
            if settlement["settled_ts_ms"] > release_ms:
                return "settlement_after_cash_release"
            entry_cost = _decimal(row.get("entry_cost"), "entry_cost")
            fee = _decimal(row.get("fee"), "fee")
            payout = _decimal(row.get("payout"), "payout")
            shares = _decimal(row.get("shares"), "shares")
            price = _decimal(row.get("price"), "price")
            balance_before = _decimal(row.get("balance_before"), "balance_before")
            balance_after = _decimal(row.get("balance_after"), "balance_after")
            released_cash = _decimal(row.get("released_cash_since_previous_entry"), "released_cash_since_previous_entry")
            if entry_cost < 0 or fee < 0 or payout < 0 or released_cash < 0:
                return "negative_cash_amount"
            if _text(row.get("execution_role"), "execution_role") != FROZEN_EXECUTION_ROLE:
                return "execution_role_must_be_taker"
            if _text(row.get("trader_side"), "trader_side").upper() != FROZEN_TRADER_SIDE:
                return "trader_side_must_be_taker"
            if _text(row.get("trader_side_source"), "trader_side_source").upper() != FROZEN_TRADER_SIDE_SOURCE:
                return "trader_side_source_must_be_settled_taker_leg"
            try:
                fee_rate = _decimal(row.get("fee_rate"), "fee_rate")
            except ValueError as exc:
                return str(exc)
            if fee_rate != FROZEN_TAKER_FEE_RATE:
                return "fee_rate_must_equal_frozen_taker_rate"
            if fee <= 0:
                return "zero_fee_not_admissible"
            if shares <= 0:
                return "shares_must_be_positive"
            if shares < window["min_order_shares"]:
                return "shares_below_market_minimum"
            if entry_cost < FROZEN_MINIMUM_ENTRY_NOTIONAL_USD:
                return "entry_cost_below_frozen_minimum_notional"
            if depth["available_shares"] < shares:
                return "depth_available_shares_insufficient"
            if price <= 0 or price >= 1:
                return "price_must_be_between_zero_and_one"
            if price < depth["ask_price"]:
                return "executed_price_below_depth_ask"
            tick_multiple = price / window["tick_size"]
            if tick_multiple != tick_multiple.to_integral_value():
                return "price_not_on_tick_size"
            if entry_cost != shares * price:
                return "entry_cost_price_arithmetic_mismatch"
            expected_fee = shares * fee_rate * price * (Decimal("1") - price)
            if abs(fee - expected_fee) > FEE_AMOUNT_TOLERANCE:
                return "taker_fee_amount_mismatch"
            if receipt["amount"] != payout or receipt["release_ts_ms"] != release_ms:
                return "cash_release_amount_mismatch"
            if settlement["payout"] != payout:
                return "settlement_payout_mismatch"
            due = Decimal("0")
            due_receipt_ids: set[str] = set()
            for prior in ordered:
                if prior is row:
                    break
                prior_receipt = receipt_by_id.get(prior.get("cash_release_receipt_id"))
                if (
                    prior_receipt
                    and prior_receipt["release_ts_ms"] == entry_ms
                    and prior_receipt["receipt_id"] not in released_receipt_ids
                ):
                    return "cash_release_timestamp_must_precede_next_entry"
                if prior_receipt and prior_receipt["release_ts_ms"] < entry_ms and prior_receipt["receipt_id"] not in released_receipt_ids:
                    due += prior_receipt["amount"]
                    due_receipt_ids.add(prior_receipt["receipt_id"])
            if released_cash != due:
                return "released_cash_amount_mismatch"
            expected_before = previous_after + due
            if balance_before != expected_before:
                return "balance_before_arithmetic_mismatch"
            expected_after = balance_before - entry_cost - fee
            if balance_after != expected_after:
                return "balance_after_arithmetic_mismatch"
            if balance_after < 0:
                return "negative_balance_after_entry"
            released_receipt_ids.update(due_receipt_ids)
            previous_after = balance_after
            previous_checkpoint = checkpoint
        except ValueError as exc:
            return str(exc)
    # All ten fills must have a release receipt, and the closing balance must
    # include every receipt that was not released before the tenth entry.
    if seen_receipts != set(receipt_by_id):
        return "cash_release_receipt_coverage_mismatch"
    final_expected = previous_after + sum(
        receipt["amount"] for rid, receipt in receipt_by_id.items() if rid not in released_receipt_ids
    )
    if closing_balance != final_expected:
        return "closing_balance_arithmetic_mismatch"
    return None


def _validate_strategy_decision_trace(
    document: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    strategy_profile: str,
    strategy_config: dict[str, Any],
    depth_by_ref: dict[tuple[str, str], dict[str, Any]],
) -> str | None:
    """Bind each account event to the frozen online sizing state.

    A top-level profile digest alone does not prove that every fill used that
    profile.  Known public profiles therefore carry a compact per-fill trace;
    each event's existing decision hash must commit to its trace row, and the
    trace state must follow the declared profitable-release transition rule.
    This remains an offline evidence check; it never authenticates the rows.
    """
    if strategy_profile not in TRACE_PROFILES:
        return None
    raw_trace = document.get("strategy_decisions")
    if not isinstance(raw_trace, list) or len(raw_trace) != len(events):
        return "strategy_decision_trace_required"
    trace_keys = (
        H600_STRATEGY_DECISION_TRACE_KEYS
        if strategy_profile in FAVORITE_DEPTH_TRACE_PROFILES
        else H561_STRATEGY_DECISION_TRACE_KEYS
        if strategy_profile in DEPTH_TRACE_PROFILES
        else STRATEGY_DECISION_TRACE_KEYS
    )
    by_ref: dict[str, dict[str, Any]] = {}
    for raw in raw_trace:
        if not isinstance(raw, dict) or set(raw) != trace_keys:
            return "strategy_decision_trace_invalid"
        try:
            ref = _text(raw.get("decision_ref"), "strategy_trace_decision_ref")
            sequence = raw.get("sequence_no")
            if type(sequence) is not int or sequence < 1:
                return "strategy_trace_sequence_invalid"
            fraction = _decimal(raw.get("cash_fraction"), "strategy_trace_cash_fraction")
            if strategy_profile in DEPTH_TRACE_PROFILES:
                position_side = _text(raw.get("position_side"), "strategy_trace_position_side").lower()
                if position_side not in {"up", "down"}:
                    return "strategy_trace_position_side_invalid"
                entry_sell_depth = _decimal(
                    raw.get("entry_sell_depth_shares"), "strategy_trace_entry_sell_depth_shares"
                )
                capacity_ratio = _decimal(
                    raw.get("depth_capacity_ratio"), "strategy_trace_depth_capacity_ratio"
                )
                if entry_sell_depth < 0 or capacity_ratio < 0:
                    return "strategy_trace_depth_negative"
                favorite_ask = None
                if strategy_profile in FAVORITE_DEPTH_TRACE_PROFILES:
                    favorite_ask = _decimal(raw.get("favorite_ask_price"), "strategy_trace_favorite_ask_price")
                    if favorite_ask < FAVORITE_PRICE_FLOOR or favorite_ask >= Decimal("1"):
                        return "strategy_trace_favorite_ask_below_floor"
                profit_count = None
            else:
                profit_count = raw.get("profitable_released_exits_before")
                if type(profit_count) is not int or profit_count < 0:
                    return "strategy_trace_profit_count_invalid"
        except ValueError as exc:
            return str(exc)
        if ref in by_ref:
            return "strategy_trace_decision_ref_reused"
        trace_row = {
            "decision_ref": ref,
            "sequence_no": sequence,
            "cash_fraction": str(raw["cash_fraction"]),
        }
        if strategy_profile in DEPTH_TRACE_PROFILES:
            trace_row.update({
                "position_side": position_side,
                "entry_sell_depth_shares": str(raw["entry_sell_depth_shares"]),
                "depth_capacity_ratio": str(raw["depth_capacity_ratio"]),
            })
            if strategy_profile in FAVORITE_DEPTH_TRACE_PROFILES:
                trace_row["favorite_ask_price"] = str(raw["favorite_ask_price"])
        else:
            trace_row["profitable_released_exits_before"] = profit_count
        by_ref[ref] = trace_row
    if len(by_ref) != len(events):
        return "strategy_decision_trace_coverage_mismatch"
    ordered = sorted(events, key=lambda row: row.get("sequence_no", -1))
    prior_profitable = 0
    for row in ordered:
        ref = _text(row.get("strategy_decision_ref"), "strategy_decision_ref")
        trace = by_ref.get(ref)
        if trace is None:
            return "strategy_decision_trace_ref_missing"
        if trace["sequence_no"] != row.get("sequence_no"):
            return "strategy_decision_trace_sequence_mismatch"
        expected_hash = hashlib.sha256(
            json.dumps(trace, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        if _text(row.get("strategy_decision_sha256"), "strategy_decision_sha256").lower() != expected_hash:
            return "strategy_decision_trace_hash_mismatch"
        if strategy_profile in DEPTH_TRACE_PROFILES:
            position_side = _text(row.get("position_side"), "event_position_side").lower()
            if position_side not in {"up", "down"}:
                return "event_position_side_invalid"
            depth_ref = row.get("depth_ref")
            if not isinstance(depth_ref, dict):
                return "depth_ref_required_per_fill"
            depth_key = (_text(row.get("market_id"), "event_market_id"), _text(depth_ref.get("snapshot_id"), "depth_snapshot_id_ref"))
            depth = depth_by_ref.get(depth_key)
            if depth is None or "sell_available_shares_2m" not in depth:
                return "h561_side_specific_sell_depth_required"
            if depth.get("position_side") != position_side:
                return "h561_depth_position_side_mismatch"
            if trace["position_side"] != position_side:
                return "strategy_trace_position_side_mismatch"
            sell_depth = _decimal(depth["sell_available_shares_2m"], "sell_available_shares_2m")
            trace_sell_depth = _decimal(trace["entry_sell_depth_shares"], "strategy_trace_entry_sell_depth_shares")
            if trace_sell_depth != sell_depth:
                return "strategy_trace_sell_depth_mismatch"
            if strategy_profile in FAVORITE_DEPTH_TRACE_PROFILES:
                favorite_ask = _decimal(trace["favorite_ask_price"], "strategy_trace_favorite_ask_price")
                if favorite_ask < FAVORITE_PRICE_FLOOR:
                    return "strategy_trace_favorite_ask_below_floor"
                if abs(favorite_ask - depth["ask_price"]) > STRATEGY_SIZE_TOLERANCE:
                    return "strategy_trace_favorite_ask_mismatch"
            try:
                balance_before = _decimal(row.get("balance_before"), "balance_before")
                price = _decimal(row.get("price"), "price")
                actual_shares = _decimal(row.get("shares"), "shares")
                actual_cost = _decimal(row.get("entry_cost"), "entry_cost")
                actual_fee = _decimal(row.get("fee"), "fee")
                full_shares, _, _ = _expected_entry_terms(balance_before, price, Decimal("1"))
            except ValueError as exc:
                return str(exc)
            expected_ratio = sell_depth / full_shares if full_shares > 0 else Decimal("0")
            trace_ratio = _decimal(trace["depth_capacity_ratio"], "strategy_trace_depth_capacity_ratio")
            if abs(trace_ratio - expected_ratio) > STRATEGY_SIZE_TOLERANCE:
                return "strategy_trace_depth_ratio_mismatch"
            thresholds = tuple(_decimal(item, "depth_capacity_threshold") for item in strategy_config["depth_capacity_thresholds"])
            expected_fraction = Decimal("1") if expected_ratio >= thresholds[2] else (
                Decimal("0.9") if expected_ratio >= thresholds[1] else (
                    Decimal("0.75") if expected_ratio >= thresholds[0] else Decimal("0.5")
                )
            )
            expected_profit_count = None
        elif strategy_profile in {H530_STRATEGY_PROFILE, H534_STRATEGY_PROFILE}:
            expected_fraction = _decimal(strategy_config["gross_cash_fraction"], "gross_cash_fraction")
            expected_profit_count = 0
        else:
            expected_profit_count = prior_profitable
            fraction_key = (
                "boost_gross_cash_fraction"
                if prior_profitable >= int(strategy_config["boost_after_profitable_released_exits"])
                else "base_gross_cash_fraction"
            )
            expected_fraction = _decimal(strategy_config[fraction_key], fraction_key)
        if strategy_profile not in DEPTH_TRACE_PROFILES and trace["profitable_released_exits_before"] != expected_profit_count:
            return "strategy_decision_trace_state_mismatch"
        if _decimal(trace["cash_fraction"], "strategy_trace_cash_fraction") != expected_fraction:
            return "strategy_decision_trace_fraction_mismatch"
        try:
            balance_before = _decimal(row.get("balance_before"), "balance_before")
            price = _decimal(row.get("price"), "price")
            actual_shares = _decimal(row.get("shares"), "shares")
            actual_cost = _decimal(row.get("entry_cost"), "entry_cost")
            actual_fee = _decimal(row.get("fee"), "fee")
            expected_shares, expected_cost, expected_fee = _expected_entry_terms(
                balance_before, price, expected_fraction
            )
        except ValueError as exc:
            return str(exc)
        if (
            abs(actual_shares - expected_shares) > STRATEGY_SIZE_TOLERANCE
            or abs(actual_cost - expected_cost) > STRATEGY_SIZE_TOLERANCE
            or abs(actual_fee - expected_fee) > STRATEGY_SIZE_TOLERANCE
        ):
            return "strategy_entry_size_mismatch"
        if strategy_profile not in {H530_STRATEGY_PROFILE, H534_STRATEGY_PROFILE, *DEPTH_TRACE_PROFILES}:
            payout = _decimal(row.get("payout"), "payout")
            entry_cost = _decimal(row.get("entry_cost"), "entry_cost")
            fee = _decimal(row.get("fee"), "fee")
            if payout > entry_cost + fee:
                prior_profitable += 1
            elif strategy_config.get("reset_after_nonprofit") is True:
                prior_profitable = 0
    return None


def validate_bundle_document(
    document: Any,
    *,
    required_fills: int = 10,
    strategy_profile: str = DEFAULT_STRATEGY_PROFILE,
    private_strategy_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check supplied fields without authenticating receipts or exposing owners."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return _fail("invalid_bundle_schema")
    if type(required_fills) is not int or required_fills <= 0:
        return _fail("invalid_required_fills")
    if document.get("required_fills") != REQUIRED_FILLS or required_fills != REQUIRED_FILLS:
        return _fail("required_fills_fixed_at_ten", event_count=len(document.get("events", [])) if isinstance(document.get("events"), list) else 0)
    strategy_error = _validate_strategy_binding(
        document,
        strategy_profile=strategy_profile,
        private_strategy_config=private_strategy_config,
    )
    if strategy_error:
        return _fail(strategy_error, event_count=len(document.get("events", [])) if isinstance(document.get("events"), list) else 0)
    owner = document.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return _fail("bundle_owner_required")
    source_events = document.get("source_events")
    events = document.get("events")
    if not isinstance(source_events, list) or not source_events:
        return _fail("source_events_required", event_count=len(events) if isinstance(events, list) else 0)
    if not isinstance(events, list):
        return _fail("bundle_events_required", source_event_count=len(source_events))
    windows, error = _market_windows(document)
    if error:
        return _fail(error, event_count=len(events), source_event_count=len(source_events))
    depth_by_ref, depth_error = _depth_snapshots(document, windows)
    if depth_error:
        return _fail(depth_error, event_count=len(events), source_event_count=len(source_events))
    settlement_by_id, settlement_error = _settlement_receipts(document, windows)
    if settlement_error:
        return _fail(settlement_error, event_count=len(events), source_event_count=len(source_events))
    account_market_ids: set[str] = set()
    for row in events:
        if not isinstance(row, dict) or not isinstance(row.get("market_id"), str) or row.get("market_id") not in windows:
            return _fail("account_event_market_identity_mismatch", event_count=len(events), source_event_count=len(source_events))
        account_market_ids.add(row["market_id"])
    source_market_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    source_by_ref: dict[tuple[str, str, int], dict[str, Any]] = {}
    for raw in source_events:
        if not isinstance(raw, dict) or not isinstance(raw.get("market_id"), str) or raw.get("market_id") not in windows:
            return _fail("source_event_market_identity_mismatch", event_count=len(events), source_event_count=len(source_events))
        source_market_ids.add(raw["market_id"])
        try:
            record = normalize_polybolt_event(raw)
        except PolyBoltContractError:
            return _fail("source_contract_violation", event_count=len(events), source_event_count=len(source_events))
        window = windows[raw["market_id"]]
        if not (window["window_start_ms"] <= record["event_ms"] <= window["window_end_ms"]):
            return _fail("source_event_after_market_window", event_count=len(events), source_event_count=len(source_events))
        record["market_id"] = raw["market_id"]
        normalized.append(record)
        if record["kind"] == "update":
            source_by_ref[(record["market_id"], record["capture_id"], record["seq"])] = record
    if source_market_ids != account_market_ids:
        return _fail("source_account_market_coverage_mismatch", event_count=len(events), source_event_count=len(source_events))
    try:
        manifest = build_continuity_manifest(normalized)
    except PolyBoltContractError:
        return _fail("source_continuity_missing", event_count=len(events), source_event_count=len(source_events))
    if not manifest["continuity_ok"]:
        return {
            **_fail("source_continuity_not_admissible", event_count=len(events), source_event_count=len(source_events)),
            "source_continuity_manifest": manifest,
        }
    if strategy_profile == H600_STRATEGY_PROFILE:
        orderfilled_by_id, orderfilled_error = _orderfilled_records(document)
        if orderfilled_error:
            return {
                **_fail(orderfilled_error, event_count=len(events), source_event_count=len(source_events)),
                "source_continuity_manifest": manifest,
            }
        orderfilled_binding_error = _validate_orderfilled_bindings(document, events, orderfilled_by_id)
        if orderfilled_binding_error:
            return {
                **_fail(orderfilled_binding_error, event_count=len(events), source_event_count=len(source_events)),
                "source_continuity_manifest": manifest,
            }
    ledger_failure = _validate_same_ledger(
        document,
        events,
        windows,
        source_by_ref,
        depth_by_ref,
        settlement_by_id,
        required_fills=required_fills,
        require_order_identity=strategy_profile == H600_STRATEGY_PROFILE,
    )
    if ledger_failure:
        return {
            **_fail(ledger_failure, event_count=len(events), source_event_count=len(source_events)),
            "source_continuity_manifest": manifest,
        }
    try:
        _, _, strategy_config = _strategy_profile(
            strategy_profile,
            private_strategy_config=private_strategy_config,
        )
    except ValueError as exc:
        return {
            **_fail(str(exc), event_count=len(events), source_event_count=len(source_events)),
            "source_continuity_manifest": manifest,
        }
    trace_failure = _validate_strategy_decision_trace(
        document,
        events,
        strategy_profile=strategy_profile,
        strategy_config=strategy_config,
        depth_by_ref=depth_by_ref,
    )
    if trace_failure:
        return {
            **_fail(trace_failure, event_count=len(events), source_event_count=len(source_events)),
            "source_continuity_manifest": manifest,
        }
    reconcile_events = []
    for row in events:
        converted = dict(row)
        converted["entry_ts"] = row["entry_ts_ms"]
        converted["cash_available_ts"] = row["cash_available_ts_ms"]
        reconcile_events.append(converted)
    result = reconcile_authenticated_chain(
        reconcile_events,
        owner=owner,
        # The market-window check above enforces the documented share floor,
        # and _validate_same_ledger enforces the frozen H30 strategy notional
        # floor before this compatibility reconciler runs.  Keep this tiny
        # ledger floor as a final zero-cost arithmetic guard; it is not a
        # second venue-unit interpretation.
        minimum_order=str(MINIMUM_ENTRY_COST_FOR_LEDGER),
        required_fills=REQUIRED_FILLS,
        starting_checkpoint_id=document.get("starting_checkpoint_id"),
        closing_balance_checkpoint_id=document.get("closing_balance_checkpoint_id"),
        closing_balance=document.get("closing_balance"),
    )
    # Keep the source manifest compact and omit owner/account identifiers.
    return {
        "schema": SCHEMA,
        "required_fills": int(required_fills),
        "event_count": len(events),
        "source_event_count": len(source_events),
        "source_update_count": sum(record.get("kind") == "update" for record in normalized),
        "source_continuity_ok": True,
        "source_reconnect_segments": manifest["reconnect_segments"],
        "market_window_count": len(windows),
        "depth_snapshot_count": len(depth_by_ref),
        "settlement_receipt_count": len(settlement_by_id),
        "fills": int(result.get("fills", 0)),
        "consecutive": bool(result.get("consecutive", False)),
        "paper_replay_passed": bool(result.get("paper_replay_passed", False)),
        # Legacy reconciler flags describe consistency of caller-supplied rows.
        # Keep that diagnostic separate from independently authenticated fills.
        **OFFLINE_VERIFICATION,
        "offline_consistency_passed": result.get("gate_passed") is True,
        "failure_reason": result.get("failure_reason"),
        "ending_cash": result.get("ending_cash"),
        "net_pnl": result.get("net_pnl"),
        "capital_floor_diagnostic": result.get(
            "capital_floor_diagnostic",
            {
                "protocol": "h643_capital_buffer_loss_ladder_v1",
                "same_evolving_ledger": True,
                "status": "not_evaluated",
            },
        ),
        "owner_scoped": bool(result.get("owner_scoped", False)),
        "identity_complete": bool(result.get("identity_complete", False)),
        "same_ledger_identity_verified": True,
        "cash_amounts_reconciled": True,
        "source_fill_references_verified": True,
        "strategy_decision_trace_verified": strategy_profile in TRACE_PROFILES,
        "depth_evidence_verified": True,
        "settlement_evidence_verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--required-fills", type=int, default=10)
    parser.add_argument(
        "--strategy-profile",
        choices=(DEFAULT_STRATEGY_PROFILE, H530_STRATEGY_PROFILE, H534_STRATEGY_PROFILE, H544_STRATEGY_PROFILE, H546_STRATEGY_PROFILE, H549_STRATEGY_PROFILE, H561_STRATEGY_PROFILE, H578_STRATEGY_PROFILE, H600_STRATEGY_PROFILE, PRIVATE_STRATEGY_PROFILE),
        default=DEFAULT_STRATEGY_PROFILE,
    )
    parser.add_argument("--private-strategy-config", type=Path)
    args = parser.parse_args()
    try:
        document = json.loads(args.bundle.read_text(encoding="utf-8"))
        private_config = None
        if args.private_strategy_config is not None:
            private_config = json.loads(args.private_strategy_config.read_text(encoding="utf-8"))
        result = validate_bundle_document(
            document,
            required_fills=args.required_fills,
            strategy_profile=args.strategy_profile,
            private_strategy_config=private_config,
        )
    except (OSError, json.JSONDecodeError):
        result = _fail("bundle_read_error")
    except ValueError as exc:
        reason = str(exc)
        result = _fail(
            reason if reason in {"private_strategy_profile_unavailable", "private_strategy_profile_invalid"} else "bundle_read_error"
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # Exit success describes the offline check, never actual canary admission.
    return 0 if result.get("offline_consistency_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

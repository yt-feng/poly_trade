"""Compose H493 maker-pair rows with H496 official V2 path evidence."""
from __future__ import annotations

from typing import Any, Mapping

try:
    from .authenticated_maker_pair_bundle_adapter import assemble_authenticated_maker_pair_event
    from .validate_v2_settlement_path import validate_path_event
except ImportError:  # pragma: no cover - direct script execution
    from authenticated_maker_pair_bundle_adapter import assemble_authenticated_maker_pair_event
    from validate_v2_settlement_path import validate_path_event


def assemble_authenticated_v2_maker_pair_event(
    *,
    up_order_event: Mapping[str, Any],
    up_trade_event: Mapping[str, Any],
    down_order_event: Mapping[str, Any],
    down_trade_event: Mapping[str, Any],
    pair_fields: Mapping[str, Any],
    settlement_fields: Mapping[str, Any],
    path_fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one H493 pair and fail closed unless H496 path evidence is valid."""
    if not isinstance(path_fields, Mapping):
        raise ValueError("path_fields_must_be_object")
    row = assemble_authenticated_maker_pair_event(
        up_order_event=up_order_event,
        up_trade_event=up_trade_event,
        down_order_event=down_order_event,
        down_trade_event=down_trade_event,
        pair_fields=pair_fields,
        settlement_fields=settlement_fields,
    )
    merged = dict(row)
    for key, value in path_fields.items():
        if key in merged and merged[key] != value:
            raise ValueError(f"path_field_conflicts_with_pair:{key}")
        merged[key] = value
    result = validate_path_event(
        merged,
        owner=str(merged["owner"]),
        ledger_id=str(merged["ledger_id"]),
        chain_id=str(merged["chain_id"]),
    )
    if result is None or "failure_reason" in result:
        raise ValueError(f"v2_path_{(result or {}).get('failure_reason', 'invalid')}")
    merged["v2_path_contract_validated_offline"] = True
    return merged


__all__ = ["assemble_authenticated_v2_maker_pair_event"]

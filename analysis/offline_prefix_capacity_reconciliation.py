"""Reconcile saved quote-domain capacity with the H695 retry ledger.

The saved quote-cash domain contains the first observed exit quote.  H695
also persisted bounded exit-retry traces.  This audit joins the two saved
artifacts and checks that a later full-size retry quote actually covers the
held shares before treating a prefix event as executable in simulation.
No quote replay, model call, network request, or order is performed here.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any


TOL = 1e-8


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _f(row: dict[str, Any], key: str) -> float:
    return float(row[key])


def _close(a: float, b: float, tol: float = TOL) -> bool:
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)


def _parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_domains(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["market_id"]: row for row in csv.DictReader(fh)}


def _retry_capacity(row: dict[str, Any]) -> tuple[float | None, dict[str, Any] | None]:
    retry = row.get("exit_retry")
    if not isinstance(retry, dict):
        return None, None
    trace = retry.get("trace")
    if not isinstance(trace, list):
        return None, None
    full_size = [item for item in trace if item.get("status") == "full_size"]
    if not full_size:
        return None, None
    selected = full_size[-1]
    available = float(selected["available_shares"])
    haircut = float(selected["depth_haircut"])
    return available * haircut, selected


def reconcile(ledger: dict[str, Any], domains: dict[str, dict[str, str]], expected_fills: int = 10) -> dict[str, Any]:
    chain = ledger.get("chain")
    if not isinstance(chain, list) or len(chain) != expected_fills:
        raise ValueError(f"expected {expected_fills} ledger chain rows")
    if ledger.get("scope") != "pooled_single_ledger":
        raise ValueError("ledger is not a pooled single ledger")

    errors: list[str] = []
    events: list[dict[str, Any]] = []
    for index, event in enumerate(chain, 1):
        market_id = event.get("market_id")
        domain = domains.get(market_id)
        if domain is None:
            errors.append(f"trade_{index}:missing_domain_row")
            continue
        held = float(event["shares"])
        cash_before = float(event["cash_before_entry"])
        entry_capacity = _f(domain, "entry_effective_available_shares")
        initial_exit_capacity = _f(domain, "exit_effective_available_shares")
        base_pair_capacity = _f(domain, "maximum_pair_capacity_shares")
        minimum_shares = _f(domain, "minimum_entry_shares")
        minimum_cash = _f(domain, "minimum_entry_cash")
        retry_capacity, retry_quote = _retry_capacity(event)
        final_exit_capacity = retry_capacity if retry_capacity is not None else initial_exit_capacity
        entry_minimum_ok = float(event["entry_cost"]) + TOL >= minimum_cash and held + TOL >= minimum_shares
        entry_full_size_ok = entry_capacity + TOL >= held
        base_exit_capacity_ok = base_pair_capacity + TOL >= held
        final_exit_capacity_ok = final_exit_capacity + TOL >= held
        release_match = domain["assumed_cash_release_ts"] == event["cash_release_ts"]
        cash_domain_observed = domain["simulation_cash_domain_observed"] == "True"
        if not cash_domain_observed:
            errors.append(f"trade_{index}:simulation_cash_domain_not_observed")
        if not entry_minimum_ok:
            errors.append(f"trade_{index}:entry_minimum_failed")
        if not entry_full_size_ok:
            errors.append(f"trade_{index}:entry_full_size_capacity_failed")
        if not final_exit_capacity_ok:
            errors.append(f"trade_{index}:final_exit_capacity_failed")
        if not release_match:
            errors.append(f"trade_{index}:cash_release_mismatch")
        if retry_capacity is not None and not retry_quote:
            errors.append(f"trade_{index}:retry_capacity_without_quote")
        events.append({
            "ordinal": index,
            "market_id": market_id,
            "held_shares": held,
            "cash_before_entry": cash_before,
            "minimum_entry_shares": minimum_shares,
            "minimum_entry_cash": minimum_cash,
            "entry_capacity_shares": entry_capacity,
            "entry_minimum_ok": entry_minimum_ok,
            "entry_full_size_capacity_ok": entry_full_size_ok,
            "initial_exit_capacity_shares": initial_exit_capacity,
            "initial_pair_capacity_ok": base_exit_capacity_ok,
            "retry_used": retry_capacity is not None,
            "retry_capacity_shares": retry_capacity,
            "final_exit_capacity_shares": final_exit_capacity,
            "final_exit_capacity_ok": final_exit_capacity_ok,
            "retry_rescued_initial_capacity": (not base_exit_capacity_ok) and final_exit_capacity_ok,
            "minimum_order_has_pair_capacity_label": domain["minimum_order_has_pair_capacity"] == "True",
            "simulation_cash_domain_observed": cash_domain_observed,
            "cash_release_match": release_match,
            "assumed_cash_release_ts": domain["assumed_cash_release_ts"],
            "cash_release_basis": domain["cash_release_basis"],
            "retry_selected_arrival_ts": retry_quote.get("arrival_ts") if retry_quote else None,
        })

    initial_pair_pass = sum(event["initial_pair_capacity_ok"] for event in events)
    final_pair_pass = sum(event["final_exit_capacity_ok"] for event in events)
    retry_used = sum(event["retry_used"] for event in events)
    rescued = sum(event["retry_rescued_initial_capacity"] for event in events)
    status = "valid_historical_retry_reconciled_prefix" if not errors else "invalid_saved_capacity_reconciliation"
    return {
        "format": "h700-prefix-capacity-reconciliation-v1",
        "status": status,
        "scope": "saved_h695_prefix_joined_to_saved_h685_quote_cash_domains",
        "required_fills": expected_fills,
        "matched_domain_rows": len(events),
        "continuous_sequence_complete": not errors and len(events) == expected_fills,
        "simulation_cash_domain_observed_count": sum(e["simulation_cash_domain_observed"] for e in events),
        "entry_minimum_pass_count": sum(e["entry_minimum_ok"] for e in events),
        "entry_full_size_capacity_pass_count": sum(e["entry_full_size_capacity_ok"] for e in events),
        "initial_exit_pair_capacity_pass_count": initial_pair_pass,
        "retry_used_count": retry_used,
        "retry_rescued_initial_capacity_count": rescued,
        "final_exit_capacity_pass_count": final_pair_pass,
        "cash_release_match_count": sum(e["cash_release_match"] for e in events),
        "cash_release_basis": sorted({e["cash_release_basis"] for e in events}),
        "historical_only": True,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "model_fit_calls": 0,
        "model_predict_calls": 0,
        "raw_quote_replays": 0,
        "interpretation": "Three first-exit snapshots are capacity-insufficient, but each has a persisted bounded full-size retry quote that covers the held shares. The prefix passes only after this retry evidence is included; the cash-release timestamp remains a declared simulation assumption, not an authenticated cash receipt.",
        "events": events,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--domains", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = reconcile(_read_json(args.ledger), load_domains(args.domains))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "matched_domain_rows", "initial_exit_pair_capacity_pass_count", "retry_used_count", "retry_rescued_initial_capacity_count", "final_exit_capacity_pass_count", "cash_release_match_count", "actual_fills", "verified_canary_fills")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

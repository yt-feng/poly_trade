"""Audit an already saved baseline prefix as one continuous cash ledger.

This is deliberately model free: it reads the H695 ledger and report, checks
the ten chronological transitions, and emits only aggregate evidence.  It
does not replay quotes, fit/predict, or enable orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any


TOL = 1e-8


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _close(a: float, b: float, tol: float = TOL) -> bool:
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def audit(ledger: dict[str, Any], report: dict[str, Any], expected_fills: int = 10) -> dict[str, Any]:
    chain = ledger.get("chain")
    if not isinstance(chain, list):
        raise ValueError("baseline ledger has no chain")
    if len(chain) != expected_fills:
        raise ValueError(f"expected {expected_fills} chain rows, found {len(chain)}")
    if ledger.get("scope") != "pooled_single_ledger":
        raise ValueError("ledger is not pooled_single_ledger")
    if ledger.get("live_orders_enabled") is not False:
        raise ValueError("live order flag is not false")

    errors: list[str] = []
    balances: list[float] = []
    pnl: list[float] = []
    timestamps: list[dt.datetime] = []
    for index, row in enumerate(chain, 1):
        try:
            timestamps.append(_ts(row["decision_ts"]))
            cash_before = float(row["cash_before_entry"])
            cash_after = float(row["cash_after_entry"])
            entry_cost = float(row["entry_cost"])
            payout = float(row["payout"])
            gross = float(row["exit_gross_proceeds"])
            exit_fee = float(row["exit_fee"])
            realized = float(row["realized_pnl"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"trade_{index}:missing_or_invalid_field:{exc}")
            continue
        if index == 1:
            if not _close(cash_before, 10.0):
                errors.append(f"trade_{index}:initial_cash={cash_before}")
        else:
            previous = float(chain[index - 2].get("payout", float("nan")))
            if not _close(cash_before, previous):
                errors.append(f"trade_{index}:cash_before_not_previous_payout")
        if entry_cost <= 0 or entry_cost > cash_before + TOL:
            errors.append(f"trade_{index}:entry_cost_outside_cash")
        if abs(cash_after) > TOL:
            errors.append(f"trade_{index}:cash_not_fully_deployed")
        if not _close(payout, gross - exit_fee):
            errors.append(f"trade_{index}:payout_fee_identity_failed")
        if not _close(realized, payout - cash_before):
            errors.append(f"trade_{index}:realized_pnl_identity_failed")
        if row.get("status") != "timed_quote_exit":
            errors.append(f"trade_{index}:unexpected_status={row.get('status')}")
        balances.append(payout)
        pnl.append(realized)

    for left, right in zip(timestamps, timestamps[1:]):
        if right <= left:
            errors.append("decision_timestamps_not_strictly_increasing")
            break

    start_cash = float(chain[0]["cash_before_entry"])
    end_cash = float(chain[-1]["payout"])
    running_peak = start_cash
    max_drawdown = 0.0
    for balance in [start_cash, *balances]:
        running_peak = max(running_peak, balance)
        max_drawdown = max(max_drawdown, running_peak - balance)
    positive = sum(value > TOL for value in pnl)
    negative = sum(value < -TOL for value in pnl)
    report_econ = report.get("economic_readout", {}).get("baseline_C_scheduled_retry_fullstream", {})
    source_payload = json.dumps(chain, sort_keys=True, separators=(",", ":")).encode()
    return {
        "format": "h699-baseline-prefix-audit-v1",
        "status": "valid_historical_simulated_continuous_prefix" if not errors else "invalid_prefix_invariants",
        "scope": ledger.get("scope"),
        "execution_class": ledger.get("execution_class"),
        "initial_cash": start_cash,
        "ending_cash": end_cash,
        "net_cash_change": end_cash - start_cash,
        "compounded_return": end_cash / start_cash - 1.0,
        "required_fills": expected_fills,
        "resolved_fills": len(chain),
        "continuous_sequence_complete": not errors,
        "positive_trades": positive,
        "negative_trades": negative,
        "max_drawdown": max_drawdown,
        "max_drawdown_fraction_of_peak": max_drawdown / max(balances + [start_cash]),
        "minimum_cash_after_resolution": min(balances + [start_cash]),
        "maximum_cash_after_resolution": max(balances + [start_cash]),
        "audit_chain_fingerprint": hashlib.sha256(source_payload).hexdigest(),
        "reported_prefix_fingerprint": report_econ.get("prefix_fingerprint"),
        "full_stream_resolved": report_econ.get("full_stream_resolved"),
        "full_stream_continuation_failure": report_econ.get("continuation_failure"),
        "historical_only": True,
        "actual_fills": int(report.get("protocol", {}).get("actual_fills", 0)),
        "verified_canary_fills": int(report.get("protocol", {}).get("verified_canary_fills", 0)),
        "live_orders_enabled": False,
        "model_fit_calls": 0,
        "model_predict_calls": 0,
        "raw_quote_replays": 0,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(_read(args.ledger), _read(args.report))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "initial_cash", "ending_cash", "net_cash_change", "resolved_fills", "continuous_sequence_complete", "full_stream_continuation_failure", "actual_fills", "verified_canary_fills")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

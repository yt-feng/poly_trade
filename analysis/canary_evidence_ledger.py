"""Fail-closed ledger for evidence on the path to a ten-trade canary.

This module consumes caller-supplied JSON/JSONL records only. It has no network,
wallet, signing, order, or account access. Public quotes, paper simulations, and
synthetic receipts are retained as diagnostics but can never count as real-fill
evidence. A private execution receipt is qualifying only when its provenance and
all required reconciliation fields are explicit.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Iterable


REQUIREMENTS = {
    "ten_canary_roundtrips": 10,
    "independent_windows": 300,
    "independent_utc_dates": 7,
    "execution_evidence": 100,
    "exit_reconciliation_rate": Decimal("0.99"),
}
PRE_CANARY_GATES = (
    "independent_windows",
    "independent_utc_dates",
    "execution_evidence",
    "cost_adjusted_pnl_lower_bound_positive",
    "three_second_stress_lower_bound_positive",
    "extra_exit_tick_stress_lower_bound_positive",
    "exit_reconciliation_rate_to_99pct",
)
POST_CANARY_GATES = ("ten_canary_roundtrips",) + PRE_CANARY_GATES
PHASES = {"pre_canary_research", "post_canary_completion"}
REAL_PROVENANCE = "private_execution_receipt"
NONQUALIFYING_PROVENANCE = {"public_quote", "paper_simulation", "synthetic_receipt"}


def decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def utc_date(value: Any) -> str:
    text = str(value or "")
    if not text:
        raise ValueError("entry_utc is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("entry_utc must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("entry_utc must include a timezone")
    return parsed.astimezone(timezone.utc).date().isoformat()


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load one JSON object/list, JSONL file, or a directory of those files."""
    paths = sorted(path.glob("*.jsonl")) + sorted(path.glob("*.json")) if path.is_dir() else [path]
    records: list[dict[str, Any]] = []
    for source in paths:
        if source.suffix == ".jsonl":
            values = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            value = json.loads(source.read_text(encoding="utf-8"))
            values = value.get("records", []) if isinstance(value, dict) else value
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError(f"{source} must contain a list of record objects")
        records.extend(values)
    return records


def _receipt_qualification(record: dict[str, Any]) -> tuple[bool, list[str], str]:
    """Return (qualifies, reasons, provenance) without exposing receipt fields."""
    provenance = str(record.get("provenance") or "")
    reasons: list[str] = []
    if provenance != REAL_PROVENANCE:
        reasons.append("non_private_provenance")
    if provenance in NONQUALIFYING_PROVENANCE:
        reasons.append(f"excluded_{provenance}")
    if record.get("synthetic") is True or record.get("is_synthetic") is True:
        reasons.append("synthetic_marker")
    if str(record.get("entry_status") or "").upper() != "CONFIRMED":
        reasons.append("entry_not_confirmed")
    for field in ("evidence_id", "entry_trade_id", "entry_transaction_ref", "window_id"):
        if not str(record.get(field) or ""):
            reasons.append(f"missing_{field}")
    try:
        record_date = utc_date(record.get("entry_utc"))
        if record.get("utc_date") not in (None, record_date):
            reasons.append("utc_date_mismatch")
    except ValueError as exc:
        reasons.append(str(exc))
    for field in ("gross_pnl_usd", "fees_usd", "lower_bound_pnl_usd"):
        try:
            decimal(record.get(field), field)
        except ValueError as exc:
            reasons.append(str(exc))
    try:
        gross = decimal(record.get("gross_pnl_usd"), "gross_pnl_usd")
        fees = decimal(record.get("fees_usd"), "fees_usd")
        lower = decimal(record.get("lower_bound_pnl_usd"), "lower_bound_pnl_usd")
        if fees < 0 or lower > gross - fees:
            reasons.append("lower_bound_not_cost_adjusted")
    except ValueError:
        pass
    return not reasons, reasons, provenance


def _exit_reconciled(record: dict[str, Any]) -> bool:
    required_true = (
        "exit_reconciled",
        "order_acknowledged",
        "cancel_reconciled",
        "fee_reconciled",
        "settlement_reconciled",
        "account_reconciled",
    )
    if any(record.get(field) is not True for field in required_true):
        return False
    if str(record.get("exit_status") or "").upper() != "CONFIRMED":
        return False
    return bool(record.get("exit_trade_id")) and bool(record.get("exit_transaction_ref"))


def evaluate(records: Iterable[dict[str, Any]], phase: str = "pre_canary_research") -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"phase must be one of: {', '.join(sorted(PHASES))}")
    records = list(records)
    duplicate_ids = [key for key, count in Counter(str(r.get("evidence_id") or "") for r in records).items()
                     if key and count > 1]
    qualifying: list[dict[str, Any]] = []
    excluded_reasons: Counter[str] = Counter()
    invalid_private = 0
    for record in records:
        ok, reasons, provenance = _receipt_qualification(record)
        if ok and provenance == REAL_PROVENANCE:
            qualifying.append(record)
        else:
            if provenance == REAL_PROVENANCE:
                invalid_private += 1
            excluded_reasons.update(reasons or ["not_qualifying"])

    real_fill_ids = {str(r["entry_trade_id"]) for r in qualifying}
    roundtrips = [r for r in qualifying if _exit_reconciled(r)]
    windows = {str(r["window_id"]) for r in qualifying}
    dates = {utc_date(r["entry_utc"]) for r in qualifying}
    lower_bound = sum((decimal(r["lower_bound_pnl_usd"], "lower_bound_pnl_usd") for r in qualifying), Decimal("0"))
    stress_3s_missing = sum("stress_3s_lower_bound_usd" not in r for r in qualifying)
    stress_tick_missing = sum("stress_extra_exit_tick_lower_bound_usd" not in r for r in qualifying)
    stress_3s = sum((decimal(r["stress_3s_lower_bound_usd"], "stress_3s_lower_bound_usd")
                     for r in qualifying if "stress_3s_lower_bound_usd" in r), Decimal("0"))
    stress_tick = sum((decimal(r["stress_extra_exit_tick_lower_bound_usd"],
                               "stress_extra_exit_tick_lower_bound_usd")
                       for r in qualifying if "stress_extra_exit_tick_lower_bound_usd" in r), Decimal("0"))
    exit_rate = Decimal(len(roundtrips)) / Decimal(len(qualifying)) if qualifying else Decimal("0")
    complete_count = len(roundtrips)

    observed = {
        "input_records": len(records),
        "public_quote_records": sum(r.get("provenance") == "public_quote" for r in records),
        "paper_simulation_records": sum(r.get("provenance") == "paper_simulation" for r in records),
        "synthetic_receipt_records": sum(r.get("provenance") == "synthetic_receipt" or r.get("synthetic") is True for r in records),
        "qualifying_private_receipt_records": len(qualifying),
        "real_confirmed_fill_count": len(real_fill_ids),
        "complete_roundtrips": complete_count,
        "independent_windows": len(windows),
        "independent_utc_dates": len(dates),
        "cost_adjusted_pnl_lower_bound_usd": str(lower_bound),
        "three_second_stress_lower_bound_usd": str(stress_3s),
        "extra_exit_tick_stress_lower_bound_usd": str(stress_tick),
        "three_second_stress_missing_records": stress_3s_missing,
        "extra_exit_tick_stress_missing_records": stress_tick_missing,
        "exit_reconciled_records": len(roundtrips),
        "exit_reconciliation_rate": str(exit_rate),
        "invalid_private_receipts": invalid_private,
        "duplicate_evidence_ids": len(duplicate_ids),
    }
    missing = {
        "ten_canary_roundtrips": max(0, REQUIREMENTS["ten_canary_roundtrips"] - complete_count),
        "independent_windows": max(0, REQUIREMENTS["independent_windows"] - len(windows)),
        "independent_utc_dates": max(0, REQUIREMENTS["independent_utc_dates"] - len(dates)),
        "execution_evidence": max(0, REQUIREMENTS["execution_evidence"] - len(qualifying)),
        "cost_adjusted_pnl_lower_bound_positive": 0 if lower_bound > 0 else 1,
        "three_second_stress_lower_bound_positive": 0 if stress_3s_missing == 0 and stress_3s > 0 else 1,
        "extra_exit_tick_stress_lower_bound_positive": 0 if stress_tick_missing == 0 and stress_tick > 0 else 1,
        "exit_reconciliation_rate_to_99pct": 0 if qualifying and exit_rate >= REQUIREMENTS["exit_reconciliation_rate"] else 1,
    }
    pre_missing = {key: missing[key] for key in PRE_CANARY_GATES if missing[key]}
    completion_missing = {key: missing[key] for key in POST_CANARY_GATES if missing[key]}
    phase_gate_names = PRE_CANARY_GATES if phase == "pre_canary_research" else POST_CANARY_GATES
    blockers = [key for key in phase_gate_names if missing[key]]
    common_blockers = []
    common_blockers.extend(["public_or_synthetic_evidence_only"] if not qualifying and records else [])
    common_blockers.extend(["duplicate_evidence_ids"] if duplicate_ids else [])
    blockers.extend(common_blockers)
    pre_blockers = list(pre_missing) + common_blockers
    completion_blockers = list(completion_missing) + common_blockers
    return {
        "schema_version": 1,
        "phase": phase,
        "status": "blocked" if blockers else "eligible_for_human_review",
        "eligible_for_human_review": not blockers,
        "pre_canary_research": {
            "eligible": not pre_blockers,
            "missing_counts": pre_missing,
            "blockers": pre_blockers,
            "note": "This phase does not require ten completed canary round-trips; it is a prerequisite review gate for a first canary.",
        },
        "post_canary_completion": {
            "complete": not completion_blockers,
            "missing_counts": completion_missing,
            "blockers": completion_blockers,
            "note": "Ten completed canary round-trips are measured after a canary run and are never used as a prerequisite for starting the first canary.",
        },
        "promotion_allowed": False,
        "evidence_boundary": "Only private_execution_receipt records can qualify; public quotes, paper simulations, and synthetic fixtures never count.",
        "requirements": {
            "ten_canary_roundtrips": REQUIREMENTS["ten_canary_roundtrips"],
            "independent_windows": REQUIREMENTS["independent_windows"],
            "independent_utc_dates": REQUIREMENTS["independent_utc_dates"],
            "execution_evidence": REQUIREMENTS["execution_evidence"],
            "exit_reconciliation_rate": str(REQUIREMENTS["exit_reconciliation_rate"]),
            "cost_adjusted_lower_bound_positive": True,
            "three_second_stress_lower_bound_positive": True,
            "extra_exit_tick_stress_lower_bound_positive": True,
        },
        "observed": observed,
        "missing_counts": missing,
        "excluded_record_reasons": dict(sorted(excluded_reasons.items())),
        "blockers": blockers,
    }


def write_report(input_path: Path, output_path: Path, phase: str = "pre_canary_research") -> dict[str, Any]:
    report = evaluate(load_records(input_path), phase=phase)
    report["input"] = str(input_path)
    report["output_is_diagnostic_only"] = True
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=sorted(PHASES), default="pre_canary_research")
    args = parser.parse_args()
    report = write_report(args.input, args.output, phase=args.phase)
    print(json.dumps({"phase": report["phase"], "status": report["status"], "promotion_allowed": report["promotion_allowed"],
                      "observed": report["observed"], "missing_counts": report["missing_counts"],
                      "blockers": report["blockers"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

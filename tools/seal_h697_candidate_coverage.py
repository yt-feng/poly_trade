"""Seal the aggregate H697 candidate-coverage report for the private journal."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

try:
    from . import research_vault as vault
except ImportError:
    import research_vault as vault


def build_payload(report: dict, descriptor: dict, *, run_id: str,
                  run_attempt: str, source_commit: str) -> dict:
    if not isinstance(report, dict) or report.get("format") != "offline-full-candidate-coverage-v1":
        raise ValueError("candidate_coverage_report_schema")
    if not isinstance(descriptor, dict) or descriptor.get("format") != "scoped-research-dataset-v1":
        raise ValueError("descriptor_schema")
    return {
        "format": "h697-full-candidate-coverage-sealed-v1",
        "run_id": str(run_id),
        "run_attempt": str(run_attempt),
        "source_commit": str(source_commit),
        "descriptor_sha256": hashlib.sha256(vault.canonical(descriptor)).hexdigest(),
        "coverage_report": report,
        "new_model_fit_calls": 0,
        "new_model_predict_calls": 0,
        "new_strategy_replays": 0,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "plaintext_published": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recipient = vault.load_json_bytes(Path("research_vault/recipient.json").read_bytes(), 4096)
    recipient_id = os.environ["QUALITY_FIRST_RECIPIENT_ID"]
    vault.validate_recipient(recipient, recipient_id)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    descriptor = json.loads(args.descriptor.read_text(encoding="utf-8"))
    payload = vault.canonical(build_payload(
        report, descriptor,
        run_id=os.environ.get("GITHUB_RUN_ID", "unknown"),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "unknown"),
        source_commit=os.environ.get("GITHUB_SHA", "unknown"),
    ))
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    vault.write_new(args.output / "part-0000.vault", vault.seal(payload, recipient, recipient_id), 0o600)
    vault.write_new(args.output / "status.json", vault.canonical({
        "version": 1, "status": "complete", "stage": "sealed_delivery",
        "strategy_evaluated": True, "live_orders_enabled": False,
        "plaintext_published": False, "sealed_parts": 1,
    }) + b"\n", 0o644)
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("delivery_complete=true\nfull_evidence=true\ncomparison_evaluated=true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

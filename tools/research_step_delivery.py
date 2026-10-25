"""Run one research child privately and seal success or failure evidence.

This wrapper does not import a strategy, acquire data, retry a command or publish
to GitHub. The workflow preserves the child's failure while subsequent always
steps retain its ciphertext. Recipient trust is pinned independently of the
public recipient document.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import traceback

try:
    from . import research_vault as vault
    from . import seal_research_outcomes as delivery
except ImportError:
    import research_vault as vault
    import seal_research_outcomes as delivery


TRUSTED_RECIPIENT_ID = "5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9"
MAX_TIMEOUT_SECONDS = 9900
MAX_SUMMARY_BYTES = 10 * 1024 * 1024
MAX_PROJECTION_DIAGNOSTICS_BYTES = 8 * 1024 * 1024
PROFILES = {
    "h711": ("old-clean-h711-causal-h600-favorite-side-depth-repair-v1",
             "H711_CAUSAL_H600_FAVORITE_SIDE_DEPTH_REPAIR", ("selection", "holdout")),
    "h712": ("old-clean-h712-causal-h549-adaptive-cash-boost-repair-v1",
             "H712_CAUSAL_H549_ADAPTIVE_CASH_BOOST_REPAIR", ("adaptive", "baseline", "comparison")),
}


def trusted_recipient(path: Path) -> dict:
    recipient = vault.load_json_bytes(vault.read_bounded(path, 4096), 4096)
    vault.validate_recipient(recipient, TRUSTED_RECIPIENT_ID)
    return recipient


def _private_directory(private: Path) -> None:
    if private.is_symlink():
        raise ValueError("PRIVATE_DIRECTORY_REQUIRED")
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    private.chmod(0o700)
    results = private / "results"
    if results.is_symlink():
        raise ValueError("PRIVATE_RESULTS_DIRECTORY_REQUIRED")
    results.mkdir(mode=0o700, exist_ok=True)


def execute(private: Path, command: list[str], *, timeout_seconds: int, recipient_path: Path) -> int:
    """Execute once with no public child output, preserving its primary exit."""
    if not command or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError("RESEARCH_EXECUTION_CONTRACT")
    trusted_recipient(recipient_path)
    _private_directory(private)
    receipt_path = private / "results" / "execution.json"
    # Exclusive log creation also prevents accidentally executing this unit twice.
    descriptor = os.open(private / "analysis-output.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    receipt = {"format": "private-research-step-execution-v1", "command": command,
               "timeout_seconds": timeout_seconds, "process_started": False,
               "timed_out": False, "exit_code": None, "process_exit_code": None}
    diagnostic = ""
    with os.fdopen(descriptor, "wb") as log:
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            receipt["process_started"] = True
            try:
                child.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                receipt["timed_out"] = True
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=5)
            receipt["process_exit_code"] = child.returncode
            receipt["exit_code"] = (124 if receipt["timed_out"] else
                                    child.returncode if child.returncode >= 0 else 128 - child.returncode)
        except Exception:
            diagnostic = traceback.format_exc()
            receipt["exit_code"] = 2
    vault.write_new(receipt_path, vault.canonical(receipt), 0o600)
    vault.write_new(private / "diagnostics.txt", diagnostic.encode("utf-8"), 0o600)
    return receipt["exit_code"]


def _read_object(path: Path, limit: int) -> dict:
    return vault.load_json_bytes(vault.read_bounded(path, limit), limit)


def _contract(profile: str | None, path: Path | None, source_commit: str) -> dict:
    if (profile is None) == (path is None):
        raise ValueError("REPORT_CONTRACT_REQUIRED")
    if path is not None:
        document = _read_object(path, 1024 * 1024)
        value = document.get("delivery_contract", document)
        if not isinstance(value, dict) or value.get("source_commit") != source_commit:
            raise ValueError("REPORT_CONTRACT_SOURCE")
        value = {key: value.get(key) for key in ("expected_format", "expected_hypothesis", "source_commit",
                                                "required_report_keys", "required_object_keys")}
        value["required_object_keys"] = value["required_object_keys"] or []
    else:
        format_name, hypothesis, fields = PROFILES[profile]
        value = {"expected_format": format_name, "expected_hypothesis": hypothesis,
                 "source_commit": source_commit, "required_report_keys": list(fields),
                 "required_object_keys": list(fields)}
    if not all(isinstance(value.get(key), str) and 0 < len(value[key]) <= 256
               for key in ("expected_format", "expected_hypothesis")):
        raise ValueError("REPORT_CONTRACT_IDENTIFIERS")
    for name in ("required_report_keys", "required_object_keys"):
        if (not isinstance(value[name], list) or len(value[name]) > 64
                or not all(isinstance(key, str) and 0 < len(key) <= 128 for key in value[name])):
            raise ValueError("REPORT_CONTRACT_KEYS")
    return value


def report_contract(report: dict, contract: dict) -> bool:
    source_commit = contract["source_commit"]
    return bool(
        report.get("format") == contract["expected_format"]
        and report.get("hypothesis_id") == contract["expected_hypothesis"]
        and report.get("source_commit") == source_commit
        and report.get("observed_source_commit") == source_commit
        and report.get("live_orders_enabled") is False
        and all(type(report.get(key)) is int and report[key] == 0 for key in ("actual_fills", "verified_canary_fills"))
        and type(report.get("feature_rows")) is int and report["feature_rows"] > 0
        and all(field in report and report[field] is not None for field in contract["required_report_keys"])
        and all(isinstance(report.get(field), dict) for field in contract["required_object_keys"])
    )


def _summary_projection(private: Path, output: Path, recipient: dict, metadata: dict,
                        report: dict | None, result: dict) -> None:
    diagnostics = {}
    omissions = []
    remaining = MAX_PROJECTION_DIAGNOSTICS_BYTES
    for name in ("analysis-output.txt", "diagnostics.txt"):
        try:
            raw = vault.read_bounded(private / name, remaining)
            remaining -= len(raw)
            diagnostics[name] = raw.decode("utf-8", errors="replace")
        except (OSError, ValueError):
            omissions.append({"member": name, "reason": "missing_or_exceeds_compact_budget"})
    payload = {"format": "private-research-step-summary-v1", "run": metadata,
               "report": report, "diagnostics": diagnostics, "summary_omissions": omissions,
               "full_evidence_retained": result["full_evidence_retained"],
               "full_diagnostics_retained": not omissions,
               "actual_fills": 0, "verified_canary_fills": 0, "live_orders_enabled": False}
    if len(vault.canonical(payload)) > vault.MAX_PLAINTEXT:
        payload["diagnostics"] = {}
        payload["summary_omissions"].append({"member": "diagnostics", "reason": "combined_envelope_budget"})
        payload["full_diagnostics_retained"] = False
    if len(vault.canonical(payload)) > vault.MAX_PLAINTEXT:
        payload["report"] = None
        payload["summary_omissions"].append({"member": "report", "reason": "combined_envelope_budget"})
    output.mkdir(mode=0o700)
    vault.write_new(output / "part-0000.vault", vault.seal(vault.canonical(payload), recipient, TRUSTED_RECIPIENT_ID), 0o600)
    status = {"version": 1, "status": "complete", "stage": "sealed_delivery",
              "strategy_evaluated": result["strategy_evaluated"], "live_orders_enabled": False,
              "plaintext_published": False, "sealed_parts": 1}
    vault.write_new(output / "status.json", vault.canonical(status) + b"\n", 0o644)


def seal(private: Path, output: Path, *, profile: str | None = None, source_commit: str,
         recipient_path: Path, env: dict, contract_path: Path | None = None,
         summary_output: Path | None = None) -> dict:
    """Seal failed/missing reports without falsely claiming evaluation success."""
    recipient = trusted_recipient(recipient_path)
    _private_directory(private)
    errors = []
    try:
        contract = _contract(profile, contract_path, source_commit)
    except (OSError, ValueError, TypeError):
        contract = None
        errors.append("report_contract_unavailable")
    try:
        execution = _read_object(private / "results" / "execution.json", 65536)
    except (OSError, ValueError, TypeError):
        execution = {}
        errors.append("execution_receipt_unavailable")
    try:
        report = _read_object(private / "results" / "summary.json", MAX_SUMMARY_BYTES)
        valid_report = contract is not None and report_contract(report, contract)
        if not valid_report:
            errors.append("report_contract_mismatch")
    except (OSError, ValueError, TypeError):
        report = None
        valid_report = False
        errors.append("report_missing_or_invalid")
    evaluated = bool(
        env.get("EVALUATION_OUTCOME") == "success"
        and execution.get("format") == "private-research-step-execution-v1"
        and execution.get("process_started") is True
        and execution.get("timed_out") is False
        and type(execution.get("exit_code")) is int and execution["exit_code"] == 0
        and type(execution.get("process_exit_code")) is int and execution["process_exit_code"] == 0
        and valid_report
    )
    metadata = {
        "format": "private-research-step-outcome-v1", "profile": profile,
        "source_commit": source_commit,
        "workflow": {key: env.get(key) for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA",
                                                   "EVALUATION_OUTCOME", "SETUP_OUTCOMES")},
        "execution": execution, "report_contract_valid": valid_report,
        "report_contract": contract,
        "contract_errors": errors, "strategy_evaluated": evaluated,
        "evaluation_status": "evaluated" if evaluated else "failed_or_not_executed",
        "actual_fills": 0, "verified_canary_fills": 0, "live_orders_enabled": False,
        "evidence_boundary": "delivery and execution validity do not establish economic qualification",
    }
    result = delivery.seal_outcomes(private, output, recipient, TRUSTED_RECIPIENT_ID, metadata=metadata)
    if summary_output is not None:
        _summary_projection(private, summary_output, recipient, metadata, report, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    run_parser = modes.add_parser("run")
    run_parser.add_argument("--private", required=True, type=Path)
    run_parser.add_argument("--recipient", default=Path("research_vault/recipient.json"), type=Path)
    run_parser.add_argument("--timeout-seconds", required=True, type=int)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    seal_parser = modes.add_parser("seal")
    seal_parser.add_argument("--private", required=True, type=Path)
    seal_parser.add_argument("--recipient", default=Path("research_vault/recipient.json"), type=Path)
    seal_parser.add_argument("--output", required=True, type=Path)
    contract_choice = seal_parser.add_mutually_exclusive_group(required=True)
    contract_choice.add_argument("--profile", choices=PROFILES)
    contract_choice.add_argument("--contract", type=Path)
    seal_parser.add_argument("--summary-output", type=Path)
    seal_parser.add_argument("--source-commit", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.mode == "run":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            code = execute(args.private, command, timeout_seconds=args.timeout_seconds, recipient_path=args.recipient)
            print(json.dumps({"status": "execution_complete" if code == 0 else "execution_failed", "plaintext_published": False}))
            return code
        result = seal(args.private, args.output, profile=args.profile, source_commit=args.source_commit,
                      recipient_path=args.recipient, env=dict(os.environ), contract_path=args.contract,
                      summary_output=args.summary_output)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
                handle.write("delivery_complete=true\n")
                handle.write("evaluation_complete=" + str(result["strategy_evaluated"]).lower() + "\n")
        print(json.dumps({"status": "delivery_complete", "plaintext_published": False}))
        return 0
    except Exception:
        # Wrapper errors never disclose child commands, source samples or paths.
        print(json.dumps({"status": "execution_contract_failed" if args.mode == "run" else "delivery_failed", "plaintext_published": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

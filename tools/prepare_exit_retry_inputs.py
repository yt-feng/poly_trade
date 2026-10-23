"""Restore bounded, precommitted private inputs without exposing their content."""
from __future__ import annotations

import argparse
import base64
import hashlib
import lzma
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import research_vault as vault


def decode_xz(value: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not 0 < len(value) <= 45000:
        raise ValueError("encoded input exceeds limit")
    packed = base64.b64decode(value, validate=True)
    if not 0 < len(packed) <= 49152:
        raise ValueError("packed input exceeds limit")
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=67108864)
    raw = decoder.decompress(packed, max_length=maximum + 1)
    if not 0 < len(raw) <= maximum or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid or excessive decompressed input")
    return raw


def verify_code_bindings(config: dict, root: Path, expected_commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise ValueError("invalid expected code commit")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if head != expected_commit or config.get("expected_code_commit") != head:
        raise ValueError("code commit mismatch")
    bindings = config.get("code_file_sha256")
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("code bindings required")
    for name, digest in bindings.items():
        relative = Path(name)
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or relative.is_absolute() or ".." in relative.parts):
            raise ValueError("invalid code binding")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("invalid code path")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("code bytes mismatch")
        subprocess.check_call(["git", "ls-files", "--error-unmatch", "--", name],
                              cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def materialize(config_encoded: str, candidate_encoded: str, audit_encoded: str,
                request_sha: str, expected_commit: str, private: Path,
                bootstrap: Path, root: Path = ROOT, *,
                evaluation_mode: str = "offline_exit_retry") -> dict:
    formats = {"offline_exit_retry": "exit-retry-private-audit-v1",
               "offline_exit_continuation": "exit-continuation-private-audit-v1"}
    if evaluation_mode not in formats:
        raise ValueError("unsupported offline comparison mode")
    if not re.fullmatch(r"[0-9a-f]{64}", request_sha):
        raise ValueError("invalid request digest")
    raw_config = decode_xz(config_encoded, 262144)
    if hashlib.sha256(raw_config).hexdigest() != request_sha:
        raise ValueError("configuration digest mismatch")
    config = vault.load_json_bytes(raw_config, 262144)
    if config.get("evaluation_mode") != evaluation_mode or config.get("live_orders_enabled") is not False:
        raise ValueError("offline configuration required")
    if config.get("source_repository") != "yt-feng/poly" or not re.fullmatch(r"[0-9a-f]{40}", config.get("source_commit", "")):
        raise ValueError("invalid immutable source")
    verify_code_bindings(config, root, expected_commit)
    # Import only after checking the committed consumer bytes. It cannot fit a model.
    sys.path.insert(0, str(root / "analysis"))
    from offline_exit_retry_audit import decode_candidate_packet
    candidates = decode_candidate_packet(candidate_encoded, config["candidate_binding"])
    audit = vault.load_json_bytes(decode_xz(audit_encoded, 524288), 524288)
    if set(audit) != {"format", "script_utf8", "registration"} or audit["format"] != formats[evaluation_mode]:
        raise ValueError("invalid independent audit envelope")
    if not isinstance(audit["script_utf8"], str):
        raise ValueError("invalid reader source")
    script = audit["script_utf8"].encode("utf-8")
    registration = vault.canonical(audit["registration"])
    if not 0 < len(script) <= 262144:
        raise ValueError("excessive reader source")
    reader_sha = hashlib.sha256(script).hexdigest()
    if (reader_sha != config["independent_audit"]["helper_sha256"]
            or hashlib.sha256(registration).hexdigest() != config["independent_audit"]["registration_canonical_sha256"]):
        raise ValueError("independent audit binding mismatch")
    scientific = {key: value for key, value in config.items() if key != "independent_audit"}
    if audit["registration"].get("scientific_config") != scientific:
        raise ValueError("scientific registration mismatch")
    private.mkdir(mode=0o700, parents=True, exist_ok=False)
    bootstrap.mkdir(mode=0o700, parents=True, exist_ok=False)
    for target, raw in [(private / "config.json", raw_config),
                        (bootstrap / "candidates.csv", candidates),
                        (bootstrap / "reader.py", script),
                        (bootstrap / "registration.json", registration)]:
        vault.write_new(target, raw, 0o600)
    receipt = {"format": ("exit-continuation-input-receipt-v1" if evaluation_mode == "offline_exit_continuation"
                           else "exit-retry-input-receipt-v1"), "status": "complete",
               "configuration_sha256": request_sha, "expected_code_commit": expected_commit,
               "candidate_bytes": len(candidates), "candidate_sha256": hashlib.sha256(candidates).hexdigest(),
               "independent_reader_sha256": reader_sha,
               "registration_canonical_sha256": hashlib.sha256(registration).hexdigest(),
               "scope": "Private input restoration only; raw archive verification belongs to evaluation.",
               "actual_model_fit_calls": 0, "actual_model_predict_calls": 0,
               "actual_fills": 0, "live_orders_enabled": False}
    vault.write_new(private / "input-receipt.json", vault.canonical(receipt), 0o600)
    return {"source_commit": config["source_commit"], "reader_sha256": reader_sha}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--evaluation-mode", choices=("offline_exit_retry", "offline_exit_continuation"),
                        default="offline_exit_retry")
    args = parser.parse_args()
    try:
        result = materialize(os.environ["AUDIT_CONFIG_B64"], os.environ["CANDIDATE_INPUT_B64"],
                             os.environ["PRIVATE_AUDIT_B64"], os.environ["EXPECTED_SHA256"],
                             os.environ["GITHUB_SHA"], args.private, args.bootstrap,
                             evaluation_mode=args.evaluation_mode)
        # Values restored from an encoded secret are not automatically masked
        # when later steps print their environment or checkout metadata.
        for value in (result["source_commit"], result["source_commit"][:7], result["reader_sha256"]):
            print("::add-mask::" + value, flush=True)
        with open(os.environ["GITHUB_ENV"], "a") as handle:
            handle.write("AUDIT_SOURCE_COMMIT=" + result["source_commit"] + "\n")
            handle.write("PRIVATE_AUDIT_SHA=" + result["reader_sha256"] + "\n")
    except Exception as exc:
        args.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        failure = args.private / "setup-failure.json"
        if not failure.exists():
            vault.write_new(failure, vault.canonical({"format": "exit-retry-private-setup-failure-v1",
                "type": type(exc).__name__, "error": str(exc), "traceback": traceback.format_exc(),
                "actual_model_fit_calls": 0, "actual_model_predict_calls": 0,
                "replayed_cells": 0, "live_orders_enabled": False}), 0o600)
        raise SystemExit("Private offline comparison inputs missing or invalid") from None


if __name__ == "__main__":
    main()

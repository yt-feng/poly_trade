"""Check the internal consistency of one private PolyBolt compound bundle.

The input is read from a runner-local file created from a GitHub secret.  Only
an allowlisted, identifier-free result is written to the requested output;
the bundle itself is never printed or uploaded.
Neither the result nor a successful exit authenticates receipts.  Exit zero
means a diagnostic result was written, including for rejected input.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from validate_polybolt_compound_bundle import (
    DEFAULT_STRATEGY_PROFILE,
    H546_STRATEGY_PROFILE,
    H530_STRATEGY_PROFILE,
    PRIVATE_STRATEGY_PROFILE,
    OFFLINE_VERIFICATION,
    validate_bundle_document,
)


MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024
RESULT_KEYS = (
    "schema",
    "required_fills",
    "event_count",
    "source_event_count",
    "source_update_count",
    "source_continuity_ok",
    "source_reconnect_segments",
    "market_window_count",
    "fills",
    "consecutive",
    "paper_replay_passed",
    "gate_passed",
    "offline_consistency_passed",
    "evidence_scope",
    "receipt_authenticity_verified",
    "actual_fills",
    "verified_canary_fills",
    "failure_reason",
    "owner_scoped",
    "identity_complete",
    "same_ledger_identity_verified",
    "cash_amounts_reconciled",
    "source_fill_references_verified",
    "strategy_profile",
)


def _failure(reason: str) -> dict[str, Any]:
    result = {key: None for key in RESULT_KEYS}
    result.update({
        "schema": "authenticated-polybolt-compound-bundle-v2",
        "required_fills": 10,
        "event_count": 0,
        "source_event_count": 0,
        **OFFLINE_VERIFICATION,
        "offline_consistency_passed": False,
        "failure_reason": reason,
    })
    return result


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("bundle_file_invalid")
    data = path.read_bytes()
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle_file_oversized")
    return json.loads(data.decode("utf-8"))


def _allowlist(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result.get(key) for key in RESULT_KEYS}


def _read_private_strategy_config(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        raise ValueError("private_strategy_profile_unavailable")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("private_strategy_profile_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("private_strategy_profile_invalid")
    return value


def run(
    bundle_path: Path,
    *,
    strategy_profile: str = DEFAULT_STRATEGY_PROFILE,
    private_strategy_config_path: Path | None = None,
) -> dict[str, Any]:
    if not bundle_path.exists():
        return _failure("bundle_secret_unavailable")
    document = _read_json(bundle_path)
    if not isinstance(document, dict):
        raise ValueError("bundle_json_object_required")
    private_config = _read_private_strategy_config(private_strategy_config_path)
    result = validate_bundle_document(
        document,
        strategy_profile=strategy_profile,
        private_strategy_config=private_config,
    )
    result["strategy_profile"] = strategy_profile
    return _allowlist(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--strategy-profile",
        choices=(DEFAULT_STRATEGY_PROFILE, H530_STRATEGY_PROFILE, H546_STRATEGY_PROFILE, PRIVATE_STRATEGY_PROFILE),
        default=DEFAULT_STRATEGY_PROFILE,
    )
    parser.add_argument("--private-strategy-config", type=Path)
    args = parser.parse_args()
    try:
        result = run(
            args.bundle,
            strategy_profile=args.strategy_profile,
            private_strategy_config_path=args.private_strategy_config,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason in {"private_strategy_profile_unavailable", "private_strategy_profile_invalid"}:
            result = _failure(reason)
        else:
            result = _failure("bundle_invalid")
    except Exception:
        # Keep malformed input fail-closed while still producing a generic
        # encrypted round record.  Never expose parser details or the bundle.
        result = _failure("bundle_invalid")
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("admission_result_oversized")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({key: result.get(key) for key in (
        "gate_passed", "offline_consistency_passed", "evidence_scope",
        "receipt_authenticity_verified", "verified_canary_fills",
        "failure_reason", "fills", "source_continuity_ok",
    )}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""v2.75 bounded 12-hour/144-window, at-most-one-POST canary.

The default and ``coordinate`` paths import no broker, signer, or v274 module.
The coordinator has no network namespace.  It delegates each exact next window
to a short credentialless ``prepare-next`` systemd unit.  Only a separately
started ``execute-first-eligible`` unit receives ``live.env``; that path fully
verifies the horizon-derived child through the frozen v274 verifier, durably
claims the sole horizon attempt, and calls frozen v274 ``execute_once``.

No field in this contract describes a derived child as a direct one-window user
authorization.  The explicit authorization is the bounded parent.  A sealed
schedule, 144 child commitments, a root receipt, and a chained per-window
receipt ledger prove that only the strict next contiguous child may run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


VERSION = "2.75.0-bounded-12h-first-eligible-v1"
STRATEGY_ID = "A_v271_threshold"

HORIZON_SECONDS = 12 * 60 * 60
WINDOW_SECONDS = 300
HORIZON_WINDOWS = 144
COORDINATOR_LAUNCH_LEAD_MILLISECONDS = 120_000
EXACT_SHARES = "5"
MAXIMUM_TOTAL_RESERVE_USDC = "2.50"
ENTRY_STRESS_CENTS = "0.01"
EXECUTION_LIMIT_PRICE_CAP = "0.40"
FEE_RATE = "0.07"

AUTHORIZATION_SCHEMA = "btc5m-v275-explicit-bounded-horizon-authorization-v1"
PUBLIC_IDENTITY_SCHEMA = "btc5m-v275-public-execution-identity-v1"
SCHEDULE_SCHEMA = "btc5m-v275-bounded-horizon-schedule-v1"
MANIFEST_SCHEMA = "btc5m-v275-bounded-horizon-child-commitment-manifest-v1"
HORIZON_RECEIPT_SCHEMA = "btc5m-v275-bounded-horizon-root-receipt-v1"
CHILD_SCHEMA = "btc5m-v275-derived-window-authorization-v1"
CLAIM_SCHEMA = "btc5m-v275-horizon-claim-v1"
REQUEST_SCHEMA = "btc5m-v275-strict-next-window-request-v1"
ANCHOR_SCHEMA = "btc5m-v275-window-qualification-anchor-v1"
CANDIDATE_SCHEMA = "btc5m-v275-horizon-derived-candidate-v1"
PERMIT_SCHEMA = "btc5m-v275-horizon-derived-disabled-permit-v1"
WINDOW_RECEIPT_SCHEMA = "btc5m-v275-window-terminal-receipt-v1"
DELEGATION_SCHEMA = "btc5m-v275-first-eligible-delegation-v1"
EXECUTE_INVOCATION_SCHEMA = "btc5m-v275-execute-invocation-claim-v1"
ATTEMPT_SCHEMA = "btc5m-v275-global-attempt-claim-v1"
TRANSPORT_SCHEMA = "btc5m-v275-global-transport-terminal-v1"
TERMINAL_SCHEMA = "btc5m-v275-bounded-horizon-terminal-v1"
EXECUTE_WRAPPER_SCHEMA = "btc5m-v275-execute-wrapper-result-v1"

ZERO_ATTEMPT_TERMINAL_DECISIONS = frozenset(
    {
        "boot_drift_terminal",
        "wall_clock_rollback_terminal",
        "horizon_expired_before_resume_terminal",
        "launch_window_missed_terminal",
        "strict_next_ledger_refusal_terminal",
        "incomplete_child_terminal",
        "strict_next_window_missed_terminal",
        "prepare_exit_without_complete_boundary_terminal",
        "eligible_boundary_without_same_process_delegation_terminal",
        "pre_delegation_ledger_drift_terminal",
        "shared_lock_replaced_pre_delegation_terminal",
        "public_identity_replaced_pre_delegation_terminal",
    }
)
UNKNOWN_SUBMISSION_TERMINAL_DECISIONS = frozenset(
    {
        "attempt_without_delegation_terminal",
        "delegation_child_path_ambiguous_terminal",
        "consuming_delegation_process_state_unknown_terminal",
        "global_attempt_claimed_process_state_unknown_terminal",
        "pre_submit_intent_process_state_unknown_terminal",
        "first_eligible_submission_unknown_terminal",
    }
)

AUTHORIZATION_ACK = (
    "I_AUTHORIZE_ONE_EXACT_BOUNDED_TWELVE_HOUR_SEQUENCE_OF_144_CONTIGUOUS_"
    "BTC5M_WINDOWS_FOR_A_V271_THRESHOLD_WITH_AT_MOST_ONE_GLOBAL_BUY_FOK_"
    "POST_OF_EXACTLY_FIVE_SHARES_AND_MAXIMUM_2_50_USDC_RESERVE_FIRST_"
    "ELIGIBLE_SIGNAL_ONLY_CASH_WINDOWS_CONTINUE_NO_RETRY_NO_EXTENSION"
)

V274_SOURCE_SHA256 = "32732ea77e77116680d7a1ce2a65b00c2cc1f24512a2e6f387c9e5a543c634a1"
V274_VERSION = "2.74.0-exact-one-window-split-qualification-live-signal"
V274_INTENT_SCHEMA = "btc5m-v274-exact-one-window-intent-v1"
V274_TRANSPORT_STARTED_SCHEMA = "btc5m-v274-transport-started-v1"
V274_EXECUTION_RESULT_SCHEMA = "btc5m-v274-exact-one-window-execution-result-v1"
V271_COLLECTOR_SHA256 = "620301883ed5621b618afc97e970b031fa4d52721013b815bdca30b7f85b1de7"
V271_FORWARD_SHA256 = "5c87684b8e83c9b4bdae617975ddff858f93b04786922f8c232e70a1578a6948"
V2733_VALIDATOR_SHA256 = "73160178c1aceb3829a611667e3225dda39b24bfaf2f66c5e499d4261d789fc7"
V273_RUNTIME_SHA256 = "d19ef79f9308fef7aa6bc358c3fdc7e92f7149fd1100319fae982e34b219b89f"
V273_PREFLIGHT_SHA256 = "99bdb8a78643b234eadc04464d47c75f7a4bfd3c434385b10248f299930fec40"
V274_PROVENANCE_AUTHORIZATION_SCHEMA = (
    "btc5m-v274-conditional-preauthorization-split-identities-v1"
)
V274_PROVENANCE_AUTHORIZATION_SHA256 = (
    "87cd489b985a7a9225cd758ca14a55ca8c2d7dc29292b292b4230744161ec16c"
)

KNOWN_QUALIFICATION_CONTRACT: dict[str, Any] = {
    "schema": "btc5m-v274-known-qualification-contract-v1",
    "qualification_experiment_id": "3b448c63f856d69c38a2ceba23a371cdbc015e446e6c4dd6d4b14fe0d0042006",
    "qualification_forward_cutoff": "2026-08-24T11:50:00Z",
    "qualification_report_sha256": "04109fb4f795de3cff397424f9f588deb5f1d897c4c442be35dc5b8b711dbde8",
    "qualification_report_artifact_sha256": "d3c434f6109e7ccab6142e626d44614d2c3bceb0235db3e3ceb0fee1ce55953e",
    "qualification_paper_ledger_sha256": "974a66e38699d9ba8512bdad96d6243b15602fcb9f57b7682a00947651f48ab6",
    "qualification_panel_ledger_sha256": "e5f7886bac4a01ede7e6be07d1b6ff98cb123ce51813482ed33530e38972ca0a",
    "qualification_forward_release_sha256": V271_FORWARD_SHA256,
    "qualification_collector_release_sha256": V271_COLLECTOR_SHA256,
    "qualification_horizon_windows": 288,
    "qualification_report_created_at": "2026-08-25T14:41:26.379098+00:00",
}

CANONICAL_DATA_DIR = Path("/var/lib/poly-bot-btc5m")
CANONICAL_ROOT = CANONICAL_DATA_DIR / "canary" / "v275"
CANONICAL_AUTHORIZATION = Path("/etc/poly-bot-btc5m/v275-bounded-horizon-authorization.json")
CANONICAL_SCHEDULE = Path("/etc/poly-bot-btc5m/v275-bounded-horizon-schedule.json")
CANONICAL_MANIFEST = Path("/etc/poly-bot-btc5m/v275-bounded-horizon-manifest.json")
CANONICAL_HORIZON_RECEIPT = Path("/etc/poly-bot-btc5m/v275-bounded-horizon-receipt.json")
CANONICAL_NONSECRET_ENVIRONMENT = Path("/etc/poly-bot-btc5m/v275-bounded-horizon.env")
CANONICAL_LIVE_ENVIRONMENT = Path("/etc/poly-bot-btc5m/live.env")
CANONICAL_PUBLIC_IDENTITY_DIRECTORY = Path(
    "/etc/poly-bot-btc5m/v275-public-identity"
)
CANONICAL_PUBLIC_IDENTITY = (
    CANONICAL_PUBLIC_IDENTITY_DIRECTORY / "public-execution-identity.json"
)
CANONICAL_V274_PROVENANCE_AUTHORIZATION = Path(
    "/etc/poly-bot-btc5m/v274-conditional-preauthorization.json"
)
CANONICAL_QUALIFICATION_ROOT = (
    CANONICAL_DATA_DIR / "canary" / "v274" / "qualification" / "v271_20260824_115000Z"
)
CANONICAL_QUALIFICATION_MANIFEST = CANONICAL_QUALIFICATION_ROOT / "manifest.json"
CANONICAL_QUALIFICATION_REPORT = CANONICAL_QUALIFICATION_ROOT / "forward_report.json"
CANONICAL_SYSTEMCTL = Path("/usr/bin/systemctl")
CANONICAL_SHARED_LOCK = (
    CANONICAL_DATA_DIR / "canary" / "v264" / "execution.lock"
)
SHARED_LOCK_PROTOCOL = "v264-persistent-block-v1"
SHARED_LOCK_OWNER_UID = 0
SHARED_LOCK_GROUP_NAME = "ubuntu"
SHARED_LOCK_MODE = 0o660
CANONICAL_PREPARE_UNIT = "poly-bot-bounded-horizon-canary-v275-prepare.service"
CANONICAL_EXECUTE_UNIT = "poly-bot-bounded-horizon-canary-v275-execute.service"
CANONICAL_UNIT_DIRECTORY = Path("/etc/systemd/system")
CANONICAL_RUNTIME_ROOT = Path(
    "/usr/local/lib/poly-bot-btc5m-v275/runtime-v1"
)
CANONICAL_RUNTIME_SOURCE = (
    CANONICAL_RUNTIME_ROOT / "app" / "bounded_horizon_canary_v275.py"
)

CONTRACT_ENVIRONMENT = {
    "V275_AUTHORIZATION_PATH": str(CANONICAL_AUTHORIZATION),
    "V275_SCHEDULE_PATH": str(CANONICAL_SCHEDULE),
    "V275_MANIFEST_PATH": str(CANONICAL_MANIFEST),
    "V275_HORIZON_RECEIPT_PATH": str(CANONICAL_HORIZON_RECEIPT),
}
NONSECRET_ENVIRONMENT_BYTES = (
    "\n".join(f"{key}={value}" for key, value in CONTRACT_ENVIRONMENT.items())
    + "\n"
).encode("ascii")
UNIT_SOURCE_NAMES = {
    "identity_provisioner_unit_sha256": (
        "poly-bot-bounded-horizon-canary-v275-provision-identity.service"
    ),
    "coordinator_unit_sha256": "poly-bot-bounded-horizon-canary-v275-coordinator.service",
    "prepare_unit_sha256": "poly-bot-bounded-horizon-canary-v275-prepare.service",
    "execute_unit_sha256": "poly-bot-bounded-horizon-canary-v275-execute.service",
}
EFFECTIVE_SYSTEMD_PROPERTIES = (
    "LoadState",
    "UnitFileState",
    "FragmentPath",
    "DropInPaths",
    "ExecStart",
    "EnvironmentFiles",
    "Environment",
    "UnsetEnvironment",
    "User",
    "Group",
    "Type",
    "PrivateNetwork",
    "Restart",
    "RestartUSec",
    "StartLimitIntervalUSec",
    "StartLimitBurst",
    "NoNewPrivileges",
    "CapabilityBoundingSet",
    "ProtectSystem",
    "ProtectHome",
    "PrivateDevices",
    "PrivateTmp",
    "UMask",
    "TimeoutStartUSec",
    "RuntimeMaxUSec",
    "InaccessiblePaths",
    "ReadOnlyPaths",
    "ReadWritePaths",
    "RestrictAddressFamilies",
    "TriggeredBy",
    "Triggers",
    "WantedBy",
    "RequiredBy",
    "UpheldBy",
    "BoundBy",
    "OnFailureOf",
)
SECRET_ENVIRONMENT_KEYS = frozenset(
    {
        "BTC5M_POLYMARKET_PRIVATE_KEY",
        "BTC5M_POLYMARKET_API_KEY",
        "BTC5M_POLYMARKET_API_SECRET",
        "BTC5M_POLYMARKET_API_PASSPHRASE",
    }
)
DANGEROUS_LOADER_ENVIRONMENT_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
        "PYTHONPLATLIBDIR",
        "LD_PRELOAD",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
    }
)

HASH = re.compile(r"^[0-9a-f]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class HorizonRefusal(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ContractFiles:
    authorization: Path
    schedule: Path
    manifest: Path
    horizon_receipt: Path


@dataclass(frozen=True, slots=True)
class HorizonPaths:
    run_dir: Path
    claim: Path
    delegation: Path
    execute_invocation: Path
    attempt: Path
    transport: Path
    terminal: Path


@dataclass(frozen=True, slots=True)
class WindowPaths:
    run_dir: Path
    request: Path
    child_authorization: Path
    v274_projection_receipt: Path
    qualification_anchor: Path
    live_definition: Path
    v271_definition: Path
    panel_ledger: Path
    live_signal_row: Path
    candidate: Path
    permit: Path
    completion: Path
    intent: Path
    transport_started: Path
    submission_unknown: Path
    execution_result: Path
    window_receipt: Path
    execute_wrapper_result: Path


def canonical_bytes(payload: Any, *, newline: bool = False) -> bytes:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_sha256() -> str:
    return file_sha256(Path(__file__).resolve())


def release_contract(*, v275_release_sha256: str | None = None) -> dict[str, Any]:
    deploy = Path(__file__).resolve().with_name("deploy")
    unit_hashes = {
        field: file_sha256(deploy / filename)
        for field, filename in UNIT_SOURCE_NAMES.items()
    }
    contract = {
        "schema": "btc5m-v275-release-and-credential-boundary-contract-v1",
        "v275_release_sha256": v275_release_sha256 or release_sha256(),
        "v274_release_sha256": V274_SOURCE_SHA256,
        "v271_collector_release_sha256": V271_COLLECTOR_SHA256,
        "v271_forward_release_sha256": V271_FORWARD_SHA256,
        "v2733_validator_release_sha256": V2733_VALIDATOR_SHA256,
        "v273_runtime_release_sha256": V273_RUNTIME_SHA256,
        "v273_preflight_release_sha256": V273_PREFLIGHT_SHA256,
        "nonsecret_environment_sha256": hashlib.sha256(
            NONSECRET_ENVIRONMENT_BYTES
        ).hexdigest(),
        "nonsecret_environment_exact_key_count": 4,
        "public_identity_provenance_authorization_path": str(
            CANONICAL_V274_PROVENANCE_AUTHORIZATION
        ),
        "public_identity_provenance_authorization_schema": (
            V274_PROVENANCE_AUTHORIZATION_SCHEMA
        ),
        "public_identity_provenance_authorization_sha256": (
            V274_PROVENANCE_AUTHORIZATION_SHA256
        ),
        "runtime_revalidates_public_identity_provenance": True,
        "identity_provisioner_loads_live_env": False,
        "coordinator_loads_live_env": False,
        "prepare_loads_live_env": False,
        "execute_loads_live_env": True,
        **unit_hashes,
    }
    return contract


def _hash(value: Any, field: str) -> str:
    text = str(value or "")
    if not HASH.fullmatch(text):
        raise HorizonRefusal("hash_invalid", f"{field} is not lowercase sha256")
    return text


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise HorizonRefusal("integer_invalid", f"{field} is not an integer >= {minimum}")
    return value


def _address(value: Any, field: str) -> str:
    text = str(value or "")
    if not ADDRESS.fullmatch(text):
        raise HorizonRefusal("address_invalid", f"{field} is malformed")
    return text.lower()


def _write_exclusive_json(path: Path, payload: dict[str, Any]) -> str:
    encoded = canonical_bytes(payload, newline=True)
    digest = hashlib.sha256(encoded).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise HorizonRefusal("immutable_output_exists", f"{path} exists") from exc
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise HorizonRefusal("short_write", f"short write for {path}")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return digest


def read_canonical_object(
    path: Path,
    *,
    require_root_owned: bool,
) -> tuple[dict[str, Any], str]:
    if require_root_owned:
        _require_trusted_directory_chain(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HorizonRefusal("artifact_unreadable", f"{path}: {type(exc).__name__}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or (require_root_owned and metadata.st_uid != 0)
        ):
            raise HorizonRefusal("artifact_identity_invalid", f"{path} is not trusted")
        blocks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
    finally:
        os.close(descriptor)
    raw = b"".join(blocks)
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HorizonRefusal("artifact_json_invalid", f"{path} is not JSON") from exc
    if not isinstance(payload, dict) or raw != canonical_bytes(payload, newline=True):
        raise HorizonRefusal("artifact_not_canonical", f"{path} is not canonical")
    return payload, hashlib.sha256(raw).hexdigest()


def _read_secure_bytes(
    path: Path,
    *,
    require_root_owned: bool,
    require_mode_0600: bool = False,
    require_root_group: bool = False,
) -> tuple[bytes, str]:
    if require_root_owned:
        _require_trusted_directory_chain(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HorizonRefusal("artifact_unreadable", f"{path}: {type(exc).__name__}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or (require_root_owned and metadata.st_uid != 0)
            or (require_root_group and metadata.st_gid != 0)
            or (require_mode_0600 and stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise HorizonRefusal("artifact_identity_invalid", f"{path} is not trusted")
        blocks: list[bytes] = []
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            blocks.append(block)
    finally:
        os.close(descriptor)
    raw = b"".join(blocks)
    return raw, hashlib.sha256(raw).hexdigest()


def _validate_public_identity_directory(
    directory: Path = CANONICAL_PUBLIC_IDENTITY_DIRECTORY,
    *,
    require_root_owned: bool,
    require_canonical_path: bool,
) -> os.stat_result:
    """Require the one narrow root:root 0700 identity output directory."""

    normalized = Path(os.path.abspath(directory))
    if (
        require_canonical_path
        and normalized != CANONICAL_PUBLIC_IDENTITY_DIRECTORY
    ):
        raise HorizonRefusal(
            "public_identity_directory_path_invalid",
            "public identity directory path is not canonical",
        )
    try:
        metadata = os.lstat(normalized)
    except OSError as exc:
        raise HorizonRefusal(
            "public_identity_directory_unreadable",
            f"{normalized}: {type(exc).__name__}",
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (
            require_root_owned
            and (metadata.st_uid != 0 or metadata.st_gid != 0)
        )
    ):
        raise HorizonRefusal(
            "public_identity_directory_identity_invalid",
            "public identity directory is not the exact root:root 0700 directory",
        )
    if require_root_owned:
        _require_trusted_directory_chain(
            normalized,
            sticky_group_writable_exception=None,
        )
    return metadata


def _write_public_identity_exclusive(
    output_path: Path,
    payload: dict[str, Any],
    *,
    require_root_owned: bool,
    require_canonical_path: bool,
) -> str:
    """O_EXCL+fsync through one already-verified identity directory inode."""

    normalized = Path(os.path.abspath(output_path))
    expected_directory = normalized.parent
    directory_metadata = _validate_public_identity_directory(
        expected_directory,
        require_root_owned=require_root_owned,
        require_canonical_path=require_canonical_path,
    )
    if require_canonical_path and normalized != CANONICAL_PUBLIC_IDENTITY:
        raise HorizonRefusal(
            "public_execution_identity_path_invalid",
            "public execution identity output path is not canonical",
        )
    encoded = canonical_bytes(payload, newline=True)
    digest = hashlib.sha256(encoded).hexdigest()
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(expected_directory, directory_flags)
    except OSError as exc:
        raise HorizonRefusal(
            "public_identity_directory_unreadable",
            f"{expected_directory}: {type(exc).__name__}",
        ) from exc
    try:
        opened_directory_metadata = os.fstat(directory_descriptor)
        if (
            opened_directory_metadata.st_dev,
            opened_directory_metadata.st_ino,
        ) != (directory_metadata.st_dev, directory_metadata.st_ino) or (
            not stat.S_ISDIR(opened_directory_metadata.st_mode)
            or stat.S_IMODE(opened_directory_metadata.st_mode) != 0o700
            or (
                require_root_owned
                and (
                    opened_directory_metadata.st_uid != 0
                    or opened_directory_metadata.st_gid != 0
                )
            )
        ):
            raise HorizonRefusal(
                "public_identity_directory_replaced",
                "public identity directory changed before O_EXCL",
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                normalized.name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError as exc:
            raise HorizonRefusal(
                "immutable_output_exists",
                f"{normalized} exists",
            ) from exc
        except OSError as exc:
            raise HorizonRefusal(
                "public_identity_output_unwritable",
                f"{normalized}: {type(exc).__name__}",
            ) from exc
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise HorizonRefusal(
                        "short_write",
                        f"short write for {normalized}",
                    )
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return digest


def _decode_canonical_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HorizonRefusal(
            "artifact_json_invalid",
            f"{label} is not JSON",
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_bytes(payload, newline=True):
        raise HorizonRefusal(
            "artifact_not_canonical",
            f"{label} is not canonical",
        )
    return payload


def _read_v274_identity_provenance(
    path: Path = CANONICAL_V274_PROVENANCE_AUTHORIZATION,
    *,
    require_root_owned: bool,
    require_canonical_path: bool,
) -> tuple[dict[str, Any], str]:
    """Read the exact external source of truth for the public account tuple."""

    normalized = Path(os.path.abspath(path))
    if (
        require_canonical_path
        and normalized != CANONICAL_V274_PROVENANCE_AUTHORIZATION
    ):
        raise HorizonRefusal(
            "v274_identity_provenance_path_invalid",
            "v274 identity provenance path is not canonical",
        )
    raw, source_sha = _read_secure_bytes(
        normalized,
        require_root_owned=require_root_owned,
        require_mode_0600=True,
        require_root_group=require_root_owned,
    )
    source = _decode_canonical_object(raw, label=str(normalized))
    if (
        source_sha != V274_PROVENANCE_AUTHORIZATION_SHA256
        or source.get("schema") != V274_PROVENANCE_AUTHORIZATION_SCHEMA
    ):
        raise HorizonRefusal(
            "v274_identity_provenance_invalid",
            "sealed v274 authorization path/schema/raw SHA differs",
        )
    _address(source.get("expected_signer"), "v274_provenance.expected_signer")
    _address(source.get("expected_funder"), "v274_provenance.expected_funder")
    signature_type = _integer(
        source.get("signature_type"),
        "v274_provenance.signature_type",
    )
    if signature_type not in {0, 1, 2, 3}:
        raise HorizonRefusal(
            "signature_type_invalid",
            "v274 provenance has unsupported signature type",
        )
    return source, source_sha


def _validate_public_identity_against_v274_provenance(
    identity: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    expected = build_public_execution_identity(
        expected_signer=provenance.get("expected_signer"),
        expected_funder=provenance.get("expected_funder"),
        signature_type=provenance.get("signature_type"),
    )
    if dict(identity) != expected:
        raise HorizonRefusal(
            "public_identity_provenance_tuple_mismatch",
            "installed public identity tuple was not derived from exact v274 bytes",
        )


def _read_public_execution_identity(
    path: Path = CANONICAL_PUBLIC_IDENTITY,
    *,
    require_root_owned: bool,
    require_canonical_path: bool,
) -> tuple[dict[str, Any], str]:
    normalized = Path(os.path.abspath(path))
    if require_canonical_path and normalized != CANONICAL_PUBLIC_IDENTITY:
        raise HorizonRefusal(
            "public_execution_identity_path_invalid",
            "public execution identity path is not canonical",
        )
    _validate_public_identity_directory(
        normalized.parent,
        require_root_owned=require_root_owned,
        require_canonical_path=require_canonical_path,
    )
    raw, artifact_sha = _read_secure_bytes(
        normalized,
        require_root_owned=require_root_owned,
        require_mode_0600=True,
        require_root_group=require_root_owned,
    )
    identity = validate_public_execution_identity(
        _decode_canonical_object(raw, label=str(normalized))
    )
    if require_canonical_path:
        provenance, _provenance_sha = _read_v274_identity_provenance(
            path=CANONICAL_V274_PROVENANCE_AUTHORIZATION,
            require_root_owned=require_root_owned,
            require_canonical_path=True,
        )
        _validate_public_identity_against_v274_provenance(
            identity,
            provenance,
        )
    return identity, artifact_sha


def _validate_installed_public_execution_identity(
    authorization: Mapping[str, Any],
    *,
    path: Path = CANONICAL_PUBLIC_IDENTITY,
    require_root_owned: bool,
    require_canonical_path: bool,
) -> None:
    identity, artifact_sha = _read_public_execution_identity(
        path,
        require_root_owned=require_root_owned,
        require_canonical_path=require_canonical_path,
    )
    if (
        identity != authorization.get("public_execution_identity")
        or artifact_sha
        != authorization.get("public_execution_identity_artifact_sha256")
        or identity.get("expected_signer")
        != authorization.get("expected_signer")
        or identity.get("expected_funder")
        != authorization.get("expected_funder")
        or identity.get("signature_type")
        != authorization.get("signature_type")
    ):
        raise HorizonRefusal(
            "installed_public_execution_identity_mismatch",
            "installed public identity differs from the authorized account axis",
        )


def _revalidate_execution_public_identity(
    authorization: Mapping[str, Any],
    *,
    require_root_owned: bool,
) -> None:
    """Re-read the external account anchor at an execution boundary."""

    if require_root_owned:
        _validate_installed_public_execution_identity(
            authorization,
            path=CANONICAL_PUBLIC_IDENTITY,
            require_root_owned=True,
            require_canonical_path=True,
        )


def provision_public_execution_identity_from_v274(
    *,
    source_path: Path = CANONICAL_V274_PROVENANCE_AUTHORIZATION,
    output_path: Path = CANONICAL_PUBLIC_IDENTITY,
    require_root: bool = True,
) -> tuple[dict[str, Any], str]:
    """One-time credentialless projection from the exact sealed v274 auth."""

    normalized_source = Path(os.path.abspath(source_path))
    normalized_output = Path(os.path.abspath(output_path))
    if require_root and (
        os.geteuid() != 0
        or normalized_source != CANONICAL_V274_PROVENANCE_AUTHORIZATION
        or normalized_output != CANONICAL_PUBLIC_IDENTITY
    ):
        raise HorizonRefusal(
            "public_identity_provision_override_forbidden",
            "production identity provisioning uses exact canonical paths as root",
        )
    if require_root:
        runtime_source = Path(__file__).resolve()
        if runtime_source != CANONICAL_RUNTIME_SOURCE:
            raise HorizonRefusal(
                "runtime_source_path_invalid",
                "identity provisioner is not using the canonical v275 snapshot",
            )
        runtime_metadata = os.lstat(runtime_source)
        if (
            not stat.S_ISREG(runtime_metadata.st_mode)
            or stat.S_ISLNK(runtime_metadata.st_mode)
            or runtime_metadata.st_nlink != 1
            or runtime_metadata.st_uid != 0
            or runtime_metadata.st_mode & 0o022
        ):
            raise HorizonRefusal(
                "runtime_source_identity_invalid",
                "identity provisioner runtime source is not immutable/root-owned",
            )
        _require_trusted_directory_chain(runtime_source.parent)
        _require_trusted_executable(CANONICAL_SYSTEMCTL)
        if any(
            key in os.environ
            for key in (
                *SECRET_ENVIRONMENT_KEYS,
                *DANGEROUS_LOADER_ENVIRONMENT_KEYS,
            )
        ):
            raise HorizonRefusal(
                "identity_provisioner_environment_invalid",
                "credential or loader environment reached the public identity provisioner",
            )
        # Query the manager, not only the unit files: stale loaded bytes or a
        # drop-in must be refused before the provenance projection is written.
        effective_systemd_contract()
    source, _source_sha = _read_v274_identity_provenance(
        path=normalized_source,
        require_root_owned=require_root,
        require_canonical_path=require_root,
    )
    identity = build_public_execution_identity(
        expected_signer=source.get("expected_signer"),
        expected_funder=source.get("expected_funder"),
        signature_type=source.get("signature_type"),
    )
    artifact_sha = _write_public_identity_exclusive(
        normalized_output,
        identity,
        require_root_owned=require_root,
        require_canonical_path=require_root,
    )
    reread, reread_sha = _read_public_execution_identity(
        normalized_output,
        require_root_owned=require_root,
        require_canonical_path=require_root,
    )
    if reread != identity or reread_sha != artifact_sha:
        raise HorizonRefusal(
            "public_identity_provision_verification_failed",
            "new public identity bytes changed after O_EXCL fsync",
        )
    return identity, artifact_sha


def _validate_live_environment_identity(
    path: Path = CANONICAL_LIVE_ENVIRONMENT,
    *,
    require_canonical_path: bool = True,
    require_root_owned: bool = True,
) -> None:
    """Validate live.env metadata without reading, hashing, or returning bytes."""

    normalized = Path(os.path.abspath(path))
    if require_canonical_path and normalized != CANONICAL_LIVE_ENVIRONMENT:
        raise HorizonRefusal(
            "live_environment_path_invalid",
            "execute credential file path is not canonical",
        )
    if require_root_owned:
        _require_trusted_directory_chain(normalized.parent)
    flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(normalized, flags)
    except OSError as exc:
        raise HorizonRefusal(
            "live_environment_unreadable",
            f"execute credential file metadata: {type(exc).__name__}",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        try:
            path_metadata = os.lstat(normalized)
        except OSError as exc:
            raise HorizonRefusal(
                "live_environment_identity_invalid",
                f"execute credential path metadata: {type(exc).__name__}",
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(path_metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (require_root_owned and (metadata.st_uid != 0 or metadata.st_gid != 0))
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise HorizonRefusal(
                "live_environment_identity_invalid",
                "execute credential file is not one canonical root:root 0600 inode",
            )
    finally:
        os.close(descriptor)


def _shared_lock_identity(
    path: Path = CANONICAL_SHARED_LOCK,
    *,
    require_root_owned: bool,
) -> dict[str, Any]:
    """Read the accepted root:ubuntu 0660 signer-lock inode without links."""

    if Path(os.path.abspath(path)) != CANONICAL_SHARED_LOCK:
        raise HorizonRefusal(
            "shared_lock_path_invalid",
            "shared execution lock path is not canonical",
        )
    if require_root_owned:
        _require_trusted_directory_chain(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HorizonRefusal(
            "shared_lock_unreadable",
            f"shared execution lock: {type(exc).__name__}",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        expected_group_gid: int | None = None
        if require_root_owned:
            import grp

            try:
                expected_group_gid = grp.getgrnam(
                    SHARED_LOCK_GROUP_NAME
                ).gr_gid
            except KeyError as exc:
                raise HorizonRefusal(
                    "shared_lock_group_missing",
                    "accepted ubuntu signer-lock group does not exist",
                ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != SHARED_LOCK_MODE
            or (
                require_root_owned
                and (
                    metadata.st_uid != SHARED_LOCK_OWNER_UID
                    or metadata.st_gid != expected_group_gid
                )
            )
        ):
            raise HorizonRefusal(
                "shared_lock_identity_invalid",
                "shared execution lock is not the accepted root:ubuntu 0660 inode",
            )
        return {
            "shared_execution_lock_protocol": SHARED_LOCK_PROTOCOL,
            "shared_execution_lock_path": str(CANONICAL_SHARED_LOCK),
            "shared_execution_lock_device": int(metadata.st_dev),
            "shared_execution_lock_inode": int(metadata.st_ino),
            "shared_execution_lock_owner_uid": int(metadata.st_uid),
            "shared_execution_lock_group_gid": int(metadata.st_gid),
            "shared_execution_lock_mode_octal": "0660",
        }
    finally:
        os.close(descriptor)


def _validate_shared_lock_identity(
    receipt: dict[str, Any],
    *,
    require_root_owned: bool,
) -> None:
    expected = {
        "shared_execution_lock_protocol": receipt.get(
            "shared_execution_lock_protocol"
        ),
        "shared_execution_lock_path": receipt.get("shared_execution_lock_path"),
        "shared_execution_lock_device": _integer(
            receipt.get("shared_execution_lock_device"),
            "shared_execution_lock_device",
        ),
        "shared_execution_lock_inode": _integer(
            receipt.get("shared_execution_lock_inode"),
            "shared_execution_lock_inode",
        ),
        "shared_execution_lock_owner_uid": _integer(
            receipt.get("shared_execution_lock_owner_uid"),
            "shared_execution_lock_owner_uid",
        ),
        "shared_execution_lock_group_gid": _integer(
            receipt.get("shared_execution_lock_group_gid"),
            "shared_execution_lock_group_gid",
        ),
        "shared_execution_lock_mode_octal": receipt.get(
            "shared_execution_lock_mode_octal"
        ),
    }
    if (
        expected["shared_execution_lock_protocol"] != SHARED_LOCK_PROTOCOL
        or expected["shared_execution_lock_path"] != str(CANONICAL_SHARED_LOCK)
    ):
        raise HorizonRefusal(
            "shared_lock_contract_invalid",
            "root receipt binds another signer-lock protocol/path",
        )
    if _shared_lock_identity(
        require_root_owned=require_root_owned
    ) != expected:
        raise HorizonRefusal(
            "shared_lock_replaced",
            "persistent signer-lock inode differs from the sealed root receipt",
        )


def _boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise HorizonRefusal("boot_identity_invalid", "kernel boot id is malformed")
    return value


def _require_trusted_directory_chain(
    directory: Path,
    *,
    sticky_group_writable_exception: Path | None = CANONICAL_DATA_DIR,
) -> None:
    current = Path(os.path.abspath(directory))
    exception = (
        None
        if sticky_group_writable_exception is None
        else Path(os.path.abspath(sticky_group_writable_exception))
    )
    while True:
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise HorizonRefusal(
                "directory_ancestry_unreadable",
                f"{current}: {type(exc).__name__}",
            ) from exc
        exact_data_root_exception = (
            exception is not None
            and current == exception
            and exception == CANONICAL_DATA_DIR
            and metadata.st_uid == 0
            and stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o1770
        )
        if exact_data_root_exception:
            import grp

            try:
                exact_data_root_exception = (
                    metadata.st_gid
                    == grp.getgrnam(SHARED_LOCK_GROUP_NAME).gr_gid
                )
            except KeyError:
                exact_data_root_exception = False
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or (metadata.st_mode & 0o022 and not exact_data_root_exception)
        ):
            raise HorizonRefusal(
                "directory_ancestry_untrusted",
                f"{current} is not a trusted root-owned directory",
            )
        if current.parent == current:
            return
        current = current.parent


def _require_trusted_executable(path: Path) -> None:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise HorizonRefusal("executable_identity_invalid", f"{path} is not trusted")
    _require_trusted_directory_chain(path.parent)


def _validate_production_surface(
    *,
    files: ContractFiles,
    root: Path,
    require_runtime: bool,
    require_systemctl: bool,
) -> None:
    for contract_path in (
        files.authorization,
        files.schedule,
        files.manifest,
        files.horizon_receipt,
    ):
        _require_trusted_directory_chain(contract_path.parent)
    _require_trusted_directory_chain(
        root,
        sticky_group_writable_exception=CANONICAL_DATA_DIR,
    )
    if require_systemctl:
        _require_trusted_executable(CANONICAL_SYSTEMCTL)
    if require_runtime:
        source = Path(__file__).resolve()
        if source != CANONICAL_RUNTIME_SOURCE:
            raise HorizonRefusal(
                "runtime_source_path_invalid",
                "production v275 source is not the canonical snapshot path",
            )
        metadata = os.lstat(source)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
        ):
            raise HorizonRefusal(
                "runtime_source_identity_invalid",
                "v275 runtime source is not immutable/root-owned",
            )
        _require_trusted_directory_chain(source.parent)


def _validate_process_environment(*, credentials_permitted: bool) -> None:
    for key, expected in CONTRACT_ENVIRONMENT.items():
        if os.environ.get(key) != expected:
            raise HorizonRefusal(
                "contract_environment_invalid",
                f"{key} does not bind the canonical contract path",
            )
    if not credentials_permitted and any(key in os.environ for key in SECRET_ENVIRONMENT_KEYS):
        raise HorizonRefusal(
            "credential_isolation_failed",
            "credentialless process received a live credential environment key",
        )
    if any(key in os.environ for key in DANGEROUS_LOADER_ENVIRONMENT_KEYS):
        raise HorizonRefusal(
            "loader_environment_invalid",
            "process received a forbidden Python/dynamic-loader environment key",
        )


def _validate_installed_release_contract(authorization: dict[str, Any]) -> None:
    contract = authorization["release_contract"]
    environment_raw, environment_sha = _read_secure_bytes(
        CANONICAL_NONSECRET_ENVIRONMENT,
        require_root_owned=True,
        require_mode_0600=True,
    )
    if (
        environment_raw != NONSECRET_ENVIRONMENT_BYTES
        or environment_sha != contract["nonsecret_environment_sha256"]
    ):
        raise HorizonRefusal(
            "nonsecret_environment_invalid",
            "nonsecret EnvironmentFile is not the exact four-key root 0600 contract",
        )
    for field, filename in UNIT_SOURCE_NAMES.items():
        installed = CANONICAL_UNIT_DIRECTORY / filename
        _raw, installed_sha = _read_secure_bytes(
            installed,
            require_root_owned=True,
        )
        if installed_sha != contract[field]:
            raise HorizonRefusal(
                "installed_unit_release_mismatch",
                f"installed unit differs from authorized bytes: {filename}",
            )


def _expected_effective_systemd_units() -> dict[str, dict[str, Any]]:
    python = str(CANONICAL_RUNTIME_ROOT / "venv" / "bin" / "python")
    source = str(CANONICAL_RUNTIME_SOURCE)
    contract_paths = {
        str(CANONICAL_NONSECRET_ENVIRONMENT),
        str(CANONICAL_V274_PROVENANCE_AUTHORIZATION),
        str(CANONICAL_PUBLIC_IDENTITY_DIRECTORY),
        str(CANONICAL_PUBLIC_IDENTITY),
        str(CANONICAL_AUTHORIZATION),
        str(CANONICAL_SCHEDULE),
        str(CANONICAL_MANIFEST),
        str(CANONICAL_HORIZON_RECEIPT),
        str(CANONICAL_RUNTIME_ROOT),
    }
    qualification = str(CANONICAL_QUALIFICATION_ROOT)
    common_service = {
        "environment": {"PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring"},
        "user": "root",
        "group": "root",
        "type": "oneshot",
        "no_new_privileges": "yes",
        "capability_bounding_set": "",
        "protect_system": "strict",
        "protect_home": "yes",
        "private_devices": "yes",
        "private_tmp": "yes",
        "umask": "0077",
    }
    credentialless_unset = set(DANGEROUS_LOADER_ENVIRONMENT_KEYS) | set(
        SECRET_ENVIRONMENT_KEYS
    )
    execute_unset = set(DANGEROUS_LOADER_ENVIRONMENT_KEYS)
    return {
        "poly-bot-bounded-horizon-canary-v275-provision-identity.service": {
            **common_service,
            "command": [python, source, "provision-public-identity-from-v274"],
            "environment_files": [],
            "unset_environment": credentialless_unset,
            "private_network": "yes",
            "restart": "no",
            "timeout_start_usec": 60_000_000,
            "runtime_max_usec": "infinity",
            "inaccessible_paths": {
                "/etc/poly-bot-btc5m/live.env",
                "/opt/poly-bot-btc5m",
            },
            "read_only_paths": {
                str(CANONICAL_V274_PROVENANCE_AUTHORIZATION),
                str(CANONICAL_RUNTIME_ROOT),
            },
            "read_write_paths": {str(CANONICAL_PUBLIC_IDENTITY.parent)},
            "address_families": {"AF_UNIX"},
        },
        "poly-bot-bounded-horizon-canary-v275-coordinator.service": {
            **common_service,
            "command": [python, source, "coordinate"],
            "environment_files": [str(CANONICAL_NONSECRET_ENVIRONMENT)],
            "unset_environment": credentialless_unset,
            "private_network": "yes",
            "restart": "on-failure",
            "restart_usec": 10_000_000,
            "start_limit_interval_usec": 120_000_000,
            "start_limit_burst": "3",
            "timeout_start_usec": 44_100_000_000,
            "runtime_max_usec": 44_100_000_000,
            "inaccessible_paths": {
                "/etc/poly-bot-btc5m/live.env",
                "/opt/poly-bot-btc5m",
            },
            "read_only_paths": contract_paths,
            "read_write_paths": {str(CANONICAL_ROOT)},
            "address_families": {"AF_UNIX"},
        },
        "poly-bot-bounded-horizon-canary-v275-prepare.service": {
            **common_service,
            "command": [python, source, "prepare-next"],
            "environment_files": [str(CANONICAL_NONSECRET_ENVIRONMENT)],
            "unset_environment": credentialless_unset,
            "private_network": "no",
            "restart": "no",
            "timeout_start_usec": 480_000_000,
            "runtime_max_usec": "infinity",
            "inaccessible_paths": {
                "/etc/poly-bot-btc5m/live.env",
                "/opt/poly-bot-btc5m",
            },
            "read_only_paths": contract_paths | {qualification},
            "read_write_paths": {str(CANONICAL_ROOT)},
            "address_families": {"AF_INET", "AF_INET6", "AF_UNIX"},
        },
        "poly-bot-bounded-horizon-canary-v275-execute.service": {
            **common_service,
            "command": [python, source, "execute-first-eligible"],
            "environment_files": [
                "/etc/poly-bot-btc5m/live.env",
                str(CANONICAL_NONSECRET_ENVIRONMENT),
            ],
            "unset_environment": execute_unset,
            "private_network": "no",
            "restart": "no",
            "timeout_start_usec": 90_000_000,
            "runtime_max_usec": "infinity",
            "inaccessible_paths": {"/opt/poly-bot-btc5m"},
            "read_only_paths": contract_paths
            | {qualification, "/etc/poly-bot-btc5m/live.env"},
            "read_write_paths": {
                str(CANONICAL_ROOT),
                str(CANONICAL_SHARED_LOCK),
            },
            "address_families": {"AF_INET", "AF_INET6", "AF_UNIX"},
        },
    }


def _systemd_path_tokens(value: str) -> list[str]:
    paths: list[str] = []
    for raw in value.split():
        token = raw.lstrip("-+!")
        if token.startswith("/"):
            paths.append(token)
    return paths


def _systemd_timespan_microseconds(value: str) -> int | str:
    """Normalize systemctl's canonical human timespan to microseconds."""

    if value == "infinity":
        return value
    multipliers = {
        "us": 1,
        "ms": 1_000,
        "s": 1_000_000,
        "min": 60_000_000,
        "h": 3_600_000_000,
        "d": 86_400_000_000,
        "w": 604_800_000_000,
    }
    total = 0
    tokens = value.split()
    if not tokens:
        raise HorizonRefusal(
            "systemd_timespan_invalid",
            "effective systemd timespan is empty",
        )
    for token in tokens:
        match = re.fullmatch(r"([0-9]+)(us|ms|s|min|h|d|w)", token)
        if match is None:
            raise HorizonRefusal(
                "systemd_timespan_invalid",
                f"unsupported effective systemd timespan token: {token}",
            )
        total += int(match.group(1)) * multipliers[match.group(2)]
    return total


def effective_systemd_contract(
    *,
    systemctl_path: Path = CANONICAL_SYSTEMCTL,
) -> dict[str, Any]:
    """Normalize and validate the manager-loaded unit boundary, not just files."""

    if systemctl_path != CANONICAL_SYSTEMCTL:
        raise HorizonRefusal(
            "systemctl_path_invalid",
            "effective unit verification requires canonical systemctl",
        )
    expected_units = _expected_effective_systemd_units()
    normalized: dict[str, Any] = {}
    for unit, expected in expected_units.items():
        command = [
            str(systemctl_path),
            "show",
            unit,
            "--no-pager",
            *(f"--property={name}" for name in EFFECTIVE_SYSTEMD_PROPERTIES),
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
        except Exception as exc:
            raise HorizonRefusal(
                "systemd_effective_query_failed",
                f"{unit}: {type(exc).__name__}",
            ) from exc
        if result.returncode != 0:
            raise HorizonRefusal(
                "systemd_effective_query_failed",
                f"{unit}: rc={result.returncode}; stderr_sha256="
                f"{hashlib.sha256(result.stderr).hexdigest()}",
            )
        try:
            lines = result.stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise HorizonRefusal(
                "systemd_effective_output_invalid",
                f"{unit}: non-UTF8 systemctl output",
            ) from exc
        properties: dict[str, str] = {}
        for line in lines:
            if "=" not in line:
                raise HorizonRefusal(
                    "systemd_effective_output_invalid",
                    f"{unit}: malformed systemctl property line",
                )
            key, value = line.split("=", 1)
            if key in properties:
                raise HorizonRefusal(
                    "systemd_effective_output_invalid",
                    f"{unit}: duplicate systemctl property {key}",
                )
            properties[key] = value
        expected_property_names = set(EFFECTIVE_SYSTEMD_PROPERTIES)
        missing_property_names = expected_property_names - set(properties)
        # Some systemd builds omit EnvironmentFiles entirely when the loaded
        # unit has no EnvironmentFile entries.  That single representation is
        # equivalent to an explicitly empty EnvironmentFiles= value; every
        # other missing or unexpected property remains a refusal.
        if missing_property_names == {"EnvironmentFiles"}:
            properties["EnvironmentFiles"] = ""
        if set(properties) != expected_property_names:
            raise HorizonRefusal(
                "systemd_effective_output_invalid",
                f"{unit}: incomplete effective property set",
            )
        if (
            expected["environment_files"] == []
            and properties["EnvironmentFiles"] != ""
        ):
            raise HorizonRefusal(
                "systemd_effective_contract_mismatch",
                f"{unit}: empty EnvironmentFiles contract has nonempty raw value",
            )
        expected_fragment = str(CANONICAL_UNIT_DIRECTORY / unit)
        exec_start = properties["ExecStart"]
        path_match = re.search(r"(?:^|[ {;])path=([^ ;}]+)", exec_start)
        argv_match = re.search(r"(?:^|[ {;])argv\[\]=([^;}]+)", exec_start)
        actual_command = (
            argv_match.group(1).strip().split()
            if argv_match is not None
            else []
        )
        stable = {
            "load_state": properties["LoadState"],
            "unit_file_state": properties["UnitFileState"],
            "fragment_path": properties["FragmentPath"],
            "drop_in_paths": properties["DropInPaths"],
            "exec_path": path_match.group(1) if path_match is not None else None,
            "command": actual_command,
            "environment_files": _systemd_path_tokens(
                properties["EnvironmentFiles"]
            ),
            "environment": sorted(properties["Environment"].split()),
            "unset_environment": sorted(
                properties["UnsetEnvironment"].split()
            ),
            "user": properties["User"],
            "group": properties["Group"],
            "type": properties["Type"],
            "private_network": properties["PrivateNetwork"],
            "restart": properties["Restart"],
            "no_new_privileges": properties["NoNewPrivileges"],
            "capability_bounding_set": properties[
                "CapabilityBoundingSet"
            ],
            "protect_system": properties["ProtectSystem"],
            "protect_home": properties["ProtectHome"],
            "private_devices": properties["PrivateDevices"],
            "private_tmp": properties["PrivateTmp"],
            "umask": properties["UMask"],
            "timeout_start_usec": _systemd_timespan_microseconds(
                properties["TimeoutStartUSec"]
            ),
            "runtime_max_usec": _systemd_timespan_microseconds(
                properties["RuntimeMaxUSec"]
            ),
            "inaccessible_paths": sorted(
                _systemd_path_tokens(properties["InaccessiblePaths"])
            ),
            "read_only_paths": sorted(
                _systemd_path_tokens(properties["ReadOnlyPaths"])
            ),
            "read_write_paths": sorted(
                _systemd_path_tokens(properties["ReadWritePaths"])
            ),
            "address_families": sorted(
                token
                for token in properties["RestrictAddressFamilies"].split()
                if token
            ),
            "triggered_by": properties["TriggeredBy"],
            "triggers": properties["Triggers"],
            "wanted_by": properties["WantedBy"],
            "required_by": properties["RequiredBy"],
            "upheld_by": properties["UpheldBy"],
            "bound_by": properties["BoundBy"],
            "on_failure_of": properties["OnFailureOf"],
        }
        expected_stable = {
            "load_state": "loaded",
            "unit_file_state": "static",
            "fragment_path": expected_fragment,
            "drop_in_paths": "",
            "exec_path": expected["command"][0],
            "command": expected["command"],
            "environment_files": expected["environment_files"],
            "environment": sorted(expected["environment"]),
            "unset_environment": sorted(expected["unset_environment"]),
            "user": expected["user"],
            "group": expected["group"],
            "type": expected["type"],
            "private_network": expected["private_network"],
            "restart": expected["restart"],
            "no_new_privileges": expected["no_new_privileges"],
            "capability_bounding_set": expected[
                "capability_bounding_set"
            ],
            "protect_system": expected["protect_system"],
            "protect_home": expected["protect_home"],
            "private_devices": expected["private_devices"],
            "private_tmp": expected["private_tmp"],
            "umask": expected["umask"],
            "timeout_start_usec": expected["timeout_start_usec"],
            "runtime_max_usec": expected["runtime_max_usec"],
            "inaccessible_paths": sorted(expected["inaccessible_paths"]),
            "read_only_paths": sorted(expected["read_only_paths"]),
            "read_write_paths": sorted(expected["read_write_paths"]),
            "address_families": sorted(expected["address_families"]),
            "triggered_by": "",
            "triggers": "",
            "wanted_by": "",
            "required_by": "",
            "upheld_by": "",
            "bound_by": "",
            "on_failure_of": "",
        }
        if unit == "poly-bot-bounded-horizon-canary-v275-coordinator.service":
            stable.update(
                {
                    "restart_usec": _systemd_timespan_microseconds(
                        properties["RestartUSec"]
                    ),
                    "start_limit_interval_usec": (
                        _systemd_timespan_microseconds(
                            properties["StartLimitIntervalUSec"]
                        )
                    ),
                    "start_limit_burst": properties["StartLimitBurst"],
                }
            )
            expected_stable.update(
                {
                    "restart_usec": expected["restart_usec"],
                    "start_limit_interval_usec": expected[
                        "start_limit_interval_usec"
                    ],
                    "start_limit_burst": expected["start_limit_burst"],
                }
            )
        if stable != expected_stable:
            raise HorizonRefusal(
                "systemd_effective_contract_mismatch",
                f"manager-loaded boundary drifted for {unit}",
            )
        normalized[unit] = stable
    return {
        "schema": "btc5m-v275-effective-systemd-boundary-v1",
        "units": normalized,
        "matching_timer_or_trigger_permitted": False,
        "drop_in_permitted": False,
    }


def horizon_paths(root: Path, authorization_sha256: str) -> HorizonPaths:
    run_dir = Path(os.path.abspath(root)) / _hash(authorization_sha256, "authorization_sha256")
    return HorizonPaths(
        run_dir=run_dir,
        claim=run_dir / "horizon_claim.json",
        delegation=run_dir / "first_eligible_delegation.json",
        execute_invocation=run_dir / "execute_invocation.json",
        attempt=run_dir / "global_attempt.json",
        transport=run_dir / "global_transport.json",
        terminal=run_dir / "terminal.json",
    )


def window_paths(paths: HorizonPaths, index: int, start: int) -> WindowPaths:
    index = _integer(index, "window_index")
    start = _integer(start, "window_start")
    run_dir = paths.run_dir / "windows" / f"{index:03d}-{start}"
    return WindowPaths(
        run_dir=run_dir,
        request=run_dir / "strict_next_request.json",
        child_authorization=run_dir / "derived_child_authorization.json",
        v274_projection_receipt=run_dir / "v274_verifier_projection_receipt.json",
        qualification_anchor=run_dir / "qualification_anchor.json",
        live_definition=run_dir / "live_signal_definition.json",
        v271_definition=run_dir / "experiment_definition.json",
        panel_ledger=run_dir / "panel_ledger.jsonl",
        live_signal_row=run_dir / "live_signal_row.json",
        candidate=run_dir / "candidate.json",
        permit=run_dir / "disabled_execution_permit.json",
        completion=run_dir / "v274_completion.json",
        intent=run_dir / "execution_intent.json",
        transport_started=run_dir / "transport_started.json",
        submission_unknown=run_dir / "submission_unknown.json",
        execution_result=run_dir / "execution_result.json",
        window_receipt=run_dir / "window_terminal_receipt.json",
        execute_wrapper_result=run_dir / "execute_wrapper_result.json",
    )


def _ensure_exact_directory(
    directory: Path,
    *,
    require_root_owned: bool,
) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise HorizonRefusal(
            "state_directory_unusable",
            f"{directory}: {type(exc).__name__}",
        ) from exc
    metadata = os.lstat(directory)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o022
        or (require_root_owned and metadata.st_uid != 0)
    ):
        raise HorizonRefusal(
            "state_directory_identity_invalid",
            f"{directory} is not the exact trusted state directory",
        )
    if require_root_owned:
        _require_trusted_directory_chain(directory)


def build_schedule(horizon_start_epoch: int) -> dict[str, Any]:
    start = _integer(horizon_start_epoch, "horizon_start_epoch")
    if start % WINDOW_SECONDS:
        raise HorizonRefusal("horizon_alignment_invalid", "start is not 5-minute aligned")
    windows = [
        {
            "window_index": index,
            "exact_window_start_epoch": start + index * WINDOW_SECONDS,
            "exact_window_end_epoch": start + (index + 1) * WINDOW_SECONDS,
        }
        for index in range(HORIZON_WINDOWS)
    ]
    return {
        "schema": SCHEDULE_SCHEMA,
        "strategy_id": STRATEGY_ID,
        "horizon_start_epoch": start,
        "horizon_end_epoch": start + HORIZON_SECONDS,
        "horizon_seconds": HORIZON_SECONDS,
        "window_seconds": WINDOW_SECONDS,
        "horizon_windows": HORIZON_WINDOWS,
        "windows": windows,
        "extension_permitted": False,
        "window_145_permitted": False,
    }


def validate_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    start = _integer(schedule.get("horizon_start_epoch"), "horizon_start_epoch")
    expected = build_schedule(start)
    if schedule != expected:
        raise HorizonRefusal("schedule_invalid", "schedule is not exact deterministic 144-window sequence")
    return schedule


AUTHORIZATION_FIELDS = frozenset(
    {
        "schema", "authorization_state", "authorization_acknowledgement",
        "strategy_id", "schedule_sha256", "horizon_start_epoch",
        "horizon_end_epoch", "horizon_seconds", "window_seconds",
        "horizon_windows", "authorized_at_epoch_millis", "expires_at_epoch_millis",
        "coordinator_launch_not_before_epoch_millis",
        "coordinator_launch_not_after_epoch_millis",
        "execute_at_most_once", "maximum_post_attempts", "maximum_execute_unit_invocations",
        "cash_windows_continue", "stop_after_first_eligible", "transport_retry_enabled",
        "extension_permitted", "window_145_permitted", "side", "order_type",
        "exact_shares", "maximum_total_reserve_usdc", "entry_stress_cents",
        "execution_limit_price_cap", "fee_rate", "expected_signer", "expected_funder",
        "signature_type", "public_execution_identity",
        "public_execution_identity_artifact_sha256", "v275_release_sha256",
        "v274_release_sha256",
        "v271_collector_release_sha256", "qualification_known_contract_sha256",
        "release_contract", "release_contract_sha256",
        "qualification_manifest_sha256", "qualification_report_sha256",
        "qualification_report_artifact_sha256", "qualification_paper_ledger_sha256",
        "qualification_panel_ledger_sha256", "qualification_forward_release_sha256",
        "qualification_collector_release_sha256",
    }
)


def known_qualification_sha256() -> str:
    return canonical_sha256(KNOWN_QUALIFICATION_CONTRACT)


def build_public_execution_identity(
    *,
    expected_signer: str,
    expected_funder: str,
    signature_type: int,
) -> dict[str, Any]:
    """Project the accepted sealed v274 account tuple into a public v275 file."""

    signer = _address(expected_signer, "expected_signer")
    funder = _address(expected_funder, "expected_funder")
    signature = _integer(signature_type, "signature_type")
    if signature not in {0, 1, 2, 3}:
        raise HorizonRefusal(
            "signature_type_invalid",
            "unsupported signature type",
        )
    return {
        "schema": PUBLIC_IDENTITY_SCHEMA,
        "provenance_authorization_path": str(
            CANONICAL_V274_PROVENANCE_AUTHORIZATION
        ),
        "provenance_authorization_schema": (
            V274_PROVENANCE_AUTHORIZATION_SCHEMA
        ),
        "provenance_authorization_sha256": (
            V274_PROVENANCE_AUTHORIZATION_SHA256
        ),
        "expected_signer": signer,
        "expected_funder": funder,
        "signature_type": signature,
    }


def validate_public_execution_identity(
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "provenance_authorization_path",
        "provenance_authorization_schema",
        "provenance_authorization_sha256",
        "expected_signer",
        "expected_funder",
        "signature_type",
    }
    if not isinstance(identity, Mapping) or set(identity) != expected_keys:
        raise HorizonRefusal(
            "public_execution_identity_fields_invalid",
            "public execution identity fields drifted",
        )
    expected = build_public_execution_identity(
        expected_signer=_address(
            identity.get("expected_signer"),
            "public_identity.expected_signer",
        ),
        expected_funder=_address(
            identity.get("expected_funder"),
            "public_identity.expected_funder",
        ),
        signature_type=_integer(
            identity.get("signature_type"),
            "public_identity.signature_type",
        ),
    )
    if dict(identity) != expected:
        raise HorizonRefusal(
            "public_execution_identity_invalid",
            "public execution identity/provenance literals drifted",
        )
    return expected


def build_authorization(
    *,
    schedule: dict[str, Any],
    authorized_at_epoch_millis: int,
    expected_signer: str,
    expected_funder: str,
    signature_type: int,
    public_execution_identity_artifact_sha256: str,
    qualification_manifest_sha256: str,
    v275_release_sha256: str | None = None,
) -> dict[str, Any]:
    """Build exact parent bytes after the user selected the bounded schedule."""
    validate_schedule(schedule)
    start = schedule["horizon_start_epoch"]
    signer = _address(expected_signer, "expected_signer")
    funder = _address(expected_funder, "expected_funder")
    signature = _integer(signature_type, "signature_type")
    public_identity = build_public_execution_identity(
        expected_signer=signer,
        expected_funder=funder,
        signature_type=signature,
    )
    public_identity_artifact_sha = _hash(
        public_execution_identity_artifact_sha256,
        "public_execution_identity_artifact_sha256",
    )
    if hashlib.sha256(
        canonical_bytes(public_identity, newline=True)
    ).hexdigest() != public_identity_artifact_sha:
        raise HorizonRefusal(
            "public_execution_identity_artifact_mismatch",
            "authorization account tuple differs from the sealed public identity bytes",
        )
    payload = {
        "schema": AUTHORIZATION_SCHEMA,
        "authorization_state": "explicit_user_bounded_horizon_preauthorized",
        "authorization_acknowledgement": AUTHORIZATION_ACK,
        "strategy_id": STRATEGY_ID,
        "schedule_sha256": canonical_sha256(schedule),
        "horizon_start_epoch": start,
        "horizon_end_epoch": start + HORIZON_SECONDS,
        "horizon_seconds": HORIZON_SECONDS,
        "window_seconds": WINDOW_SECONDS,
        "horizon_windows": HORIZON_WINDOWS,
        "authorized_at_epoch_millis": _integer(
            authorized_at_epoch_millis,
            "authorized_at_epoch_millis",
        ),
        "expires_at_epoch_millis": (start + HORIZON_SECONDS) * 1000,
        "coordinator_launch_not_before_epoch_millis": (
            start * 1000 - COORDINATOR_LAUNCH_LEAD_MILLISECONDS
        ),
        "coordinator_launch_not_after_epoch_millis": start * 1000 - 1,
        "execute_at_most_once": True,
        "maximum_post_attempts": 1,
        "maximum_execute_unit_invocations": 1,
        "cash_windows_continue": True,
        "stop_after_first_eligible": True,
        "transport_retry_enabled": False,
        "extension_permitted": False,
        "window_145_permitted": False,
        "side": "BUY",
        "order_type": "FOK",
        "exact_shares": EXACT_SHARES,
        "maximum_total_reserve_usdc": MAXIMUM_TOTAL_RESERVE_USDC,
        "entry_stress_cents": ENTRY_STRESS_CENTS,
        "execution_limit_price_cap": EXECUTION_LIMIT_PRICE_CAP,
        "fee_rate": FEE_RATE,
        "expected_signer": signer,
        "expected_funder": funder,
        "signature_type": signature,
        "public_execution_identity": public_identity,
        "public_execution_identity_artifact_sha256": (
            public_identity_artifact_sha
        ),
        "v275_release_sha256": v275_release_sha256 or release_sha256(),
        "v274_release_sha256": V274_SOURCE_SHA256,
        "v271_collector_release_sha256": V271_COLLECTOR_SHA256,
        "qualification_known_contract_sha256": known_qualification_sha256(),
        "qualification_manifest_sha256": _hash(
            qualification_manifest_sha256,
            "qualification_manifest_sha256",
        ),
        "qualification_report_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_report_sha256"],
        "qualification_report_artifact_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_report_artifact_sha256"],
        "qualification_paper_ledger_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_paper_ledger_sha256"],
        "qualification_panel_ledger_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_panel_ledger_sha256"],
        "qualification_forward_release_sha256": V271_FORWARD_SHA256,
        "qualification_collector_release_sha256": V271_COLLECTOR_SHA256,
    }
    payload["release_contract"] = release_contract(
        v275_release_sha256=payload["v275_release_sha256"]
    )
    payload["release_contract_sha256"] = canonical_sha256(
        payload["release_contract"]
    )
    validate_authorization(
        payload,
        schedule=schedule,
        expected_release_sha256=payload["v275_release_sha256"],
    )
    return payload


def validate_authorization(
    authorization: dict[str, Any],
    *,
    schedule: dict[str, Any],
    expected_release_sha256: str,
) -> dict[str, Any]:
    if set(authorization) != AUTHORIZATION_FIELDS:
        raise HorizonRefusal("authorization_fields_invalid", "authorization fields drifted")
    validate_schedule(schedule)
    schedule_sha = canonical_sha256(schedule)
    start = schedule["horizon_start_epoch"]
    fixed = {
        "schema": AUTHORIZATION_SCHEMA,
        "authorization_state": "explicit_user_bounded_horizon_preauthorized",
        "authorization_acknowledgement": AUTHORIZATION_ACK,
        "strategy_id": STRATEGY_ID,
        "schedule_sha256": schedule_sha,
        "horizon_start_epoch": start,
        "horizon_end_epoch": start + HORIZON_SECONDS,
        "horizon_seconds": HORIZON_SECONDS,
        "window_seconds": WINDOW_SECONDS,
        "horizon_windows": HORIZON_WINDOWS,
        "expires_at_epoch_millis": (start + HORIZON_SECONDS) * 1000,
        "coordinator_launch_not_before_epoch_millis": (
            start * 1000 - COORDINATOR_LAUNCH_LEAD_MILLISECONDS
        ),
        "coordinator_launch_not_after_epoch_millis": start * 1000 - 1,
        "execute_at_most_once": True,
        "maximum_post_attempts": 1,
        "maximum_execute_unit_invocations": 1,
        "cash_windows_continue": True,
        "stop_after_first_eligible": True,
        "transport_retry_enabled": False,
        "extension_permitted": False,
        "window_145_permitted": False,
        "side": "BUY",
        "order_type": "FOK",
        "exact_shares": EXACT_SHARES,
        "maximum_total_reserve_usdc": MAXIMUM_TOTAL_RESERVE_USDC,
        "entry_stress_cents": ENTRY_STRESS_CENTS,
        "execution_limit_price_cap": EXECUTION_LIMIT_PRICE_CAP,
        "fee_rate": FEE_RATE,
        "v275_release_sha256": expected_release_sha256,
        "v274_release_sha256": V274_SOURCE_SHA256,
        "v271_collector_release_sha256": V271_COLLECTOR_SHA256,
        "qualification_known_contract_sha256": known_qualification_sha256(),
        "qualification_report_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_report_sha256"],
        "qualification_report_artifact_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_report_artifact_sha256"],
        "qualification_paper_ledger_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_paper_ledger_sha256"],
        "qualification_panel_ledger_sha256": KNOWN_QUALIFICATION_CONTRACT["qualification_panel_ledger_sha256"],
        "qualification_forward_release_sha256": V271_FORWARD_SHA256,
        "qualification_collector_release_sha256": V271_COLLECTOR_SHA256,
        "release_contract": release_contract(
            v275_release_sha256=expected_release_sha256
        ),
    }
    fixed["release_contract_sha256"] = canonical_sha256(
        fixed["release_contract"]
    )
    for field, expected in fixed.items():
        if authorization.get(field) != expected:
            raise HorizonRefusal("authorization_literal_mismatch", f"{field} drifted")
    authorized_at = _integer(authorization.get("authorized_at_epoch_millis"), "authorized_at")
    if authorized_at >= start * 1000:
        raise HorizonRefusal("authorization_too_late", "parent authorization must be sealed pre-start")
    _hash(authorization.get("qualification_manifest_sha256"), "qualification_manifest_sha256")
    signer = _address(authorization.get("expected_signer"), "expected_signer")
    funder = _address(authorization.get("expected_funder"), "expected_funder")
    signature_type = _integer(authorization.get("signature_type"), "signature_type")
    expected_public_identity = build_public_execution_identity(
        expected_signer=signer,
        expected_funder=funder,
        signature_type=signature_type,
    )
    public_identity = validate_public_execution_identity(
        authorization.get("public_execution_identity")
    )
    public_identity_artifact_sha = _hash(
        authorization.get("public_execution_identity_artifact_sha256"),
        "public_execution_identity_artifact_sha256",
    )
    if (
        public_identity != expected_public_identity
        or hashlib.sha256(
            canonical_bytes(public_identity, newline=True)
        ).hexdigest()
        != public_identity_artifact_sha
    ):
        raise HorizonRefusal(
            "public_execution_identity_binding_mismatch",
            "parent account identity differs from its sealed public identity",
        )
    return authorization


def child_projection_core(
    authorization: dict[str, Any],
    authorization_sha256: str,
    schedule: dict[str, Any],
    index: int,
    previous_commitment_sha256: str | None,
) -> dict[str, Any]:
    validate_authorization(
        authorization,
        schedule=schedule,
        expected_release_sha256=authorization["v275_release_sha256"],
    )
    index = _integer(index, "window_index")
    if index >= HORIZON_WINDOWS:
        raise HorizonRefusal("window_index_invalid", "no window 145 or extension")
    if index == 0:
        if previous_commitment_sha256 is not None:
            raise HorizonRefusal("child_chain_invalid", "first child must have null predecessor")
    else:
        _hash(previous_commitment_sha256, "previous_child_commitment_sha256")
    item = schedule["windows"][index]
    start = item["exact_window_start_epoch"]
    return {
        "schema": "btc5m-v275-derived-window-commitment-core-v1",
        "authorization_state": "derived_from_explicit_bounded_horizon",
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": authorization["schedule_sha256"],
        "release_contract_sha256": authorization["release_contract_sha256"],
        "window_index": index,
        "previous_child_commitment_sha256": previous_commitment_sha256,
        "exact_window_start_epoch": start,
        "exact_window_end_epoch": start + WINDOW_SECONDS,
        "expires_at_epoch_millis": start * 1000 + 253_000,
        "authorized_at_epoch_millis": authorization["authorized_at_epoch_millis"],
        "strategy_id": STRATEGY_ID,
        "side": "BUY",
        "order_type": "FOK",
        "exact_shares": EXACT_SHARES,
        "maximum_total_reserve_usdc": MAXIMUM_TOTAL_RESERVE_USDC,
        "entry_stress_cents": ENTRY_STRESS_CENTS,
        "execution_limit_price_cap": EXECUTION_LIMIT_PRICE_CAP,
        "fee_rate": FEE_RATE,
        "maximum_post_attempts_global": 1,
        "transport_retry_enabled": False,
        "expected_signer": authorization["expected_signer"],
        "expected_funder": authorization["expected_funder"],
        "signature_type": authorization["signature_type"],
        "public_execution_identity_artifact_sha256": authorization[
            "public_execution_identity_artifact_sha256"
        ],
        "v274_release_sha256": V274_SOURCE_SHA256,
        "v271_collector_release_sha256": V271_COLLECTOR_SHA256,
        "qualification_manifest_sha256": authorization["qualification_manifest_sha256"],
        "qualification_known_contract_sha256": authorization["qualification_known_contract_sha256"],
        "qualification_report_sha256": authorization["qualification_report_sha256"],
        "qualification_report_artifact_sha256": authorization["qualification_report_artifact_sha256"],
        "qualification_paper_ledger_sha256": authorization["qualification_paper_ledger_sha256"],
        "qualification_panel_ledger_sha256": authorization["qualification_panel_ledger_sha256"],
        "qualification_forward_release_sha256": authorization["qualification_forward_release_sha256"],
        "qualification_collector_release_sha256": authorization["qualification_collector_release_sha256"],
    }


def build_manifest(
    authorization: dict[str, Any],
    authorization_sha256: str,
    schedule: dict[str, Any],
    *,
    created_at_epoch_millis: int,
) -> dict[str, Any]:
    previous: str | None = None
    children: list[dict[str, Any]] = []
    for index in range(HORIZON_WINDOWS):
        core = child_projection_core(
            authorization, authorization_sha256, schedule, index, previous
        )
        commitment = canonical_sha256(core)
        children.append(
            {
                "window_index": index,
                "exact_window_start_epoch": core["exact_window_start_epoch"],
                "exact_window_end_epoch": core["exact_window_end_epoch"],
                "previous_child_commitment_sha256": previous,
                "child_commitment_sha256": commitment,
            }
        )
        previous = commitment
    return {
        "schema": MANIFEST_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": canonical_sha256(schedule),
        "release_contract_sha256": authorization["release_contract_sha256"],
        "manifest_created_at_epoch_millis": _integer(created_at_epoch_millis, "manifest_created_at"),
        "horizon_windows": HORIZON_WINDOWS,
        "child_hash_semantics": "sha256_of_acyclic_child_projection_core",
        "children": children,
        "final_child_commitment_sha256": previous,
        "extension_permitted": False,
        "window_145_permitted": False,
    }


def validate_manifest(
    manifest: dict[str, Any],
    *,
    authorization: dict[str, Any],
    authorization_sha256: str,
    schedule: dict[str, Any],
) -> dict[str, Any]:
    created = _integer(manifest.get("manifest_created_at_epoch_millis"), "manifest_created_at")
    expected = build_manifest(
        authorization,
        authorization_sha256,
        schedule,
        created_at_epoch_millis=created,
    )
    if manifest != expected:
        raise HorizonRefusal("manifest_invalid", "manifest/144 child commitments drifted")
    if not (
        authorization["authorized_at_epoch_millis"] <= created
        < authorization["horizon_start_epoch"] * 1000
    ):
        raise HorizonRefusal("manifest_clock_invalid", "manifest must be sealed pre-start")
    return manifest


def build_horizon_receipt(
    *,
    authorization_sha256: str,
    schedule_sha256: str,
    manifest_sha256: str,
    release_contract_sha256: str,
    public_execution_identity_artifact_sha256: str,
    shared_execution_lock_device: int,
    shared_execution_lock_inode: int,
    shared_execution_lock_group_gid: int,
    effective_systemd_contract_sha256: str,
    registered_at_epoch_millis: int,
) -> dict[str, Any]:
    return {
        "schema": HORIZON_RECEIPT_SCHEMA,
        "parent_horizon_authorization_sha256": _hash(authorization_sha256, "authorization_sha256"),
        "schedule_sha256": _hash(schedule_sha256, "schedule_sha256"),
        "manifest_sha256": _hash(manifest_sha256, "manifest_sha256"),
        "release_contract_sha256": _hash(
            release_contract_sha256,
            "release_contract_sha256",
        ),
        "public_execution_identity_artifact_sha256": _hash(
            public_execution_identity_artifact_sha256,
            "public_execution_identity_artifact_sha256",
        ),
        "shared_execution_lock_protocol": SHARED_LOCK_PROTOCOL,
        "shared_execution_lock_path": str(CANONICAL_SHARED_LOCK),
        "shared_execution_lock_device": _integer(
            shared_execution_lock_device,
            "shared_execution_lock_device",
        ),
        "shared_execution_lock_inode": _integer(
            shared_execution_lock_inode,
            "shared_execution_lock_inode",
        ),
        "shared_execution_lock_owner_uid": SHARED_LOCK_OWNER_UID,
        "shared_execution_lock_group_gid": _integer(
            shared_execution_lock_group_gid,
            "shared_execution_lock_group_gid",
        ),
        "shared_execution_lock_mode_octal": "0660",
        "effective_systemd_contract_sha256": _hash(
            effective_systemd_contract_sha256,
            "effective_systemd_contract_sha256",
        ),
        "registered_at_epoch_millis": _integer(registered_at_epoch_millis, "registered_at"),
        "initial_next_index": 0,
        "global_attempt_reserved": False,
        "post_attempts": 0,
        "extension_permitted": False,
    }


def _validate_horizon_receipt_clock(
    receipt: Mapping[str, Any],
    *,
    authorization: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    registered_at = _integer(
        receipt.get("registered_at_epoch_millis"),
        "registered_at_epoch_millis",
    )
    if not (
        authorization["authorized_at_epoch_millis"]
        <= registered_at
        < authorization["horizon_start_epoch"] * 1000
        and registered_at >= manifest["manifest_created_at_epoch_millis"]
    ):
        raise HorizonRefusal(
            "horizon_receipt_clock_invalid",
            "root receipt was not causally registered after the manifest and pre-start",
        )


def load_contract(
    files: ContractFiles,
    *,
    require_root_owned: bool,
) -> tuple[dict[str, Any], str, dict[str, Any], str, dict[str, Any], str, dict[str, Any], str]:
    schedule, schedule_artifact_sha = read_canonical_object(
        files.schedule, require_root_owned=require_root_owned
    )
    validate_schedule(schedule)
    authorization, authorization_sha = read_canonical_object(
        files.authorization, require_root_owned=require_root_owned
    )
    validate_authorization(
        authorization,
        schedule=schedule,
        expected_release_sha256=release_sha256(),
    )
    if require_root_owned:
        _validate_installed_public_execution_identity(
            authorization,
            require_root_owned=True,
            require_canonical_path=True,
        )
        _validate_installed_release_contract(authorization)
    if schedule_artifact_sha != hashlib.sha256(canonical_bytes(schedule, newline=True)).hexdigest():
        raise AssertionError("unreachable schedule raw hash mismatch")
    manifest, manifest_sha = read_canonical_object(
        files.manifest, require_root_owned=require_root_owned
    )
    validate_manifest(
        manifest,
        authorization=authorization,
        authorization_sha256=authorization_sha,
        schedule=schedule,
    )
    receipt, receipt_sha = read_canonical_object(
        files.horizon_receipt, require_root_owned=require_root_owned
    )
    expected_receipt = build_horizon_receipt(
        authorization_sha256=authorization_sha,
        schedule_sha256=canonical_sha256(schedule),
        manifest_sha256=manifest_sha,
        release_contract_sha256=authorization["release_contract_sha256"],
        public_execution_identity_artifact_sha256=authorization[
            "public_execution_identity_artifact_sha256"
        ],
        shared_execution_lock_device=_integer(
            receipt.get("shared_execution_lock_device"),
            "shared_execution_lock_device",
        ),
        shared_execution_lock_inode=_integer(
            receipt.get("shared_execution_lock_inode"),
            "shared_execution_lock_inode",
        ),
        shared_execution_lock_group_gid=_integer(
            receipt.get("shared_execution_lock_group_gid"),
            "shared_execution_lock_group_gid",
        ),
        effective_systemd_contract_sha256=_hash(
            receipt.get("effective_systemd_contract_sha256"),
            "effective_systemd_contract_sha256",
        ),
        registered_at_epoch_millis=_integer(receipt.get("registered_at_epoch_millis"), "registered_at"),
    )
    if receipt != expected_receipt:
        raise HorizonRefusal("horizon_receipt_invalid", "root receipt bindings drifted")
    _validate_horizon_receipt_clock(
        receipt,
        authorization=authorization,
        manifest=manifest,
    )
    if require_root_owned:
        _validate_shared_lock_identity(
            receipt,
            require_root_owned=True,
        )
        if canonical_sha256(effective_systemd_contract()) != receipt[
            "effective_systemd_contract_sha256"
        ]:
            raise HorizonRefusal(
                "systemd_effective_contract_replaced",
                "manager-loaded unit boundary differs from the sealed root receipt",
            )
    return (
        authorization, authorization_sha, schedule, canonical_sha256(schedule),
        manifest, manifest_sha, receipt, receipt_sha,
    )


def build_child(
    *,
    authorization: dict[str, Any],
    authorization_sha256: str,
    schedule: dict[str, Any],
    manifest: dict[str, Any],
    manifest_sha256: str,
    horizon_receipt_sha256: str,
    index: int,
) -> dict[str, Any]:
    entry = manifest["children"][index]
    core = child_projection_core(
        authorization,
        authorization_sha256,
        schedule,
        index,
        entry["previous_child_commitment_sha256"],
    )
    if canonical_sha256(core) != entry["child_commitment_sha256"]:
        raise HorizonRefusal("child_commitment_invalid", "manifest child commitment drifted")
    start = core["exact_window_start_epoch"]
    # This is a deterministic projection used only as input to the frozen v274
    # candidate/permit verifier.  It is not a v274 explicit-user authorization.
    v274_projection = {
        "schema": "btc5m-v275-v274-verifier-projection-v1",
        "strategy_id": STRATEGY_ID,
        "qualification_experiment_id": KNOWN_QUALIFICATION_CONTRACT["qualification_experiment_id"],
        "qualification_forward_cutoff": KNOWN_QUALIFICATION_CONTRACT["qualification_forward_cutoff"],
        "qualification_report_sha256": authorization["qualification_report_sha256"],
        "qualification_report_artifact_sha256": authorization["qualification_report_artifact_sha256"],
        "qualification_paper_ledger_sha256": authorization["qualification_paper_ledger_sha256"],
        "qualification_panel_ledger_sha256": authorization["qualification_panel_ledger_sha256"],
        "qualification_forward_release_sha256": authorization["qualification_forward_release_sha256"],
        "qualification_collector_release_sha256": authorization["qualification_collector_release_sha256"],
        "qualification_known_contract_sha256": authorization["qualification_known_contract_sha256"],
        "qualification_manifest_sha256": authorization["qualification_manifest_sha256"],
        "live_panel_validator_release_sha256": V2733_VALIDATOR_SHA256,
        "qualification_horizon_windows": 288,
        "exact_window_start_epoch": start,
        "exact_window_end_epoch": start + WINDOW_SECONDS,
        "expires_at_epoch_millis": start * 1000 + 253_000,
        "authorized_at_epoch_millis": authorization["authorized_at_epoch_millis"],
        "expected_signer": authorization["expected_signer"],
        "expected_funder": authorization["expected_funder"],
        "signature_type": authorization["signature_type"],
        "public_execution_identity_artifact_sha256": authorization[
            "public_execution_identity_artifact_sha256"
        ],
    }
    return {
        "schema": CHILD_SCHEMA,
        "authorization_state": "derived_from_explicit_bounded_horizon",
        "parent_horizon_authorization_sha256": authorization_sha256,
        "horizon_receipt_sha256": horizon_receipt_sha256,
        "manifest_sha256": manifest_sha256,
        "schedule_sha256": authorization["schedule_sha256"],
        "release_contract_sha256": authorization["release_contract_sha256"],
        "public_execution_identity_artifact_sha256": authorization[
            "public_execution_identity_artifact_sha256"
        ],
        "window_index": index,
        "exact_window_start_epoch": start,
        "exact_window_end_epoch": start + WINDOW_SECONDS,
        "previous_child_commitment_sha256": entry["previous_child_commitment_sha256"],
        "child_commitment_sha256": entry["child_commitment_sha256"],
        "child_commitment_core": core,
        "v274_verifier_projection": v274_projection,
        "maximum_post_attempts_global": 1,
        "first_eligible_only": True,
        "cash_windows_continue": True,
        "transport_retry_enabled": False,
    }


def _validate_account_identity_chain(
    *,
    authorization: Mapping[str, Any],
    child: Mapping[str, Any],
    candidate: Mapping[str, Any] | None = None,
    permit: Mapping[str, Any] | None = None,
) -> None:
    """Require one external identity SHA/tuple through child and execution bytes."""

    expected_sha = _hash(
        authorization.get("public_execution_identity_artifact_sha256"),
        "public_execution_identity_artifact_sha256",
    )
    expected_tuple = {
        "expected_signer": authorization.get("expected_signer"),
        "expected_funder": authorization.get("expected_funder"),
        "signature_type": authorization.get("signature_type"),
    }
    child_layers = (
        child,
        child.get("child_commitment_core"),
        child.get("v274_verifier_projection"),
    )
    for label, layer in zip(
        ("child", "child_commitment_core", "v274_verifier_projection"),
        child_layers,
    ):
        if not isinstance(layer, Mapping) or (
            layer.get("public_execution_identity_artifact_sha256")
            != expected_sha
        ):
            raise HorizonRefusal(
                "account_identity_chain_invalid",
                f"{label} binds another public identity artifact",
            )
        if label != "child" and any(
            layer.get(field) != expected
            for field, expected in expected_tuple.items()
        ):
            raise HorizonRefusal(
                "account_identity_chain_invalid",
                f"{label} account tuple drifted",
            )
    for label, artifact in (("candidate", candidate), ("permit", permit)):
        if artifact is None:
            continue
        if (
            artifact.get("public_execution_identity_artifact_sha256")
            != expected_sha
            or any(
                artifact.get(field) != expected
                for field, expected in expected_tuple.items()
            )
        ):
            raise HorizonRefusal(
                "account_identity_chain_invalid",
                f"{label} account identity drifted",
            )


def _load_v274(*, require_root_owned: bool) -> Any:
    expected_path = Path(__file__).resolve().with_name("one_window_canary_v274.py")
    if require_root_owned:
        metadata = os.lstat(expected_path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
        ):
            raise HorizonRefusal("v274_source_identity_invalid", "v274 source is not immutable/root-owned")
        _require_trusted_directory_chain(expected_path.parent)
    if file_sha256(expected_path) != V274_SOURCE_SHA256:
        raise HorizonRefusal("v274_release_mismatch", "frozen v274 source bytes drifted")
    app_dir = str(expected_path.parent)
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    module = importlib.import_module("one_window_canary_v274")
    if Path(module.__file__).resolve() != expected_path or file_sha256(Path(module.__file__)) != V274_SOURCE_SHA256:
        raise HorizonRefusal("v274_import_path_mismatch", "v274 loaded from another path")
    dependency_contract = {
        Path(module.producer.__file__).resolve(): V271_COLLECTOR_SHA256,
        Path(module.panel_v2733.__file__).resolve(): V2733_VALIDATOR_SHA256,
        Path(module.panel_v2733.forward.__file__).resolve(): V271_FORWARD_SHA256,
        Path(module.runtime_v273.__file__).resolve(): V273_RUNTIME_SHA256,
        Path(module.preflight_v273.__file__).resolve(): V273_PREFLIGHT_SHA256,
    }
    for dependency_path, expected_sha256 in dependency_contract.items():
        if dependency_path.parent != expected_path.parent or file_sha256(dependency_path) != expected_sha256:
            raise HorizonRefusal(
                "v274_dependency_release_mismatch",
                f"frozen dependency drifted: {dependency_path.name}",
            )
        if require_root_owned:
            metadata = os.lstat(dependency_path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
            ):
                raise HorizonRefusal(
                    "v274_dependency_identity_invalid",
                    f"frozen dependency is not immutable/root-owned: {dependency_path.name}",
                )
    return module


def _claim_payload(
    *,
    authorization: dict[str, Any], authorization_sha256: str,
    schedule_sha256: str, manifest_sha256: str, horizon_receipt_sha256: str,
    claimed_at_epoch_millis: int, boot_id: str,
) -> dict[str, Any]:
    return {
        "schema": CLAIM_SCHEMA,
        "version": VERSION,
        "v275_release_sha256": release_sha256(),
        "v274_release_sha256": V274_SOURCE_SHA256,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": schedule_sha256,
        "manifest_sha256": manifest_sha256,
        "horizon_receipt_sha256": horizon_receipt_sha256,
        "horizon_start_epoch": authorization["horizon_start_epoch"],
        "horizon_end_epoch": authorization["horizon_end_epoch"],
        "horizon_windows": HORIZON_WINDOWS,
        "initial_next_index": 0,
        "coordinator_launch_not_before_epoch_millis": authorization[
            "coordinator_launch_not_before_epoch_millis"
        ],
        "coordinator_launch_not_after_epoch_millis": authorization[
            "coordinator_launch_not_after_epoch_millis"
        ],
        "maximum_post_attempts": 1,
        "claimed_at_epoch_millis": claimed_at_epoch_millis,
        "boot_id": boot_id,
        "extension_permitted": False,
    }


def _validate_claim(
    claim: dict[str, Any],
    *,
    authorization: dict[str, Any], authorization_sha256: str,
    schedule_sha256: str, manifest_sha256: str, horizon_receipt_sha256: str,
    enforce_current_boot: bool = True,
) -> None:
    claimed_at = _integer(
        claim.get("claimed_at_epoch_millis"),
        "claimed_at",
    )
    expected = _claim_payload(
        authorization=authorization,
        authorization_sha256=authorization_sha256,
        schedule_sha256=schedule_sha256,
        manifest_sha256=manifest_sha256,
        horizon_receipt_sha256=horizon_receipt_sha256,
        claimed_at_epoch_millis=claimed_at,
        boot_id=str(claim.get("boot_id") or ""),
    )
    if claim != expected:
        raise HorizonRefusal("claim_invalid", "horizon claim bindings drifted")
    claim_not_before = max(
        authorization["authorized_at_epoch_millis"],
        authorization["coordinator_launch_not_before_epoch_millis"],
    )
    claim_not_after = authorization[
        "coordinator_launch_not_after_epoch_millis"
    ]
    if not claim_not_before <= claimed_at <= claim_not_after:
        raise HorizonRefusal(
            "claim_clock_invalid",
            "claim timestamp is outside the sealed initial launch band",
        )
    if enforce_current_boot and claim["boot_id"] != _boot_id():
        raise HorizonRefusal("boot_drift_terminal", "horizon cannot resume across boot")


@contextmanager
def _exclusive_claim_lease(
    claim_path: Path,
    *,
    expected_claim_sha256: str,
    require_root_owned: bool,
) -> Any:
    """Serialize every coordinator transition on the immutable claim inode."""

    if require_root_owned:
        _require_trusted_directory_chain(claim_path.parent)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(claim_path, flags)
    except OSError as exc:
        raise HorizonRefusal(
            "claim_lease_unreadable",
            f"claim lease: {type(exc).__name__}",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        path_metadata = os.lstat(claim_path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or (require_root_owned and metadata.st_uid != 0)
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise HorizonRefusal(
                "claim_lease_identity_invalid",
                "claim lease is not the exact immutable claim inode",
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HorizonRefusal(
                "coordinator_claim_busy",
                "another coordinator owns the exact horizon claim",
            ) from exc
        os.lseek(descriptor, 0, os.SEEK_SET)
        blocks: list[bytes] = []
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            blocks.append(block)
        if hashlib.sha256(b"".join(blocks)).hexdigest() != _hash(
            expected_claim_sha256,
            "expected_claim_sha256",
        ):
            raise HorizonRefusal(
                "claim_lease_bytes_invalid",
                "locked claim inode differs from the validated claim bytes",
            )
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _request_payload(
    *,
    authorization_sha256: str, schedule_sha256: str, manifest_sha256: str,
    horizon_receipt_sha256: str, claim_sha256: str, index: int, start: int,
    previous_window_receipt_sha256: str | None, requested_at_epoch_millis: int,
) -> dict[str, Any]:
    return {
        "schema": REQUEST_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": schedule_sha256,
        "manifest_sha256": manifest_sha256,
        "horizon_receipt_sha256": horizon_receipt_sha256,
        "horizon_claim_sha256": claim_sha256,
        "window_index": index,
        "exact_window_start_epoch": start,
        "exact_window_end_epoch": start + WINDOW_SECONDS,
        "previous_window_receipt_sha256": previous_window_receipt_sha256,
        "requested_at_epoch_millis": requested_at_epoch_millis,
        "strict_next_only": True,
        "prepare_invocations_for_window": 1,
    }


def _receipt_common(
    *,
    authorization_sha256: str, schedule_sha256: str, manifest_sha256: str,
    horizon_receipt_sha256: str, request_sha256: str, child_sha256: str,
    child: dict[str, Any], previous_window_receipt_sha256: str | None,
) -> dict[str, Any]:
    return {
        "schema": WINDOW_RECEIPT_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": schedule_sha256,
        "manifest_sha256": manifest_sha256,
        "horizon_receipt_sha256": horizon_receipt_sha256,
        "strict_next_request_sha256": request_sha256,
        "derived_child_authorization_sha256": child_sha256,
        "child_commitment_sha256": child["child_commitment_sha256"],
        "authorization_state": "derived_from_explicit_bounded_horizon",
        "window_index": child["window_index"],
        "exact_window_start_epoch": child["exact_window_start_epoch"],
        "exact_window_end_epoch": child["exact_window_end_epoch"],
        "previous_window_receipt_sha256": previous_window_receipt_sha256,
        "maximum_post_attempts_global": 1,
        "transport_retry_enabled": False,
    }


def _scan_receipts(
    paths: HorizonPaths,
    *,
    authorization: dict[str, Any],
    authorization_sha256: str,
    schedule: dict[str, Any],
    schedule_sha256: str,
    manifest: dict[str, Any],
    manifest_sha256: str,
    horizon_receipt_sha256: str,
    claim_sha256: str,
    require_root_owned: bool,
) -> tuple[int, str | None, dict[str, Any] | None]:
    claim, observed_claim_sha = read_canonical_object(
        paths.claim,
        require_root_owned=require_root_owned,
    )
    if observed_claim_sha != _hash(claim_sha256, "claim_sha256"):
        raise HorizonRefusal(
            "request_claim_identity_invalid",
            "strict-next scan is not bound to the exact durable claim bytes",
        )
    _validate_claim(
        claim,
        authorization=authorization,
        authorization_sha256=authorization_sha256,
        schedule_sha256=schedule_sha256,
        manifest_sha256=manifest_sha256,
        horizon_receipt_sha256=horizon_receipt_sha256,
        enforce_current_boot=False,
    )
    claim_created_at = claim["claimed_at_epoch_millis"]
    previous: str | None = None
    previous_completed_at: int | None = None
    for index, item in enumerate(schedule["windows"]):
        window = window_paths(paths, index, item["exact_window_start_epoch"])
        if not _artifact_present(window.window_receipt):
            later = (
                window_paths(
                    paths,
                    future,
                    schedule["windows"][future]["exact_window_start_epoch"],
                ).run_dir
                for future in range(index + 1, HORIZON_WINDOWS)
            )
            if any(os.path.lexists(path) for path in later):
                raise HorizonRefusal(
                    "future_artifact_gap_terminal",
                    "future window artifacts exist after the strict-next gap",
                )
            return index, previous, None
        receipt, receipt_sha = read_canonical_object(
            window.window_receipt, require_root_owned=require_root_owned
        )
        fixed = {
            "schema": WINDOW_RECEIPT_SCHEMA,
            "parent_horizon_authorization_sha256": authorization_sha256,
            "schedule_sha256": schedule_sha256,
            "manifest_sha256": manifest_sha256,
            "horizon_receipt_sha256": horizon_receipt_sha256,
            "window_index": index,
            "exact_window_start_epoch": item["exact_window_start_epoch"],
            "exact_window_end_epoch": item["exact_window_end_epoch"],
            "previous_window_receipt_sha256": previous,
            "authorization_state": "derived_from_explicit_bounded_horizon",
            "maximum_post_attempts_global": 1,
            "transport_retry_enabled": False,
        }
        if any(receipt.get(key) != value for key, value in fixed.items()):
            raise HorizonRefusal("window_receipt_invalid", f"window {index} receipt drifted")
        request, request_sha = read_canonical_object(
            window.request,
            require_root_owned=require_root_owned,
        )
        child, child_sha = read_canonical_object(
            window.child_authorization,
            require_root_owned=require_root_owned,
        )
        requested_at = _integer(
            request.get("requested_at_epoch_millis"),
            "requested_at_epoch_millis",
        )
        expected_request = _request_payload(
            authorization_sha256=authorization_sha256,
            schedule_sha256=schedule_sha256,
            manifest_sha256=manifest_sha256,
            horizon_receipt_sha256=horizon_receipt_sha256,
            claim_sha256=claim_sha256,
            index=index,
            start=item["exact_window_start_epoch"],
            previous_window_receipt_sha256=previous,
            requested_at_epoch_millis=requested_at,
        )
        expected_child = build_child(
            authorization=authorization,
            authorization_sha256=authorization_sha256,
            schedule=schedule,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            horizon_receipt_sha256=horizon_receipt_sha256,
            index=index,
        )
        if (
            request != expected_request
            or not (
                max(
                    authorization["authorized_at_epoch_millis"],
                    claim_created_at,
                    previous_completed_at or 0,
                )
                <= requested_at
                < item["exact_window_start_epoch"] * 1000
            )
            or child != expected_child
            or receipt.get("strict_next_request_sha256") != request_sha
            or receipt.get("derived_child_authorization_sha256") != child_sha
            or receipt.get("child_commitment_sha256") != child.get("child_commitment_sha256")
            or request.get("window_index") != index
            or child.get("window_index") != index
            or request.get("exact_window_start_epoch") != item["exact_window_start_epoch"]
            or child.get("exact_window_start_epoch") != item["exact_window_start_epoch"]
        ):
            raise HorizonRefusal(
                "window_receipt_source_binding_invalid",
                f"window {index} request/child/receipt binding drifted",
            )
        common_keys = {
            "schema", "parent_horizon_authorization_sha256", "schedule_sha256",
            "manifest_sha256", "horizon_receipt_sha256",
            "strict_next_request_sha256", "derived_child_authorization_sha256",
            "child_commitment_sha256", "authorization_state", "window_index",
            "exact_window_start_epoch", "exact_window_end_epoch",
            "previous_window_receipt_sha256", "maximum_post_attempts_global",
            "transport_retry_enabled",
        }
        completed_at = _integer(
            receipt.get("completed_at_epoch_millis"),
            "completed_at_epoch_millis",
        )
        if not (
            item["exact_window_start_epoch"] * 1000
            <= completed_at
            < item["exact_window_end_epoch"] * 1000
        ):
            raise HorizonRefusal(
                "window_receipt_clock_invalid",
                f"window {index} receipt is outside its exact window",
            )
        if receipt.get("action") == "cash":
            expected_keys = common_keys | {
                "completed", "action", "cash_reason", "qualification_anchor_sha256",
                "live_signal_row_artifact_sha256", "live_panel_record_sha256",
                "completed_at_epoch_millis", "preauthorization_consumed", "signed",
                "post_attempts", "orders",
            }
            cash_fixed = {
                "completed": True, "post_attempts": 0, "orders": "none",
                "preauthorization_consumed": False, "signed": False,
            }
            if (
                set(receipt) != expected_keys
                or any(receipt.get(key) != value for key, value in cash_fixed.items())
                or not isinstance(receipt.get("cash_reason"), str)
                or not HASH.fullmatch(str(receipt.get("qualification_anchor_sha256") or ""))
                or not HASH.fullmatch(str(receipt.get("live_signal_row_artifact_sha256") or ""))
                or _artifact_present(window.candidate)
                or _artifact_present(window.permit)
                or _artifact_present(window.completion)
                or _artifact_present(window.intent)
                or _artifact_present(window.transport_started)
                or _artifact_present(window.submission_unknown)
                or _artifact_present(window.execution_result)
                or _artifact_present(window.execute_wrapper_result)
            ):
                raise HorizonRefusal("cash_receipt_invalid", f"window {index} cash fields drifted")
            anchor, anchor_sha = read_canonical_object(
                window.qualification_anchor,
                require_root_owned=require_root_owned,
            )
            live_signal, live_signal_sha = read_canonical_object(
                window.live_signal_row,
                require_root_owned=require_root_owned,
            )
            projection, _ = read_canonical_object(
                window.v274_projection_receipt,
                require_root_owned=require_root_owned,
            )
            if (
                receipt.get("qualification_anchor_sha256") != anchor_sha
                or receipt.get("live_signal_row_artifact_sha256")
                != live_signal_sha
                or anchor.get("authorization_sha256") != child_sha
                or projection.get("authorization_sha256") != child_sha
                or live_signal.get("panel_record_sha256")
                != receipt.get("live_panel_record_sha256")
            ):
                raise HorizonRefusal(
                    "cash_evidence_binding_invalid",
                    f"window {index} cash evidence bytes drifted",
                )
            previous = receipt_sha
            previous_completed_at = completed_at
            continue
        if receipt.get("action") == "eligible":
            expected_keys = common_keys | {
                "completed", "action", "cash_reason", "qualification_anchor_sha256",
                "live_signal_row_artifact_sha256", "live_panel_record_sha256",
                "candidate_artifact_sha256", "permit_artifact_sha256", "limit_price",
                "completed_at_epoch_millis", "preauthorization_consumed", "signed",
                "post_attempts", "orders",
            }
            candidate, candidate_sha = read_canonical_object(
                window.candidate,
                require_root_owned=require_root_owned,
            )
            permit, permit_sha = read_canonical_object(
                window.permit,
                require_root_owned=require_root_owned,
            )
            _validate_account_identity_chain(
                authorization=authorization,
                child=child,
                candidate=candidate,
                permit=permit,
            )
            _anchor, anchor_sha = read_canonical_object(
                window.qualification_anchor,
                require_root_owned=require_root_owned,
            )
            live_signal, live_signal_sha = read_canonical_object(
                window.live_signal_row,
                require_root_owned=require_root_owned,
            )
            if (
                set(receipt) != expected_keys
                or receipt.get("completed") is not True
                or receipt.get("cash_reason") is not None
                or receipt.get("post_attempts") != 0
                or receipt.get("orders") != "none"
                or receipt.get("preauthorization_consumed") is not False
                or receipt.get("signed") is not False
                or receipt.get("candidate_artifact_sha256") != candidate_sha
                or receipt.get("permit_artifact_sha256") != permit_sha
                or receipt.get("qualification_anchor_sha256") != anchor_sha
                or receipt.get("live_signal_row_artifact_sha256")
                != live_signal_sha
                or receipt.get("live_panel_record_sha256")
                != live_signal.get("panel_record_sha256")
                or candidate.get("authorization_sha256") != child_sha
                or permit.get("authorization_sha256") != child_sha
                or permit.get("candidate_artifact_sha256") != candidate_sha
                or _artifact_present(window.completion)
                or _artifact_present(window.intent)
                or _artifact_present(window.transport_started)
                or _artifact_present(window.submission_unknown)
                or _artifact_present(window.execution_result)
                or _artifact_present(window.execute_wrapper_result)
            ):
                raise HorizonRefusal(
                    "eligible_receipt_invalid",
                    f"window {index} eligible bundle/receipt drifted",
                )
            for future in range(index + 1, HORIZON_WINDOWS):
                later_dir = window_paths(
                    paths,
                    future,
                    schedule["windows"][future]["exact_window_start_epoch"],
                ).run_dir
                if os.path.lexists(later_dir):
                    raise HorizonRefusal(
                        "eligible_was_skipped",
                        "artifacts exist after first eligible",
                    )
            return index, receipt_sha, receipt
        raise HorizonRefusal("window_receipt_action_invalid", f"window {index} action invalid")
    return HORIZON_WINDOWS, previous, None


def _terminal_payload(
    *,
    authorization_sha256: str,
    schedule_sha256: str,
    manifest_sha256: str,
    decision: str,
    completed_windows: int,
    post_attempts: int,
    orders: str,
    completed_at_epoch_millis: int,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": TERMINAL_SCHEMA,
        "version": VERSION,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": schedule_sha256,
        "manifest_sha256": manifest_sha256,
        "decision": decision,
        "completed_windows": completed_windows,
        "maximum_windows": HORIZON_WINDOWS,
        "post_attempts": post_attempts,
        "maximum_post_attempts": 1,
        "orders": orders,
        "retry_permitted": False,
        "extension_permitted": False,
        "window_145_permitted": False,
        "completed_at_epoch_millis": completed_at_epoch_millis,
    }
    if detail:
        payload["detail"] = dict(detail)
    return payload


def _validate_terminal(
    payload: dict[str, Any],
    *,
    authorization_sha256: str,
    schedule_sha256: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    base_keys = {
        "schema", "version", "parent_horizon_authorization_sha256",
        "schedule_sha256", "manifest_sha256", "decision", "completed_windows",
        "maximum_windows", "post_attempts", "maximum_post_attempts", "orders",
        "retry_permitted", "extension_permitted", "window_145_permitted",
        "completed_at_epoch_millis",
    }
    if frozenset(payload) not in {
        frozenset(base_keys),
        frozenset(base_keys | {"detail"}),
    }:
        raise HorizonRefusal("terminal_fields_invalid", "terminal fields drifted")
    fixed = {
        "schema": TERMINAL_SCHEMA,
        "version": VERSION,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": schedule_sha256,
        "manifest_sha256": manifest_sha256,
        "maximum_windows": HORIZON_WINDOWS,
        "maximum_post_attempts": 1,
        "retry_permitted": False,
        "extension_permitted": False,
        "window_145_permitted": False,
    }
    if any(payload.get(key) != value for key, value in fixed.items()):
        raise HorizonRefusal("terminal_identity_invalid", "terminal identity/literals drifted")
    completed = _integer(payload.get("completed_windows"), "completed_windows")
    attempts = _integer(payload.get("post_attempts"), "post_attempts")
    _integer(payload.get("completed_at_epoch_millis"), "completed_at_epoch_millis")
    decision = payload.get("decision")
    if completed > HORIZON_WINDOWS or attempts > 1 or not isinstance(decision, str):
        raise HorizonRefusal("terminal_value_invalid", "terminal counts/decision drifted")
    actual_semantics = (attempts, payload.get("orders"))
    if decision == "horizon_completed_144_cash_no_trade":
        if (completed, attempts, payload.get("orders")) != (
            HORIZON_WINDOWS,
            0,
            "none",
        ):
            raise HorizonRefusal(
                "terminal_no_trade_semantics_invalid",
                "all-cash terminal must be exactly 144/0/none",
            )
    elif decision in ZERO_ATTEMPT_TERMINAL_DECISIONS:
        if actual_semantics != (0, "none"):
            raise HorizonRefusal(
                "terminal_pre_delegation_semantics_invalid",
                "pre-delegation terminal must be zero/none",
            )
        if decision == "launch_window_missed_terminal" and completed != 0:
            raise HorizonRefusal(
                "terminal_launch_missed_semantics_invalid",
                "missed initial launch must be exactly zero completed windows",
            )
    elif decision in UNKNOWN_SUBMISSION_TERMINAL_DECISIONS:
        if actual_semantics != (1, "one_fok_submission_unknown"):
            raise HorizonRefusal(
                "terminal_unknown_semantics_invalid",
                "unknown terminal must be one/one_fok_submission_unknown",
            )
    elif decision == "first_eligible_fok_response_terminal":
        if actual_semantics != (1, "one_fok_attempt"):
            raise HorizonRefusal(
                "terminal_response_semantics_invalid",
                "response terminal must be one/one_fok_attempt",
            )
    else:
        raise HorizonRefusal(
            "terminal_decision_invalid",
            "terminal decision is not an enumerated v275 outcome",
        )
    if "detail" in payload and not isinstance(payload["detail"], dict):
        raise HorizonRefusal("terminal_detail_invalid", "terminal detail is not an object")
    return payload


def _write_terminal(paths: HorizonPaths, payload: dict[str, Any], *, require_root_owned: bool) -> dict[str, Any]:
    _validate_terminal(
        payload,
        authorization_sha256=payload["parent_horizon_authorization_sha256"],
        schedule_sha256=payload["schedule_sha256"],
        manifest_sha256=payload["manifest_sha256"],
    )
    if _artifact_present(paths.terminal):
        existing, _ = read_canonical_object(paths.terminal, require_root_owned=require_root_owned)
        _validate_terminal(
            existing,
            authorization_sha256=payload["parent_horizon_authorization_sha256"],
            schedule_sha256=payload["schedule_sha256"],
            manifest_sha256=payload["manifest_sha256"],
        )
        return existing
    try:
        _write_exclusive_json(paths.terminal, payload)
        return payload
    except HorizonRefusal as exc:
        # O_EXCL is the terminal linearization point.  A simultaneous
        # coordinator may win after our existence check; in that case consume
        # and validate the immutable winner instead of turning a successfully
        # terminal horizon into a restart/error path.
        if exc.code != "immutable_output_exists":
            raise
        existing, _ = read_canonical_object(
            paths.terminal,
            require_root_owned=require_root_owned,
        )
        return _validate_terminal(
            existing,
            authorization_sha256=payload[
                "parent_horizon_authorization_sha256"
            ],
            schedule_sha256=payload["schedule_sha256"],
            manifest_sha256=payload["manifest_sha256"],
        )


def _systemctl_start(
    unit: str,
    *,
    systemctl_path: Path,
    timeout: int,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(systemctl_path), "start", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
        return {
            "returncode": result.returncode,
            "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        }
    except Exception as exc:
        return {
            "returncode": None,
            "exception_type": type(exc).__name__,
            "exception_sha256": hashlib.sha256(str(exc).encode()).hexdigest(),
        }


def _partial_window_exists(window: WindowPaths) -> bool:
    # The directory itself is already a durable child artifact.  Counting an
    # empty or broken-symlink preseed prevents a future-index attacker from
    # hiding behind Path.exists()/iterdir() semantics.
    return os.path.lexists(window.run_dir)


def _artifact_present(path: Path) -> bool:
    """Treat regular files, links, and broken-link preseeds as artifacts."""

    return os.path.lexists(path)


def _first_window_execution_stage_artifact(
    paths: HorizonPaths,
    schedule: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Find any stage that makes a zero-attempt terminal unprovable."""

    stage_names = (
        "completion",
        "intent",
        "transport_started",
        "submission_unknown",
        "execution_result",
        "execute_wrapper_result",
    )
    for index, item in enumerate(schedule["windows"]):
        window = window_paths(
            paths,
            index,
            item["exact_window_start_epoch"],
        )
        for stage_name in stage_names:
            if _artifact_present(getattr(window, stage_name)):
                return {
                    "window_index": index,
                    "execution_stage_artifact": stage_name,
                }
    return None


def _finalize_window_execution_stage_if_present(
    *,
    paths: HorizonPaths,
    schedule: dict[str, Any],
    authorization_sha256: str,
    schedule_sha256: str,
    manifest_sha256: str,
    completed_windows: int,
    clock: Callable[[], int],
    require_root_owned: bool,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    stage = _first_window_execution_stage_artifact(paths, schedule)
    if stage is None:
        return None
    return _finalize_after_delegation(
        paths=paths,
        authorization_sha256=authorization_sha256,
        schedule_sha256=schedule_sha256,
        manifest_sha256=manifest_sha256,
        schedule=schedule,
        completed_windows=completed_windows,
        clock=clock,
        require_root_owned=require_root_owned,
        observation={
            **dict(observation or {}),
            **stage,
            "stage_without_global_consumption_marker": True,
        },
    )


DELEGATION_FIELDS = frozenset(
    {
        "schema",
        "parent_horizon_authorization_sha256",
        "schedule_sha256",
        "manifest_sha256",
        "window_index",
        "exact_window_start_epoch",
        "exact_window_end_epoch",
        "window_receipt_sha256",
        "derived_child_authorization_sha256",
        "permit_artifact_sha256",
        "execute_unit_invocations",
        "maximum_execute_unit_invocations",
        "delegated_at_epoch_millis",
        "retry_permitted",
        "global_horizon_consumed",
        "horizon_terminal_on_creation",
        "possible_post_attempts_if_process_state_unknown",
        "orders_if_process_state_unknown",
    }
)
EXECUTE_INVOCATION_FIELDS = frozenset(
    {
        "schema",
        "parent_horizon_authorization_sha256",
        "schedule_sha256",
        "manifest_sha256",
        "consuming_delegation_sha256",
        "window_receipt_sha256",
        "derived_child_authorization_sha256",
        "permit_artifact_sha256",
        "window_index",
        "exact_window_start_epoch",
        "exact_window_end_epoch",
        "execute_unit_invocation",
        "maximum_execute_unit_invocations",
        "invoked_at_epoch_millis",
        "retry_permitted",
        "horizon_terminal_on_creation",
    }
)
ATTEMPT_FIELDS = frozenset(
    {
        "schema",
        "parent_horizon_authorization_sha256",
        "derived_child_authorization_sha256",
        "window_index",
        "permit_artifact_sha256",
        "execute_invocation_sha256",
        "maximum_post_attempts",
        "execute_once_invocations",
        "transport_retry_enabled",
        "horizon_terminal_on_creation",
        "claimed_at_epoch_millis",
    }
)


def _validate_delegation(
    delegation: dict[str, Any],
    *,
    authorization_sha256: str,
    schedule_sha256: str,
    manifest_sha256: str,
    schedule: dict[str, Any],
    expected_window_receipt_sha256: str | None = None,
    expected_window_receipt_completed_at_epoch_millis: int | None = None,
    expected_child_sha256: str | None = None,
    expected_permit_sha256: str | None = None,
) -> tuple[int, int]:
    """Validate the consuming launch claim before any signer can run."""

    if set(delegation) != DELEGATION_FIELDS:
        raise HorizonRefusal(
            "delegation_fields_invalid",
            "consuming delegation fields drifted",
        )
    index = _integer(delegation.get("window_index"), "delegation.window_index")
    if index >= HORIZON_WINDOWS:
        raise HorizonRefusal(
            "delegation_index_invalid",
            "consuming delegation cannot address window 145",
        )
    item = schedule["windows"][index]
    exact_start = item["exact_window_start_epoch"]
    exact_end = item["exact_window_end_epoch"]
    fixed = {
        "schema": DELEGATION_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": schedule_sha256,
        "manifest_sha256": manifest_sha256,
        "window_index": index,
        "exact_window_start_epoch": exact_start,
        "exact_window_end_epoch": exact_end,
        "execute_unit_invocations": 1,
        "maximum_execute_unit_invocations": 1,
        "retry_permitted": False,
        "global_horizon_consumed": True,
        "horizon_terminal_on_creation": True,
        "possible_post_attempts_if_process_state_unknown": 1,
        "orders_if_process_state_unknown": "one_fok_submission_unknown",
    }
    if any(delegation.get(key) != value for key, value in fixed.items()):
        raise HorizonRefusal(
            "delegation_literal_invalid",
            "consuming delegation identity/literals drifted",
        )
    receipt_sha = _hash(
        delegation.get("window_receipt_sha256"),
        "delegation.window_receipt_sha256",
    )
    child_sha = _hash(
        delegation.get("derived_child_authorization_sha256"),
        "delegation.derived_child_authorization_sha256",
    )
    permit_sha = _hash(
        delegation.get("permit_artifact_sha256"),
        "delegation.permit_artifact_sha256",
    )
    delegated_at = _integer(
        delegation.get("delegated_at_epoch_millis"),
        "delegation.delegated_at_epoch_millis",
    )
    if not (exact_start * 1000 <= delegated_at < exact_end * 1000):
        raise HorizonRefusal(
            "delegation_clock_invalid",
            "consuming delegation was not created inside its exact window",
        )
    if (
        expected_window_receipt_completed_at_epoch_millis is not None
        and delegated_at
        < _integer(
            expected_window_receipt_completed_at_epoch_millis,
            "expected_window_receipt_completed_at_epoch_millis",
        )
    ):
        raise HorizonRefusal(
            "delegation_clock_invalid",
            "consuming delegation predates its bound eligible receipt",
        )
    expected_hashes = (
        (expected_window_receipt_sha256, receipt_sha, "window receipt"),
        (expected_child_sha256, child_sha, "derived child"),
        (expected_permit_sha256, permit_sha, "permit"),
    )
    for expected, actual, label in expected_hashes:
        if expected is not None and actual != _hash(expected, f"expected {label}"):
            raise HorizonRefusal(
                "delegation_bundle_mismatch",
                f"consuming delegation binds another {label}",
            )
    return index, exact_start


def _validate_execute_invocation(
    invocation: Mapping[str, Any],
    *,
    authorization_sha256: str,
    schedule_sha256: str,
    manifest_sha256: str,
    delegation_sha256: str,
    window_receipt_sha256: str,
    child_sha256: str,
    permit_sha256: str,
    window_index: int,
    exact_window_start_epoch: int,
    exact_window_end_epoch: int,
    delegated_at_epoch_millis: int,
) -> int:
    if set(invocation) != EXECUTE_INVOCATION_FIELDS:
        raise HorizonRefusal(
            "execute_invocation_fields_invalid",
            "execute invocation fields drifted",
        )
    fixed = {
        "schema": EXECUTE_INVOCATION_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "schedule_sha256": schedule_sha256,
        "manifest_sha256": manifest_sha256,
        "consuming_delegation_sha256": delegation_sha256,
        "window_receipt_sha256": window_receipt_sha256,
        "derived_child_authorization_sha256": child_sha256,
        "permit_artifact_sha256": permit_sha256,
        "window_index": window_index,
        "exact_window_start_epoch": exact_window_start_epoch,
        "exact_window_end_epoch": exact_window_end_epoch,
        "execute_unit_invocation": 1,
        "maximum_execute_unit_invocations": 1,
        "retry_permitted": False,
        "horizon_terminal_on_creation": True,
    }
    if any(invocation.get(key) != value for key, value in fixed.items()):
        raise HorizonRefusal(
            "execute_invocation_binding_invalid",
            "execute invocation binds another horizon child",
        )
    invoked_at = _integer(
        invocation.get("invoked_at_epoch_millis"),
        "execute_invocation.invoked_at_epoch_millis",
    )
    if not (
        _integer(
            delegated_at_epoch_millis,
            "delegation.delegated_at_epoch_millis",
        )
        <= invoked_at
        < exact_window_end_epoch * 1000
    ):
        raise HorizonRefusal(
            "execute_invocation_clock_invalid",
            "execute invocation is not causally after delegation in the exact window",
        )
    return invoked_at


def _validate_global_attempt(
    attempt: Mapping[str, Any],
    *,
    authorization_sha256: str,
    child_sha256: str,
    window_index: int,
    permit_sha256: str,
    invocation_sha256: str,
    invoked_at_epoch_millis: int,
) -> int:
    if set(attempt) != ATTEMPT_FIELDS:
        raise HorizonRefusal(
            "global_attempt_fields_invalid",
            "global attempt fields drifted",
        )
    fixed = {
        "schema": ATTEMPT_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha256,
        "derived_child_authorization_sha256": child_sha256,
        "window_index": window_index,
        "permit_artifact_sha256": permit_sha256,
        "execute_invocation_sha256": invocation_sha256,
        "maximum_post_attempts": 1,
        "execute_once_invocations": 1,
        "transport_retry_enabled": False,
        "horizon_terminal_on_creation": True,
    }
    if any(attempt.get(key) != value for key, value in fixed.items()):
        raise HorizonRefusal(
            "global_attempt_binding_invalid",
            "global attempt binds another invocation or child",
        )
    claimed_at = _integer(
        attempt.get("claimed_at_epoch_millis"),
        "global_attempt.claimed_at_epoch_millis",
    )
    if claimed_at < _integer(
        invoked_at_epoch_millis,
        "execute_invocation.invoked_at_epoch_millis",
    ):
        raise HorizonRefusal(
            "global_attempt_clock_invalid",
            "global attempt predates the sole execute invocation",
        )
    return claimed_at


def _coordinate_under_claim_lease(
    *,
    files: ContractFiles = ContractFiles(
        CANONICAL_AUTHORIZATION, CANONICAL_SCHEDULE, CANONICAL_MANIFEST, CANONICAL_HORIZON_RECEIPT
    ),
    root: Path = CANONICAL_ROOT,
    systemctl_path: Path = CANONICAL_SYSTEMCTL,
    prepare_unit: str = CANONICAL_PREPARE_UNIT,
    execute_unit: str = CANONICAL_EXECUTE_UNIT,
    now_epoch_millis: Callable[[], int] | None = None,
    require_root: bool = True,
    claim_lease_held: bool = False,
) -> dict[str, Any]:
    """Networkless strict-next coordinator.  It never imports v274."""
    if not claim_lease_held:
        raise HorizonRefusal(
            "coordinator_claim_lease_required",
            "coordinator transitions require the exact claim-inode lease",
        )
    if require_root and (
        files != ContractFiles(CANONICAL_AUTHORIZATION, CANONICAL_SCHEDULE, CANONICAL_MANIFEST, CANONICAL_HORIZON_RECEIPT)
        or root != CANONICAL_ROOT or systemctl_path != CANONICAL_SYSTEMCTL
        or prepare_unit != CANONICAL_PREPARE_UNIT or execute_unit != CANONICAL_EXECUTE_UNIT
        or now_epoch_millis is not None
    ):
        raise HorizonRefusal("production_override_forbidden", "coordinator production inputs drifted")
    if require_root and os.geteuid() != 0:
        raise HorizonRefusal("production_owner_invalid", "coordinator requires root")
    if require_root:
        _validate_production_surface(
            files=files,
            root=root,
            require_runtime=True,
            require_systemctl=True,
        )
        _validate_process_environment(credentials_permitted=False)
    clock = now_epoch_millis or (lambda: time.time_ns() // 1_000_000)
    (
        authorization, authorization_sha, schedule, schedule_sha,
        manifest, manifest_sha, horizon_receipt, horizon_receipt_sha,
    ) = load_contract(files, require_root_owned=require_root)
    paths = horizon_paths(root, authorization_sha)
    _ensure_exact_directory(
        paths.run_dir,
        require_root_owned=require_root,
    )
    now_ms = clock()
    if _artifact_present(paths.terminal):
        existing, _ = read_canonical_object(paths.terminal, require_root_owned=require_root)
        return _validate_terminal(
            existing,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
        )
    if (
        _artifact_present(paths.delegation)
        or _artifact_present(paths.execute_invocation)
        or _artifact_present(paths.attempt)
        or _artifact_present(paths.transport)
    ):
        return _finalize_after_delegation(
            paths=paths,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            schedule=schedule,
            completed_windows=0,
            clock=clock,
            require_root_owned=require_root,
            observation={"claim_present": _artifact_present(paths.claim)},
        )
    execution_stage = _first_window_execution_stage_artifact(paths, schedule)
    if execution_stage is not None:
        return _finalize_after_delegation(
            paths=paths,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            schedule=schedule,
            completed_windows=0,
            clock=clock,
            require_root_owned=require_root,
            observation={
                **execution_stage,
                "stage_without_global_consumption_marker": True,
            },
        )
    if not _artifact_present(paths.claim):
        raise HorizonRefusal(
            "claim_disappeared_under_lease",
            "validated claim path disappeared while its exact inode was locked",
        )
    else:
        claim, claim_sha = read_canonical_object(paths.claim, require_root_owned=require_root)
        _validate_claim(
            claim, authorization=authorization, authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha, manifest_sha256=manifest_sha,
            horizon_receipt_sha256=horizon_receipt_sha,
            enforce_current_boot=False,
        )
        _ensure_exact_directory(
            paths.run_dir / "windows",
            require_root_owned=require_root,
        )
        observed_boot_id = _boot_id()
        resume_refusal: str | None = None
        if claim["boot_id"] != observed_boot_id:
            resume_refusal = "boot_drift_terminal"
        elif now_ms < claim["claimed_at_epoch_millis"]:
            resume_refusal = "wall_clock_rollback_terminal"
        elif now_ms >= authorization["horizon_end_epoch"] * 1000:
            resume_refusal = "horizon_expired_before_resume_terminal"
        if resume_refusal is not None:
            completed_windows = 0
            scan_refusal: str | None = None
            try:
                scanned_index, _previous, scanned_eligible = _scan_receipts(
                    paths,
                    authorization=authorization,
                    authorization_sha256=authorization_sha,
                    schedule=schedule,
                    schedule_sha256=schedule_sha,
                    manifest=manifest,
                    manifest_sha256=manifest_sha,
                    horizon_receipt_sha256=horizon_receipt_sha,
                    claim_sha256=claim_sha,
                    require_root_owned=require_root,
                )
                completed_windows = scanned_index + (
                    1 if scanned_eligible is not None else 0
                )
            except HorizonRefusal as exc:
                scan_refusal = exc.code
            detail = {
                "resume_refusal": resume_refusal,
                "claim_boot_id": claim["boot_id"],
                "observed_boot_id": observed_boot_id,
                "claimed_at_epoch_millis": claim["claimed_at_epoch_millis"],
                "observed_at_epoch_millis": now_ms,
            }
            if scan_refusal is not None:
                detail["receipt_scan_refusal"] = scan_refusal
            if (
                _artifact_present(paths.delegation)
                or _artifact_present(paths.execute_invocation)
                or _artifact_present(paths.attempt)
                or _artifact_present(paths.transport)
            ):
                return _finalize_after_delegation(
                    paths=paths,
                    authorization_sha256=authorization_sha,
                    schedule_sha256=schedule_sha,
                    manifest_sha256=manifest_sha,
                    schedule=schedule,
                    completed_windows=completed_windows,
                    clock=clock,
                    require_root_owned=require_root,
                    observation=detail,
                )
            stage_terminal = _finalize_window_execution_stage_if_present(
                paths=paths,
                schedule=schedule,
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                completed_windows=completed_windows,
                clock=clock,
                require_root_owned=require_root,
                observation=detail,
            )
            if stage_terminal is not None:
                return stage_terminal
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision=resume_refusal,
                completed_windows=completed_windows,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=now_ms,
                detail=detail,
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=require_root,
            )
    if (
        _artifact_present(paths.delegation)
        or _artifact_present(paths.execute_invocation)
        or _artifact_present(paths.attempt)
        or _artifact_present(paths.transport)
    ):
        return _finalize_after_delegation(
            paths=paths, authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha, manifest_sha256=manifest_sha,
            schedule=schedule,
            completed_windows=0,
            clock=clock, require_root_owned=require_root,
        )
    try:
        next_index, previous_receipt_sha, eligible_receipt = _scan_receipts(
            paths,
            authorization=authorization,
            authorization_sha256=authorization_sha,
            schedule=schedule,
            schedule_sha256=schedule_sha,
            manifest=manifest,
            manifest_sha256=manifest_sha,
            horizon_receipt_sha256=horizon_receipt_sha,
            claim_sha256=claim_sha,
            require_root_owned=require_root,
        )
    except HorizonRefusal as exc:
        stage_terminal = _finalize_window_execution_stage_if_present(
            paths=paths,
            schedule=schedule,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            completed_windows=0,
            clock=clock,
            require_root_owned=require_root,
            observation={"receipt_scan_refusal": exc.code},
        )
        if stage_terminal is not None:
            return stage_terminal
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            decision="strict_next_ledger_refusal_terminal",
            completed_windows=0,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=clock(),
            detail={"refusal": exc.code},
        )
        return _write_terminal(
            paths,
            terminal,
            require_root_owned=require_root,
        )
    if eligible_receipt is not None:
        # A complete eligible receipt is not a resumable cash boundary.  If it
        # existed when this coordinator entered, the process that observed it
        # may have crashed immediately before consuming delegation creation.
        # Terminate at zero because no delegation exists, and never recreate
        # the same-process prepare->delegate transition after a restart.
        existing_eligible_index = _integer(
            eligible_receipt.get("window_index"),
            "existing_eligible_index",
        )
        stage_terminal = _finalize_window_execution_stage_if_present(
            paths=paths,
            schedule=schedule,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            completed_windows=existing_eligible_index + 1,
            clock=clock,
            require_root_owned=require_root,
            observation={"existing_eligible_receipt": True},
        )
        if stage_terminal is not None:
            return stage_terminal
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            decision=(
                "eligible_boundary_without_same_process_delegation_terminal"
            ),
            completed_windows=existing_eligible_index + 1,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=clock(),
            detail={"same_process_prepare_delegation_proof": False},
        )
        return _write_terminal(
            paths,
            terminal,
            require_root_owned=require_root,
        )
    while next_index < HORIZON_WINDOWS and eligible_receipt is None:
        item = schedule["windows"][next_index]
        window = window_paths(paths, next_index, item["exact_window_start_epoch"])
        stage_terminal = _finalize_window_execution_stage_if_present(
            paths=paths,
            schedule=schedule,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            completed_windows=next_index,
            clock=clock,
            require_root_owned=require_root,
            observation={"before_next_window": next_index},
        )
        if stage_terminal is not None:
            return stage_terminal
        if _partial_window_exists(window):
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha, schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha, decision="incomplete_child_terminal",
                completed_windows=next_index, post_attempts=0, orders="none",
                completed_at_epoch_millis=clock(),
            )
            return _write_terminal(paths, terminal, require_root_owned=require_root)
        now_ms = clock()
        if now_ms >= authorization["horizon_end_epoch"] * 1000:
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision="horizon_expired_before_resume_terminal",
                completed_windows=next_index,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=now_ms,
                detail={"absolute_h1_cutoff_enforced": True},
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=require_root,
            )
        if now_ms < claim["claimed_at_epoch_millis"]:
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision="wall_clock_rollback_terminal",
                completed_windows=next_index,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=now_ms,
                detail={
                    "claimed_at_epoch_millis": claim[
                        "claimed_at_epoch_millis"
                    ],
                    "observed_at_epoch_millis": now_ms,
                },
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=require_root,
            )
        if now_ms >= item["exact_window_start_epoch"] * 1000:
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha, schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha, decision="strict_next_window_missed_terminal",
                completed_windows=next_index, post_attempts=0, orders="none",
                completed_at_epoch_millis=now_ms,
            )
            return _write_terminal(paths, terminal, require_root_owned=require_root)
        request = _request_payload(
            authorization_sha256=authorization_sha, schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha, horizon_receipt_sha256=horizon_receipt_sha,
            claim_sha256=claim_sha, index=next_index,
            start=item["exact_window_start_epoch"],
            previous_window_receipt_sha256=previous_receipt_sha,
            requested_at_epoch_millis=now_ms,
        )
        _write_exclusive_json(window.request, request)
        observation = _systemctl_start(
            prepare_unit, systemctl_path=systemctl_path, timeout=8 * 60
        )
        stage_terminal = _finalize_window_execution_stage_if_present(
            paths=paths,
            schedule=schedule,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            completed_windows=next_index,
            clock=clock,
            require_root_owned=require_root,
            observation={"prepare_observation": observation},
        )
        if stage_terminal is not None:
            return stage_terminal
        if not _artifact_present(window.window_receipt):
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha, schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha, decision="prepare_exit_without_complete_boundary_terminal",
                completed_windows=next_index, post_attempts=0, orders="none",
                completed_at_epoch_millis=clock(), detail={"prepare_observation": observation},
            )
            return _write_terminal(paths, terminal, require_root_owned=require_root)
        post_prepare_ms = clock()
        if post_prepare_ms >= authorization["horizon_end_epoch"] * 1000:
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision="horizon_expired_before_resume_terminal",
                completed_windows=next_index,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=post_prepare_ms,
                detail={"absolute_h1_cutoff_enforced": True},
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=require_root,
            )
        try:
            scanned_index, previous_receipt_sha, eligible_receipt = _scan_receipts(
                paths,
                authorization=authorization,
                authorization_sha256=authorization_sha,
                schedule=schedule, schedule_sha256=schedule_sha,
                manifest=manifest, manifest_sha256=manifest_sha,
                horizon_receipt_sha256=horizon_receipt_sha,
                claim_sha256=claim_sha,
                require_root_owned=require_root,
            )
        except HorizonRefusal as exc:
            stage_terminal = _finalize_window_execution_stage_if_present(
                paths=paths,
                schedule=schedule,
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                completed_windows=next_index,
                clock=clock,
                require_root_owned=require_root,
                observation={"receipt_scan_refusal": exc.code},
            )
            if stage_terminal is not None:
                return stage_terminal
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision="strict_next_ledger_refusal_terminal",
                completed_windows=next_index,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=clock(),
                detail={"refusal": exc.code},
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=require_root,
            )
        if eligible_receipt is None:
            if scanned_index != next_index + 1:
                raise HorizonRefusal("next_index_drift", "prepare did not complete exact next cash window")
            next_index = scanned_index
            continue
        if scanned_index != next_index or eligible_receipt.get("action") != "eligible":
            raise HorizonRefusal("eligible_index_drift", "first eligible is not strict next")

    if eligible_receipt is None:
        stage_terminal = _finalize_window_execution_stage_if_present(
            paths=paths,
            schedule=schedule,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            completed_windows=HORIZON_WINDOWS,
            clock=clock,
            require_root_owned=require_root,
            observation={"all_cash_boundary_recheck": True},
        )
        if stage_terminal is not None:
            return stage_terminal
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha, schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha, decision="horizon_completed_144_cash_no_trade",
            completed_windows=HORIZON_WINDOWS, post_attempts=0, orders="none",
            completed_at_epoch_millis=clock(),
        )
        return _write_terminal(paths, terminal, require_root_owned=require_root)

    eligible_index = _integer(eligible_receipt["window_index"], "eligible_index")
    eligible_observed_ms = clock()
    stage_terminal = _finalize_window_execution_stage_if_present(
        paths=paths,
        schedule=schedule,
        authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha,
        completed_windows=eligible_index + 1,
        clock=clock,
        require_root_owned=require_root,
        observation={"fresh_eligible_observed": True},
    )
    if stage_terminal is not None:
        return stage_terminal
    if eligible_observed_ms >= authorization["horizon_end_epoch"] * 1000:
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            decision="horizon_expired_before_resume_terminal",
            completed_windows=eligible_index + 1,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=eligible_observed_ms,
            detail={"absolute_h1_cutoff_enforced": True},
        )
        return _write_terminal(
            paths,
            terminal,
            require_root_owned=require_root,
        )
    # Prepare may have waited four minutes.  Re-read the complete strict-next
    # ledger and every globally terminal marker immediately before consuming
    # the horizon; a concurrent coordinator is never allowed to delegate from
    # an observation made before that wait.
    if _artifact_present(paths.terminal):
        existing, _ = read_canonical_object(
            paths.terminal,
            require_root_owned=require_root,
        )
        return _validate_terminal(
            existing,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
        )
    pre_delegation_ms = clock()
    if pre_delegation_ms >= authorization["horizon_end_epoch"] * 1000:
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            decision="horizon_expired_before_resume_terminal",
            completed_windows=eligible_index + 1,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=pre_delegation_ms,
            detail={"absolute_h1_cutoff_enforced": True},
        )
        return _write_terminal(
            paths,
            terminal,
            require_root_owned=require_root,
        )
    if (
        _artifact_present(paths.delegation)
        or _artifact_present(paths.execute_invocation)
        or _artifact_present(paths.attempt)
        or _artifact_present(paths.transport)
    ):
        return _finalize_after_delegation(
            paths=paths,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            schedule=schedule,
            completed_windows=eligible_index + 1,
            clock=clock,
            require_root_owned=require_root,
        )
    try:
        rescanned_index, rescanned_receipt_sha, rescanned_eligible = _scan_receipts(
            paths,
            authorization=authorization,
            authorization_sha256=authorization_sha,
            schedule=schedule,
            schedule_sha256=schedule_sha,
            manifest=manifest,
            manifest_sha256=manifest_sha,
            horizon_receipt_sha256=horizon_receipt_sha,
            claim_sha256=claim_sha,
            require_root_owned=require_root,
        )
    except HorizonRefusal as exc:
        stage_terminal = _finalize_window_execution_stage_if_present(
            paths=paths,
            schedule=schedule,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            completed_windows=eligible_index + 1,
            clock=clock,
            require_root_owned=require_root,
            observation={"pre_delegation_scan_refusal": exc.code},
        )
        if stage_terminal is not None:
            return stage_terminal
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            decision="pre_delegation_ledger_drift_terminal",
            completed_windows=eligible_index,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=clock(),
            detail={"refusal": exc.code},
        )
        return _write_terminal(
            paths,
            terminal,
            require_root_owned=require_root,
        )
    if (
        rescanned_eligible is None
        or rescanned_index != eligible_index
        or rescanned_receipt_sha != previous_receipt_sha
        or rescanned_eligible != eligible_receipt
    ):
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            decision="pre_delegation_ledger_drift_terminal",
            completed_windows=eligible_index,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=clock(),
        )
        return _write_terminal(
            paths,
            terminal,
            require_root_owned=require_root,
        )
    stage_terminal = _finalize_window_execution_stage_if_present(
        paths=paths,
        schedule=schedule,
        authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha,
        completed_windows=eligible_index + 1,
        clock=clock,
        require_root_owned=require_root,
        observation={"before_shared_lock_recheck": True},
    )
    if stage_terminal is not None:
        return stage_terminal
    if require_root:
        try:
            _validate_shared_lock_identity(
                horizon_receipt,
                require_root_owned=True,
            )
        except HorizonRefusal as exc:
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision="shared_lock_replaced_pre_delegation_terminal",
                completed_windows=eligible_index + 1,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=clock(),
                detail={"refusal": exc.code},
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=True,
            )
        try:
            _validate_installed_public_execution_identity(
                authorization,
                require_root_owned=True,
                require_canonical_path=True,
            )
        except HorizonRefusal as exc:
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision="public_identity_replaced_pre_delegation_terminal",
                completed_windows=eligible_index + 1,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=clock(),
                detail={"refusal": exc.code},
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=True,
            )
    if _artifact_present(paths.terminal):
        existing, _ = read_canonical_object(
            paths.terminal,
            require_root_owned=require_root,
        )
        return _validate_terminal(
            existing,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
        )
    stage_terminal = _finalize_window_execution_stage_if_present(
        paths=paths,
        schedule=schedule,
        authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha,
        completed_windows=eligible_index + 1,
        clock=clock,
        require_root_owned=require_root,
        observation={"immediately_before_delegation": True},
    )
    if stage_terminal is not None:
        return stage_terminal
    final_pre_delegation_ms = clock()
    if final_pre_delegation_ms >= authorization["horizon_end_epoch"] * 1000:
        terminal = _terminal_payload(
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            decision="horizon_expired_before_resume_terminal",
            completed_windows=eligible_index + 1,
            post_attempts=0,
            orders="none",
            completed_at_epoch_millis=final_pre_delegation_ms,
            detail={"absolute_h1_cutoff_enforced": True},
        )
        return _write_terminal(
            paths,
            terminal,
            require_root_owned=require_root,
        )
    delegation = {
        "schema": DELEGATION_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha,
        "schedule_sha256": schedule_sha,
        "manifest_sha256": manifest_sha,
        "window_index": eligible_index,
        "exact_window_start_epoch": eligible_receipt["exact_window_start_epoch"],
        "exact_window_end_epoch": eligible_receipt["exact_window_end_epoch"],
        "window_receipt_sha256": previous_receipt_sha,
        "derived_child_authorization_sha256": eligible_receipt["derived_child_authorization_sha256"],
        "permit_artifact_sha256": eligible_receipt["permit_artifact_sha256"],
        "execute_unit_invocations": 1,
        "maximum_execute_unit_invocations": 1,
        "delegated_at_epoch_millis": clock(),
        "retry_permitted": False,
        "global_horizon_consumed": True,
        "horizon_terminal_on_creation": True,
        "possible_post_attempts_if_process_state_unknown": 1,
        "orders_if_process_state_unknown": "one_fok_submission_unknown",
    }
    _write_exclusive_json(paths.delegation, delegation)
    observation = _systemctl_start(
        execute_unit, systemctl_path=systemctl_path, timeout=110
    )
    return _finalize_after_delegation(
        paths=paths, authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha, manifest_sha256=manifest_sha,
        schedule=schedule,
        completed_windows=eligible_index + 1, clock=clock,
        require_root_owned=require_root, observation=observation,
    )


def coordinate(
    *,
    files: ContractFiles = ContractFiles(
        CANONICAL_AUTHORIZATION,
        CANONICAL_SCHEDULE,
        CANONICAL_MANIFEST,
        CANONICAL_HORIZON_RECEIPT,
    ),
    root: Path = CANONICAL_ROOT,
    systemctl_path: Path = CANONICAL_SYSTEMCTL,
    prepare_unit: str = CANONICAL_PREPARE_UNIT,
    execute_unit: str = CANONICAL_EXECUTE_UNIT,
    now_epoch_millis: Callable[[], int] | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    """Create/read the claim, then hold its inode lease for all transitions."""

    canonical_files = ContractFiles(
        CANONICAL_AUTHORIZATION,
        CANONICAL_SCHEDULE,
        CANONICAL_MANIFEST,
        CANONICAL_HORIZON_RECEIPT,
    )
    if require_root and (
        files != canonical_files
        or root != CANONICAL_ROOT
        or systemctl_path != CANONICAL_SYSTEMCTL
        or prepare_unit != CANONICAL_PREPARE_UNIT
        or execute_unit != CANONICAL_EXECUTE_UNIT
        or now_epoch_millis is not None
    ):
        raise HorizonRefusal(
            "production_override_forbidden",
            "coordinator production inputs drifted",
        )
    if require_root and os.geteuid() != 0:
        raise HorizonRefusal(
            "production_owner_invalid",
            "coordinator requires root",
        )
    if require_root:
        _validate_production_surface(
            files=files,
            root=root,
            require_runtime=True,
            require_systemctl=True,
        )
        _validate_process_environment(credentials_permitted=False)
    clock = now_epoch_millis or (lambda: time.time_ns() // 1_000_000)
    (
        authorization,
        authorization_sha,
        schedule,
        schedule_sha,
        _manifest,
        manifest_sha,
        _horizon_receipt,
        horizon_receipt_sha,
    ) = load_contract(files, require_root_owned=require_root)
    paths = horizon_paths(root, authorization_sha)
    _ensure_exact_directory(
        paths.run_dir,
        require_root_owned=require_root,
    )
    if _artifact_present(paths.terminal):
        terminal, _ = read_canonical_object(
            paths.terminal,
            require_root_owned=require_root,
        )
        return _validate_terminal(
            terminal,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
        )
    if (
        _artifact_present(paths.delegation)
        or _artifact_present(paths.execute_invocation)
        or _artifact_present(paths.attempt)
        or _artifact_present(paths.transport)
    ):
        return _finalize_after_delegation(
            paths=paths,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            schedule=schedule,
            completed_windows=0,
            clock=clock,
            require_root_owned=require_root,
            observation={"claim_present": _artifact_present(paths.claim)},
        )
    execution_stage = _first_window_execution_stage_artifact(paths, schedule)
    if execution_stage is not None:
        return _finalize_after_delegation(
            paths=paths,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            schedule=schedule,
            completed_windows=0,
            clock=clock,
            require_root_owned=require_root,
            observation={
                **execution_stage,
                "stage_without_global_consumption_marker": True,
            },
        )
    if not _artifact_present(paths.claim):
        now_ms = clock()
        if now_ms < authorization[
            "coordinator_launch_not_before_epoch_millis"
        ]:
            raise HorizonRefusal(
                "claim_too_early",
                "initial claim cannot be created before the sealed H0-120s boundary",
            )
        if now_ms > authorization[
            "coordinator_launch_not_after_epoch_millis"
        ]:
            terminal = _terminal_payload(
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
                decision="launch_window_missed_terminal",
                completed_windows=0,
                post_attempts=0,
                orders="none",
                completed_at_epoch_millis=now_ms,
            )
            return _write_terminal(
                paths,
                terminal,
                require_root_owned=require_root,
            )
        claim = _claim_payload(
            authorization=authorization,
            authorization_sha256=authorization_sha,
            schedule_sha256=schedule_sha,
            manifest_sha256=manifest_sha,
            horizon_receipt_sha256=horizon_receipt_sha,
            claimed_at_epoch_millis=now_ms,
            boot_id=_boot_id(),
        )
        try:
            claim_sha = _write_exclusive_json(paths.claim, claim)
        except HorizonRefusal as exc:
            if exc.code != "immutable_output_exists":
                raise
            claim, claim_sha = read_canonical_object(
                paths.claim,
                require_root_owned=require_root,
            )
    else:
        claim, claim_sha = read_canonical_object(
            paths.claim,
            require_root_owned=require_root,
        )
    _validate_claim(
        claim,
        authorization=authorization,
        authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha,
        horizon_receipt_sha256=horizon_receipt_sha,
        enforce_current_boot=False,
    )
    with _exclusive_claim_lease(
        paths.claim,
        expected_claim_sha256=claim_sha,
        require_root_owned=require_root,
    ):
        return _coordinate_under_claim_lease(
            files=files,
            root=root,
            systemctl_path=systemctl_path,
            prepare_unit=prepare_unit,
            execute_unit=execute_unit,
            now_epoch_millis=now_epoch_millis,
            require_root=require_root,
            claim_lease_held=True,
        )


def _recorded_v274_response_is_fully_bound(
    *,
    paths: HorizonPaths,
    window: WindowPaths,
    schedule: dict[str, Any],
    authorization_sha256: str,
    schedule_sha256: str,
    manifest_sha256: str,
    delegation: dict[str, Any],
    delegation_sha256: str,
    expected_permit_sha256: str,
    expected_window_start_epoch: int,
    require_root_owned: bool,
) -> bool:
    """Accept only one causally bound v275 invocation plus the full v274 chain."""

    try:
        receipt, receipt_sha = read_canonical_object(
            window.window_receipt,
            require_root_owned=require_root_owned,
        )
        permit, permit_sha = read_canonical_object(
            window.permit,
            require_root_owned=require_root_owned,
        )
        invocation, invocation_sha = read_canonical_object(
            paths.execute_invocation,
            require_root_owned=require_root_owned,
        )
        attempt, _attempt_sha = read_canonical_object(
            paths.attempt,
            require_root_owned=require_root_owned,
        )
        intent, intent_sha = read_canonical_object(
            window.intent,
            require_root_owned=require_root_owned,
        )
        transport, transport_sha = read_canonical_object(
            window.transport_started,
            require_root_owned=require_root_owned,
        )
        result, _result_sha = read_canonical_object(
            window.execution_result,
            require_root_owned=require_root_owned,
        )
        expected_permit_sha = _hash(
            expected_permit_sha256,
            "expected_permit_sha256",
        )
        delegation_sha = _hash(
            delegation_sha256,
            "delegation_sha256",
        )
        child_sha = _hash(
            delegation.get("derived_child_authorization_sha256"),
            "delegation.derived_child_authorization_sha256",
        )
        receipt_completed_at = _integer(
            receipt.get("completed_at_epoch_millis"),
            "window_receipt.completed_at_epoch_millis",
        )
        delegated_index, delegated_start = _validate_delegation(
            delegation,
            authorization_sha256=authorization_sha256,
            schedule_sha256=schedule_sha256,
            manifest_sha256=manifest_sha256,
            schedule=schedule,
            expected_window_receipt_sha256=receipt_sha,
            expected_window_receipt_completed_at_epoch_millis=(
                receipt_completed_at
            ),
            expected_child_sha256=child_sha,
            expected_permit_sha256=expected_permit_sha,
        )
        eligible_receipt_keys = {
            "schema", "parent_horizon_authorization_sha256", "schedule_sha256",
            "manifest_sha256", "horizon_receipt_sha256",
            "strict_next_request_sha256", "derived_child_authorization_sha256",
            "child_commitment_sha256", "authorization_state", "window_index",
            "exact_window_start_epoch", "exact_window_end_epoch",
            "previous_window_receipt_sha256", "maximum_post_attempts_global",
            "transport_retry_enabled", "completed", "action", "cash_reason",
            "qualification_anchor_sha256", "live_signal_row_artifact_sha256",
            "live_panel_record_sha256", "candidate_artifact_sha256",
            "permit_artifact_sha256", "limit_price",
            "completed_at_epoch_millis", "preauthorization_consumed", "signed",
            "post_attempts", "orders",
        }
        if (
            set(receipt) != eligible_receipt_keys
            or receipt.get("schema") != WINDOW_RECEIPT_SCHEMA
            or receipt.get("parent_horizon_authorization_sha256")
            != authorization_sha256
            or receipt.get("schedule_sha256") != schedule_sha256
            or receipt.get("manifest_sha256") != manifest_sha256
            or receipt.get("derived_child_authorization_sha256") != child_sha
            or receipt.get("permit_artifact_sha256") != expected_permit_sha
            or receipt.get("window_index") != delegated_index
            or receipt.get("exact_window_start_epoch") != delegated_start
            or receipt.get("exact_window_end_epoch")
            != schedule["windows"][delegated_index]["exact_window_end_epoch"]
            or receipt.get("authorization_state")
            != "derived_from_explicit_bounded_horizon"
            or receipt.get("maximum_post_attempts_global") != 1
            or receipt.get("transport_retry_enabled") is not False
            or receipt.get("completed") is not True
            or receipt.get("action") != "eligible"
            or receipt.get("cash_reason") is not None
            or receipt.get("preauthorization_consumed") is not False
            or receipt.get("signed") is not False
            or receipt.get("post_attempts") != 0
            or receipt.get("orders") != "none"
        ):
            return False
        invoked_at = _validate_execute_invocation(
            invocation,
            authorization_sha256=authorization_sha256,
            schedule_sha256=schedule_sha256,
            manifest_sha256=manifest_sha256,
            delegation_sha256=delegation_sha,
            window_receipt_sha256=receipt_sha,
            child_sha256=child_sha,
            permit_sha256=expected_permit_sha,
            window_index=delegated_index,
            exact_window_start_epoch=delegated_start,
            exact_window_end_epoch=receipt["exact_window_end_epoch"],
            delegated_at_epoch_millis=delegation["delegated_at_epoch_millis"],
        )
        attempt_claimed_at = _validate_global_attempt(
            attempt,
            authorization_sha256=authorization_sha256,
            child_sha256=child_sha,
            window_index=delegated_index,
            permit_sha256=expected_permit_sha,
            invocation_sha256=invocation_sha,
            invoked_at_epoch_millis=invoked_at,
        )
        shared_fields = (
            "qualification_experiment_id",
            "qualification_report_sha256",
            "live_signal_run_id",
            "live_signal_definition_sha256",
            "exact_window_start_epoch",
        )
        intent_keys = {
            "schema", "orchestrator_version", "permit_artifact_sha256",
            *shared_fields, "maximum_post_attempts", "transport_retry_enabled",
            "preauthorization_consumed", "signed", "execution_state",
            "post_attempts", "reserved_at_epoch_millis",
        }
        transport_keys = {
            "schema", "orchestrator_version", "permit_artifact_sha256",
            "intent_sha256", *shared_fields, "execution_state",
            "preauthorization_consumed", "intent_reserved", "signed",
            "post_attempts", "transport_retry_enabled", "retry_permitted",
            "orders", "started_at_epoch_millis",
        }
        result_keys = {
            "schema", "orchestrator_version", "permit_artifact_sha256",
            "intent_sha256", "transport_started_sha256", *shared_fields,
            "execution_state", "order_contract_sha256", "maker_amount_micro",
            "taker_amount_micro", "fee_reserve_micro",
            "preauthorization_consumed", "intent_reserved", "signed",
            "post_attempts", "transport_retry_enabled", "retry_permitted",
            "orders", "outcome", "completed_at_epoch_millis",
        }
        if (
            permit_sha != expected_permit_sha
            or permit.get("exact_window_start_epoch")
            != expected_window_start_epoch
            or permit.get("side") != "BUY"
            or permit.get("order_type") != "FOK"
            or permit.get("exact_taker_amount_micro") != 5_000_000
            or permit.get("maximum_total_reserve_micro") != 2_500_000
            or permit.get("maximum_live_attempts") != 1
            or permit.get("transport_retry_enabled") is not False
            or set(intent) != intent_keys
            or set(transport) != transport_keys
            or set(result) != result_keys
        ):
            return False
        fixed_intent = {
            "schema": V274_INTENT_SCHEMA,
            "orchestrator_version": V274_VERSION,
            "permit_artifact_sha256": expected_permit_sha,
            "maximum_post_attempts": 1,
            "transport_retry_enabled": False,
            "preauthorization_consumed": True,
            "signed": True,
            "execution_state": "pre_submit_verified",
            "post_attempts": 0,
        }
        fixed_transport = {
            "schema": V274_TRANSPORT_STARTED_SCHEMA,
            "orchestrator_version": V274_VERSION,
            "permit_artifact_sha256": expected_permit_sha,
            "intent_sha256": intent_sha,
            "execution_state": "transport_started_response_not_yet_durable",
            "preauthorization_consumed": True,
            "intent_reserved": True,
            "signed": True,
            "post_attempts": 1,
            "transport_retry_enabled": False,
            "retry_permitted": False,
            "orders": "one_fok_submission_unknown",
        }
        fixed_result = {
            "schema": V274_EXECUTION_RESULT_SCHEMA,
            "orchestrator_version": V274_VERSION,
            "permit_artifact_sha256": expected_permit_sha,
            "intent_sha256": intent_sha,
            "transport_started_sha256": transport_sha,
            "execution_state": "response_recorded",
            "preauthorization_consumed": True,
            "intent_reserved": True,
            "signed": True,
            "post_attempts": 1,
            "transport_retry_enabled": False,
            "retry_permitted": False,
            "orders": "one_fok_attempt",
        }
        if (
            any(intent.get(key) != value for key, value in fixed_intent.items())
            or any(
                transport.get(key) != value
                for key, value in fixed_transport.items()
            )
            or any(result.get(key) != value for key, value in fixed_result.items())
        ):
            return False
        for artifact in (intent, transport, result):
            if any(
                artifact.get(field) != permit.get(field)
                for field in shared_fields
            ):
                return False
        reserved_at = _integer(
            intent.get("reserved_at_epoch_millis"),
            "intent.reserved_at_epoch_millis",
        )
        started_at = _integer(
            transport.get("started_at_epoch_millis"),
            "transport.started_at_epoch_millis",
        )
        completed_at = _integer(
            result.get("completed_at_epoch_millis"),
            "result.completed_at_epoch_millis",
        )
        maker = _integer(
            result.get("maker_amount_micro"),
            "result.maker_amount_micro",
            minimum=1,
        )
        taker = _integer(
            result.get("taker_amount_micro"),
            "result.taker_amount_micro",
            minimum=1,
        )
        fee = _integer(
            result.get("fee_reserve_micro"),
            "result.fee_reserve_micro",
        )
        if (
            not (
                receipt_completed_at
                <= delegation["delegated_at_epoch_millis"]
                <= invoked_at
                <= attempt_claimed_at
                <= reserved_at
                <= started_at
                <= completed_at
            )
            or taker != 5_000_000
            or maker + fee > 2_500_000
            or not HASH.fullmatch(
                str(result.get("order_contract_sha256") or "")
            )
            or not isinstance(result.get("outcome"), dict)
        ):
            return False
    except (HorizonRefusal, KeyError, TypeError, ValueError):
        return False
    return True


def _finalize_after_delegation(
    *,
    paths: HorizonPaths, authorization_sha256: str, schedule_sha256: str,
    manifest_sha256: str, schedule: dict[str, Any], completed_windows: int,
    clock: Callable[[], int],
    require_root_owned: bool, observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if _artifact_present(paths.terminal):
        existing, _ = read_canonical_object(
            paths.terminal,
            require_root_owned=require_root_owned,
        )
        return _validate_terminal(
            existing,
            authorization_sha256=authorization_sha256,
            schedule_sha256=schedule_sha256,
            manifest_sha256=manifest_sha256,
        )
    delegation_sha: str | None = None
    index = 0
    if not _artifact_present(paths.delegation):
        decision = "attempt_without_delegation_terminal"
        attempts = 1
        orders = "one_fok_submission_unknown"
        detail: dict[str, Any] = {
            "execute_observation": dict(observation or {}),
            "transport_omitted_without_delegation_identity": True,
            "execute_invocation_present": _artifact_present(
                paths.execute_invocation
            ),
        }
    else:
        try:
            delegation, delegation_sha = read_canonical_object(
                paths.delegation,
                require_root_owned=require_root_owned,
            )
            raw_index = delegation.get("window_index")
            if type(raw_index) is int and 0 <= raw_index < HORIZON_WINDOWS:
                index = raw_index
            index, exact_start = _validate_delegation(
                delegation,
                authorization_sha256=authorization_sha256,
                schedule_sha256=schedule_sha256,
                manifest_sha256=manifest_sha256,
                schedule=schedule,
            )
        except HorizonRefusal as exc:
            decision = "delegation_child_path_ambiguous_terminal"
            attempts = 1
            orders = "one_fok_submission_unknown"
            detail = {
                "delegation_refusal": exc.code,
                "execute_observation": dict(observation or {}),
            }
        else:
            completed_windows = max(completed_windows, index + 1)
            window = window_paths(paths, index, exact_start)
            wrapper_path = window.execute_wrapper_result
            transport_started = window.transport_started
            submission_unknown = window.submission_unknown
            execution_result = window.execution_result
            intent = window.intent
            possible_post = (
                _artifact_present(submission_unknown)
                or _artifact_present(transport_started)
                or _artifact_present(execution_result)
            )
            if possible_post:
                recorded = (
                    _artifact_present(execution_result)
                    and not _artifact_present(submission_unknown)
                    and _recorded_v274_response_is_fully_bound(
                        paths=paths,
                        window=window,
                        schedule=schedule,
                        authorization_sha256=authorization_sha256,
                        schedule_sha256=schedule_sha256,
                        manifest_sha256=manifest_sha256,
                        delegation=delegation,
                        delegation_sha256=delegation_sha,
                        expected_permit_sha256=delegation[
                            "permit_artifact_sha256"
                        ],
                        expected_window_start_epoch=exact_start,
                        require_root_owned=require_root_owned,
                    )
                )
                attempts = 1
                orders = (
                    "one_fok_attempt"
                    if recorded
                    else "one_fok_submission_unknown"
                )
                decision = (
                    "first_eligible_fok_response_terminal"
                    if recorded
                    else "first_eligible_submission_unknown_terminal"
                )
            elif _artifact_present(paths.attempt):
                attempts = 1
                orders = "one_fok_submission_unknown"
                decision = "global_attempt_claimed_process_state_unknown_terminal"
            elif _artifact_present(intent):
                attempts = 1
                orders = "one_fok_submission_unknown"
                decision = "pre_submit_intent_process_state_unknown_terminal"
            else:
                attempts = 1
                orders = "one_fok_submission_unknown"
                decision = "consuming_delegation_process_state_unknown_terminal"
            detail = {
                "execute_wrapper_result_present": _artifact_present(
                    wrapper_path
                ),
                "execute_invocation_present": _artifact_present(
                    paths.execute_invocation
                ),
                "execute_observation": dict(observation or {}),
            }
    if attempts == 1 and delegation_sha is not None:
        transport = {
            "schema": TRANSPORT_SCHEMA,
            "parent_horizon_authorization_sha256": authorization_sha256,
            "schedule_sha256": schedule_sha256,
            "manifest_sha256": manifest_sha256,
            "consuming_delegation_sha256": delegation_sha,
            "window_index": index,
            "post_attempts": 1,
            "orders": orders,
            "transport_state": decision,
            "retry_permitted": False,
            "recorded_at_epoch_millis": clock(),
        }
        if not _artifact_present(paths.transport):
            _write_exclusive_json(paths.transport, transport)
    terminal = _terminal_payload(
        authorization_sha256=authorization_sha256,
        schedule_sha256=schedule_sha256,
        manifest_sha256=manifest_sha256,
        decision=decision,
        completed_windows=completed_windows,
        post_attempts=attempts,
        orders=orders,
        completed_at_epoch_millis=clock(),
        detail=detail,
    )
    return _write_terminal(paths, terminal, require_root_owned=require_root_owned)


def _v274_paths(v274: Any, window: WindowPaths) -> Any:
    return v274.V274Paths(
        run_dir=window.run_dir,
        live_definition=window.live_definition,
        v271_definition=window.v271_definition,
        panel_ledger=window.panel_ledger,
        live_signal_row=window.live_signal_row,
        qualification_anchor=window.qualification_anchor,
        candidate=window.candidate,
        permit=window.permit,
        completion=window.completion,
        intent=window.intent,
        transport_started=window.transport_started,
        submission_unknown=window.submission_unknown,
        execution_result=window.execution_result,
    )


def _projection_fields(v274: Any, child: dict[str, Any], live_definition: dict[str, Any]) -> dict[str, Any]:
    fields = dict(child["v274_verifier_projection"])
    fields.update(
        {
            "live_signal_run_id": live_definition["live_signal_run_id"],
            "live_signal_definition_sha256": v274.canonical_sha256(live_definition),
            "v271_live_experiment_id": live_definition["v271_live_experiment_id"],
            "live_forward_cutoff": live_definition["forward_cutoff"],
        }
    )
    return fields


def _projection_receipt(v274: Any, fields: dict[str, Any], child_sha: str, registered_at: int) -> dict[str, Any]:
    return {
        "schema": v274.CONDITIONAL_RECEIPT_SCHEMA,
        "authorization_sha256": child_sha,
        "registered_at_epoch_millis": registered_at,
        "intent_reserved": False,
        "preauthorization_consumed": False,
        **{key: value for key, value in fields.items() if key != "schema"},
    }


def _validate_qualification(v274: Any, authorization: dict[str, Any], target_start: int) -> tuple[dict[str, Any], str]:
    manifest, manifest_sha = v274.read_canonical_object(
        CANONICAL_QUALIFICATION_MANIFEST, require_root_owned=True
    )
    if manifest_sha != authorization["qualification_manifest_sha256"]:
        raise HorizonRefusal("qualification_manifest_mismatch", "parent binds another qualification manifest")
    manifest_paths = v274.validate_qualification_manifest(
        manifest,
        manifest_sha256=manifest_sha,
        target_start=target_start,
        require_root_owned=True,
    )
    report, report_artifact_sha = v274.read_canonical_object(
        CANONICAL_QUALIFICATION_REPORT, require_root_owned=True
    )
    gate = v274._qualification_gate(report, target_start=target_start)
    if (
        report_artifact_sha != authorization["qualification_report_artifact_sha256"]
        or v274.sha256_regular_file(manifest_paths["paper_ledger"], require_root_owned=True)
        != authorization["qualification_paper_ledger_sha256"]
        or v274.sha256_regular_file(manifest_paths["panel_ledger"], require_root_owned=True)
        != authorization["qualification_panel_ledger_sha256"]
    ):
        raise HorizonRefusal("qualification_artifact_mismatch", "qualification snapshot bytes drifted")
    return gate, manifest_sha


def _verify_request_and_child(
    *,
    files: ContractFiles, root: Path, require_root_owned: bool,
) -> tuple[
    dict[str, Any], str, dict[str, Any], str, dict[str, Any], str,
    HorizonPaths, WindowPaths, dict[str, Any], str, dict[str, Any], str,
]:
    (
        authorization, authorization_sha, schedule, schedule_sha,
        manifest, manifest_sha, _horizon_receipt, horizon_receipt_sha,
    ) = load_contract(files, require_root_owned=require_root_owned)
    paths = horizon_paths(root, authorization_sha)
    _ensure_exact_directory(
        paths.run_dir,
        require_root_owned=require_root_owned,
    )
    _ensure_exact_directory(
        paths.run_dir / "windows",
        require_root_owned=require_root_owned,
    )
    claim, claim_sha = read_canonical_object(paths.claim, require_root_owned=require_root_owned)
    _validate_claim(
        claim, authorization=authorization, authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha, manifest_sha256=manifest_sha,
        horizon_receipt_sha256=horizon_receipt_sha,
    )
    next_index, previous_receipt_sha, eligible = _scan_receipts(
        paths, authorization=authorization,
        authorization_sha256=authorization_sha, schedule=schedule,
        schedule_sha256=schedule_sha, manifest=manifest,
        manifest_sha256=manifest_sha,
        horizon_receipt_sha256=horizon_receipt_sha,
        claim_sha256=claim_sha,
        require_root_owned=require_root_owned,
    )
    if eligible is not None:
        index = eligible["window_index"]
    else:
        index = next_index
    if index >= HORIZON_WINDOWS:
        raise HorizonRefusal("no_next_window", "all 144 windows completed")
    item = schedule["windows"][index]
    window = window_paths(paths, index, item["exact_window_start_epoch"])
    request, request_sha = read_canonical_object(window.request, require_root_owned=require_root_owned)
    expected_previous_receipt_sha = (
        eligible.get("previous_window_receipt_sha256")
        if eligible
        else previous_receipt_sha
    )
    expected_request = _request_payload(
        authorization_sha256=authorization_sha, schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha, horizon_receipt_sha256=horizon_receipt_sha,
        claim_sha256=claim_sha, index=index, start=item["exact_window_start_epoch"],
        previous_window_receipt_sha256=expected_previous_receipt_sha,
        requested_at_epoch_millis=_integer(request.get("requested_at_epoch_millis"), "requested_at"),
    )
    causal_floor = claim["claimed_at_epoch_millis"]
    if index > 0:
        prior_item = schedule["windows"][index - 1]
        prior_window = window_paths(
            paths,
            index - 1,
            prior_item["exact_window_start_epoch"],
        )
        prior_receipt, prior_receipt_sha = read_canonical_object(
            prior_window.window_receipt,
            require_root_owned=require_root_owned,
        )
        if prior_receipt_sha != expected_previous_receipt_sha:
            raise HorizonRefusal(
                "strict_next_previous_receipt_invalid",
                "request predecessor differs from the scanned cash boundary",
            )
        causal_floor = max(
            causal_floor,
            _integer(
                prior_receipt.get("completed_at_epoch_millis"),
                "previous_receipt.completed_at_epoch_millis",
            ),
        )
    requested_at = request["requested_at_epoch_millis"]
    if (
        request != expected_request
        or not causal_floor <= requested_at < item["exact_window_start_epoch"] * 1000
    ):
        raise HorizonRefusal(
            "strict_next_request_invalid",
            "request is not causally derived from the exact next cash boundary",
        )
    expected_child = build_child(
        authorization=authorization, authorization_sha256=authorization_sha,
        schedule=schedule, manifest=manifest, manifest_sha256=manifest_sha,
        horizon_receipt_sha256=horizon_receipt_sha, index=index,
    )
    if _artifact_present(window.child_authorization):
        child, child_sha = read_canonical_object(
            window.child_authorization, require_root_owned=require_root_owned
        )
        if child != expected_child:
            raise HorizonRefusal("child_transplant_or_drift", "child differs from exact manifest projection")
    else:
        child = expected_child
        child_sha = _write_exclusive_json(window.child_authorization, child)
    return (
        authorization, authorization_sha, schedule, schedule_sha, manifest, manifest_sha,
        paths, window, request, request_sha, child, child_sha,
    )


def prepare_next(
    *,
    files: ContractFiles = ContractFiles(
        CANONICAL_AUTHORIZATION, CANONICAL_SCHEDULE, CANONICAL_MANIFEST, CANONICAL_HORIZON_RECEIPT
    ),
    root: Path = CANONICAL_ROOT,
    now_seconds: Callable[[], float] = time.time,
    wait: Callable[[float], None] | None = None,
    collector_factory: Callable[..., Any] | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    if require_root and (
        files != ContractFiles(CANONICAL_AUTHORIZATION, CANONICAL_SCHEDULE, CANONICAL_MANIFEST, CANONICAL_HORIZON_RECEIPT)
        or root != CANONICAL_ROOT or now_seconds is not time.time or wait is not None
        or collector_factory is not None
    ):
        raise HorizonRefusal("production_override_forbidden", "prepare production inputs drifted")
    if require_root and os.geteuid() != 0:
        raise HorizonRefusal("production_owner_invalid", "prepare requires root")
    if require_root:
        _validate_production_surface(
            files=files,
            root=root,
            require_runtime=True,
            require_systemctl=True,
        )
        _validate_process_environment(credentials_permitted=False)
    v274 = _load_v274(require_root_owned=require_root)
    (
        authorization, authorization_sha, schedule, schedule_sha, manifest, manifest_sha,
        paths, window, request, request_sha, child, child_sha,
    ) = _verify_request_and_child(files=files, root=root, require_root_owned=require_root)
    if (
        _artifact_present(paths.delegation)
        or _artifact_present(paths.execute_invocation)
        or _artifact_present(paths.attempt)
        or _artifact_present(paths.terminal)
    ):
        raise HorizonRefusal(
            "horizon_already_terminal",
            "prepare cannot run after delegation/invocation/attempt/terminal",
        )
    start = child["exact_window_start_epoch"]
    started_ms = int(now_seconds() * 1000)
    if started_ms >= start * 1000:
        raise HorizonRefusal("prepare_not_future", "prepare must begin before exact child start")
    gate, qualification_manifest_sha = _validate_qualification(v274, authorization, start)
    live_definition = v274.build_live_signal_definition(start)
    fields = _projection_fields(v274, child, live_definition)
    projection_receipt = _projection_receipt(
        v274, fields, child_sha, request["requested_at_epoch_millis"]
    )
    projection_receipt_sha = _write_exclusive_json(
        window.v274_projection_receipt,
        projection_receipt,
    )
    anchor = {
        "schema": v274.QUALIFICATION_ANCHOR_SCHEMA,
        "orchestrator_version": v274.VERSION,
        "authorization_sha256": child_sha,
        "conditional_receipt_sha256": projection_receipt_sha,
        "qualification_manifest_schema": v274.QUALIFICATION_MANIFEST_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha,
        "manifest_sha256": manifest_sha,
        "horizon_receipt_sha256": child["horizon_receipt_sha256"],
        "derived_child_authorization_sha256": child_sha,
        "window_index": child["window_index"],
        "qualification_experiment_id": fields["qualification_experiment_id"],
        "qualification_report_sha256": fields["qualification_report_sha256"],
        "qualification_report_artifact_sha256": fields["qualification_report_artifact_sha256"],
        "qualification_paper_ledger_sha256": fields["qualification_paper_ledger_sha256"],
        "qualification_panel_ledger_sha256": fields["qualification_panel_ledger_sha256"],
        "qualification_forward_release_sha256": fields["qualification_forward_release_sha256"],
        "qualification_collector_release_sha256": fields["qualification_collector_release_sha256"],
        "qualification_known_contract_sha256": fields["qualification_known_contract_sha256"],
        "qualification_manifest_sha256": qualification_manifest_sha,
        "live_panel_validator_release_sha256": V2733_VALIDATOR_SHA256,
        "live_signal_run_id": fields["live_signal_run_id"],
        "live_signal_definition_sha256": fields["live_signal_definition_sha256"],
        "v271_live_experiment_id": fields["v271_live_experiment_id"],
        "exact_window_start_epoch": start,
        "exact_window_end_epoch": start + WINDOW_SECONDS,
        "qualification_snapshot": gate,
        "anchored_at_epoch_millis": started_ms,
        "paper_only": True,
        "orders": "none",
        "live_ready": False,
    }
    anchor_sha = _write_exclusive_json(window.qualification_anchor, anchor)
    row, live_artifact, live_artifact_sha = v274.collect_live_signal_once(
        paths=_v274_paths(v274, window),
        live_definition=live_definition,
        now=now_seconds,
        wait=wait,
        collector_factory=collector_factory or v274.producer.NearSettlementCollector,
    )
    common = _receipt_common(
        authorization_sha256=authorization_sha, schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha, horizon_receipt_sha256=child["horizon_receipt_sha256"],
        request_sha256=request_sha, child_sha256=child_sha, child=child,
        previous_window_receipt_sha256=request["previous_window_receipt_sha256"],
    )
    try:
        if row.get("panel_valid") is not True:
            raise v274.CashWindow(
                f"live_panel_cash:{str(row.get('support_reason') or 'invalid')[:140]}",
                "complete live panel chose cash",
            )
        _live, live_fields = v274._recompute_live_execution_fields(
            row=row, live_definition=live_definition
        )
    except v274.CashWindow as exc:
        receipt = {
            **common,
            "completed": True,
            "action": "cash",
            "cash_reason": exc.code,
            "qualification_anchor_sha256": anchor_sha,
            "live_signal_row_artifact_sha256": live_artifact_sha,
            "live_panel_record_sha256": row.get("record_sha256"),
            "completed_at_epoch_millis": int(now_seconds() * 1000),
            "preauthorization_consumed": False,
            "signed": False,
            "post_attempts": 0,
            "orders": "none",
        }
        _write_exclusive_json(window.window_receipt, receipt)
        return receipt
    materialized = int(now_seconds() * 1000)
    if not (
        live_fields["execute_not_before_epoch_millis"] <= materialized
        <= live_fields["execute_not_after_epoch_millis"]
    ):
        raise HorizonRefusal("eligible_materialization_outside_band", "eligible bundle was not materialized in entry band")
    bindings = {
        "strategy_id": STRATEGY_ID,
        "authorization_sha256": child_sha,
        "conditional_receipt_sha256": projection_receipt_sha,
        "parent_horizon_authorization_sha256": authorization_sha,
        "horizon_manifest_sha256": manifest_sha,
        "horizon_receipt_sha256": child["horizon_receipt_sha256"],
        "public_execution_identity_artifact_sha256": authorization[
            "public_execution_identity_artifact_sha256"
        ],
        "window_index": child["window_index"],
        "qualification_experiment_id": fields["qualification_experiment_id"],
        "qualification_report_sha256": fields["qualification_report_sha256"],
        "qualification_report_artifact_sha256": fields["qualification_report_artifact_sha256"],
        "qualification_paper_ledger_sha256": fields["qualification_paper_ledger_sha256"],
        "qualification_panel_ledger_sha256": fields["qualification_panel_ledger_sha256"],
        "qualification_forward_release_sha256": fields["qualification_forward_release_sha256"],
        "qualification_collector_release_sha256": fields["qualification_collector_release_sha256"],
        "qualification_known_contract_sha256": fields["qualification_known_contract_sha256"],
        "qualification_manifest_sha256": fields["qualification_manifest_sha256"],
        "live_panel_validator_release_sha256": fields["live_panel_validator_release_sha256"],
        "qualification_anchor_sha256": anchor_sha,
        "live_signal_run_id": fields["live_signal_run_id"],
        "live_signal_definition_sha256": fields["live_signal_definition_sha256"],
        "v271_live_experiment_id": fields["v271_live_experiment_id"],
        "live_signal_row_artifact_sha256": live_artifact_sha,
        "live_panel_record_sha256": row["record_sha256"],
        "exact_window_start_epoch": start,
        "exact_window_end_epoch": start + WINDOW_SECONDS,
    }
    candidate = {
        "schema": v274.CANDIDATE_SCHEMA,
        "orchestrator_version": v274.VERSION,
        **bindings,
        **live_fields,
        "materialized_at_epoch_millis": materialized,
        "expected_signer": authorization["expected_signer"],
        "expected_funder": authorization["expected_funder"],
        "signature_type": authorization["signature_type"],
        "venue_fees_enabled": True,
        "fee_rate": FEE_RATE,
        "paper_only": True,
        "orders": "none",
        "live_ready": False,
    }
    _validate_account_identity_chain(
        authorization=authorization,
        child=child,
        candidate=candidate,
    )
    candidate_sha = canonical_sha256(candidate)
    candidate_artifact_sha = _write_exclusive_json(window.candidate, candidate)
    permit = {
        "schema": v274.PERMIT_SCHEMA,
        "orchestrator_version": v274.VERSION,
        **bindings,
        "candidate_sha256": candidate_sha,
        "candidate_artifact_sha256": candidate_artifact_sha,
        "paper_only": True,
        "orders": "none",
        "live_armed": False,
        "maximum_live_attempts": 1,
        "transport_retry_enabled": False,
        "side": "BUY",
        "order_type": "FOK",
        "maximum_total_reserve_usdc": MAXIMUM_TOTAL_RESERVE_USDC,
        "maximum_total_reserve_micro": 2_500_000,
        "exact_shares": EXACT_SHARES,
        "exact_taker_amount_micro": 5_000_000,
        "entry_stress_cents": ENTRY_STRESS_CENTS,
        "execution_limit_price_cap": EXECUTION_LIMIT_PRICE_CAP,
        "fee_rate": FEE_RATE,
        "cash_consumes_preauthorization": False,
        "one_attempt_no_retry": True,
        **{key: candidate[key] for key in (
            "slug", "direction", "market", "token_id", "neg_risk", "tick_size",
            "first_entry_ask", "limit_price", "signal_target_epoch_millis",
            "first_signal_receipt_epoch_millis", "alpha_frozen_at_epoch_millis",
            "entry_receipt_epoch_millis", "execute_not_before_epoch_millis",
            "execute_not_after_epoch_millis", "materialized_at_epoch_millis",
            "expected_signer", "expected_funder", "signature_type", "venue_fees_enabled",
        )},
        "mode": "disabled_until_explicit_execute_once",
    }
    _validate_account_identity_chain(
        authorization=authorization,
        child=child,
        candidate=candidate,
        permit=permit,
    )
    permit_artifact_sha = _write_exclusive_json(window.permit, permit)
    # Full frozen verifier, with a deterministic derived projection receipt.
    v274.verify_candidate_and_permit(
        authorization_fields=fields,
        authorization_sha256=child_sha,
        receipt=projection_receipt,
        receipt_sha256=projection_receipt_sha,
        qualification_anchor=anchor,
        qualification_anchor_sha256=anchor_sha,
        live_definition=live_definition,
        live_signal_artifact=live_artifact,
        live_signal_row_sha256=live_artifact_sha,
        candidate=candidate,
        candidate_artifact_sha256=candidate_artifact_sha,
        permit=permit,
    )
    receipt = {
        **common,
        "completed": True,
        "action": "eligible",
        "cash_reason": None,
        "qualification_anchor_sha256": anchor_sha,
        "live_signal_row_artifact_sha256": live_artifact_sha,
        "live_panel_record_sha256": row["record_sha256"],
        "candidate_artifact_sha256": candidate_artifact_sha,
        "permit_artifact_sha256": permit_artifact_sha,
        "limit_price": permit["limit_price"],
        "completed_at_epoch_millis": int(now_seconds() * 1000),
        "preauthorization_consumed": False,
        "signed": False,
        "post_attempts": 0,
        "orders": "none",
    }
    _write_exclusive_json(window.window_receipt, receipt)
    return receipt


def _claim_execute_invocation(
    *,
    files: ContractFiles,
    root: Path,
    invoked_at_epoch_millis: int,
    require_root_owned: bool,
) -> tuple[dict[str, Any], str]:
    """Consume the sole execute-unit invocation before importing v274."""

    (
        authorization,
        authorization_sha,
        schedule,
        schedule_sha,
        manifest,
        manifest_sha,
        _horizon_receipt,
        horizon_receipt_sha,
    ) = load_contract(files, require_root_owned=require_root_owned)
    paths = horizon_paths(root, authorization_sha)
    _ensure_exact_directory(
        paths.run_dir,
        require_root_owned=require_root_owned,
    )
    claim, claim_sha = read_canonical_object(
        paths.claim,
        require_root_owned=require_root_owned,
    )
    _validate_claim(
        claim,
        authorization=authorization,
        authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha,
        horizon_receipt_sha256=horizon_receipt_sha,
    )
    if (
        _artifact_present(paths.terminal)
        or _artifact_present(paths.transport)
        or _artifact_present(paths.attempt)
    ):
        raise HorizonRefusal(
            "execute_invocation_already_terminal",
            "execute invocation cannot begin after terminal/transport/attempt",
        )
    eligible_index, eligible_receipt_sha, eligible_receipt = _scan_receipts(
        paths,
        authorization=authorization,
        authorization_sha256=authorization_sha,
        schedule=schedule,
        schedule_sha256=schedule_sha,
        manifest=manifest,
        manifest_sha256=manifest_sha,
        horizon_receipt_sha256=horizon_receipt_sha,
        claim_sha256=claim_sha,
        require_root_owned=require_root_owned,
    )
    if eligible_receipt is None or eligible_receipt.get("action") != "eligible":
        raise HorizonRefusal(
            "execute_invocation_without_eligible",
            "execute invocation requires the exact first eligible receipt",
        )
    eligible_receipt_sha = _hash(
        eligible_receipt_sha,
        "eligible_receipt_sha256",
    )
    item = schedule["windows"][eligible_index]
    window = window_paths(
        paths,
        eligible_index,
        item["exact_window_start_epoch"],
    )
    child, child_sha = read_canonical_object(
        window.child_authorization,
        require_root_owned=require_root_owned,
    )
    expected_child = build_child(
        authorization=authorization,
        authorization_sha256=authorization_sha,
        schedule=schedule,
        manifest=manifest,
        manifest_sha256=manifest_sha,
        horizon_receipt_sha256=horizon_receipt_sha,
        index=eligible_index,
    )
    if child != expected_child:
        raise HorizonRefusal(
            "execute_invocation_child_invalid",
            "execute invocation child differs from the sealed projection",
        )
    _permit, permit_sha = read_canonical_object(
        window.permit,
        require_root_owned=require_root_owned,
    )
    delegation, delegation_sha = read_canonical_object(
        paths.delegation,
        require_root_owned=require_root_owned,
    )
    _validate_delegation(
        delegation,
        authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha,
        schedule=schedule,
        expected_window_receipt_sha256=eligible_receipt_sha,
        expected_window_receipt_completed_at_epoch_millis=eligible_receipt.get(
            "completed_at_epoch_millis"
        ),
        expected_child_sha256=child_sha,
        expected_permit_sha256=permit_sha,
    )
    delegated_at = _integer(
        delegation.get("delegated_at_epoch_millis"),
        "delegation.delegated_at_epoch_millis",
    )
    invoked_at = _integer(
        invoked_at_epoch_millis,
        "invoked_at_epoch_millis",
    )
    if not delegated_at <= invoked_at < item["exact_window_end_epoch"] * 1000:
        raise HorizonRefusal(
            "execute_invocation_clock_invalid",
            "execute invocation is not causally after delegation in its exact window",
        )
    # Re-read the exact external account anchor at the last boundary before
    # consuming the sole execute-unit invocation.
    _revalidate_execution_public_identity(
        authorization,
        require_root_owned=require_root_owned,
    )
    invocation = {
        "schema": EXECUTE_INVOCATION_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha,
        "schedule_sha256": schedule_sha,
        "manifest_sha256": manifest_sha,
        "consuming_delegation_sha256": delegation_sha,
        "window_receipt_sha256": eligible_receipt_sha,
        "derived_child_authorization_sha256": child_sha,
        "permit_artifact_sha256": permit_sha,
        "window_index": eligible_index,
        "exact_window_start_epoch": item["exact_window_start_epoch"],
        "exact_window_end_epoch": item["exact_window_end_epoch"],
        "execute_unit_invocation": 1,
        "maximum_execute_unit_invocations": 1,
        "invoked_at_epoch_millis": invoked_at,
        "retry_permitted": False,
        "horizon_terminal_on_creation": True,
    }
    invocation_sha = _write_exclusive_json(
        paths.execute_invocation,
        invocation,
    )
    return invocation, invocation_sha


def _load_v275_bundle_for_execute(
    *,
    files: ContractFiles, root: Path, require_root_owned: bool,
) -> tuple[Any, dict[str, Any], str, HorizonPaths, WindowPaths, dict[str, Any], str, dict[str, Any], str]:
    v274 = _load_v274(require_root_owned=require_root_owned)
    (
        authorization, authorization_sha, schedule, schedule_sha, manifest, manifest_sha,
        paths, window, request, request_sha, child, child_sha,
    ) = _verify_request_and_child(files=files, root=root, require_root_owned=require_root_owned)
    if not _artifact_present(paths.delegation):
        raise HorizonRefusal("delegation_missing", "execute requires durable coordinator delegation")
    delegation, _ = read_canonical_object(paths.delegation, require_root_owned=require_root_owned)
    receipt, receipt_sha = read_canonical_object(window.window_receipt, require_root_owned=require_root_owned)
    delegated_index, delegated_start = _validate_delegation(
        delegation,
        authorization_sha256=authorization_sha,
        schedule_sha256=schedule_sha,
        manifest_sha256=manifest_sha,
        schedule=schedule,
        expected_window_receipt_sha256=receipt_sha,
        expected_window_receipt_completed_at_epoch_millis=receipt.get(
            "completed_at_epoch_millis"
        ),
        expected_child_sha256=child_sha,
        expected_permit_sha256=receipt.get("permit_artifact_sha256"),
    )
    if (
        delegated_index != child["window_index"]
        or delegated_start != child["exact_window_start_epoch"]
        or receipt.get("action") != "eligible"
        or receipt.get("derived_child_authorization_sha256") != child_sha
        or receipt.get("strict_next_request_sha256") != request_sha
        or receipt.get("child_commitment_sha256") != child["child_commitment_sha256"]
        or receipt.get("parent_horizon_authorization_sha256") != authorization_sha
        or receipt.get("manifest_sha256") != manifest_sha
        or receipt.get("schedule_sha256") != schedule_sha
        or receipt.get("window_index") != child["window_index"]
        or receipt.get("exact_window_start_epoch") != child["exact_window_start_epoch"]
        or receipt.get("exact_window_end_epoch") != child["exact_window_end_epoch"]
    ):
        raise HorizonRefusal("eligible_delegation_mismatch", "delegation does not bind first eligible receipt")
    live_definition, _ = v274.read_canonical_object(window.live_definition, require_root_owned=require_root_owned)
    fields = _projection_fields(v274, child, live_definition)
    expected_projection_receipt = _projection_receipt(
        v274,
        fields,
        child_sha,
        request["requested_at_epoch_millis"],
    )
    projection_receipt, projection_receipt_sha = v274.read_canonical_object(
        window.v274_projection_receipt,
        require_root_owned=require_root_owned,
    )
    if projection_receipt != expected_projection_receipt:
        raise HorizonRefusal(
            "v274_projection_receipt_invalid",
            "persisted projection receipt differs from deterministic child projection",
        )
    anchor, anchor_sha = v274.read_canonical_object(window.qualification_anchor, require_root_owned=require_root_owned)
    live_artifact, live_sha = v274.read_canonical_object(window.live_signal_row, require_root_owned=require_root_owned)
    candidate, candidate_sha = v274.read_canonical_object(window.candidate, require_root_owned=require_root_owned)
    permit, permit_sha = v274.read_canonical_object(window.permit, require_root_owned=require_root_owned)
    _validate_account_identity_chain(
        authorization=authorization,
        child=child,
        candidate=candidate,
        permit=permit,
    )
    if delegation.get("permit_artifact_sha256") != permit_sha:
        raise HorizonRefusal("delegated_permit_mismatch", "delegation binds another permit")
    # Required full frozen verifier immediately before the global attempt claim.
    v274.verify_candidate_and_permit(
        authorization_fields=fields,
        authorization_sha256=child_sha,
        receipt=projection_receipt,
        receipt_sha256=projection_receipt_sha,
        qualification_anchor=anchor,
        qualification_anchor_sha256=anchor_sha,
        live_definition=live_definition,
        live_signal_artifact=live_artifact,
        live_signal_row_sha256=live_sha,
        candidate=candidate,
        candidate_artifact_sha256=candidate_sha,
        permit=permit,
    )
    return v274, authorization, authorization_sha, paths, window, child, child_sha, permit, permit_sha


def execute_first_eligible(
    *,
    files: ContractFiles = ContractFiles(
        CANONICAL_AUTHORIZATION, CANONICAL_SCHEDULE, CANONICAL_MANIFEST, CANONICAL_HORIZON_RECEIPT
    ),
    root: Path = CANONICAL_ROOT,
    now_epoch_millis: Callable[[], int] | None = None,
    runtime: Any | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    if require_root and (
        files != ContractFiles(CANONICAL_AUTHORIZATION, CANONICAL_SCHEDULE, CANONICAL_MANIFEST, CANONICAL_HORIZON_RECEIPT)
        or root != CANONICAL_ROOT or now_epoch_millis is not None or runtime is not None
    ):
        raise HorizonRefusal("production_override_forbidden", "execute production inputs drifted")
    if require_root and os.geteuid() != 0:
        raise HorizonRefusal("production_owner_invalid", "execute requires root")
    if require_root:
        _validate_production_surface(
            files=files,
            root=root,
            require_runtime=True,
            require_systemctl=True,
        )
        _validate_process_environment(credentials_permitted=True)
        # Consume no execute invocation until the credential-bearing unit has
        # proved the exact metadata boundary.  The secret bytes are never read,
        # hashed, logged, or copied by v275.
        _validate_live_environment_identity()
    clock = now_epoch_millis or (lambda: time.time_ns() // 1_000_000)
    _invocation, invocation_sha = _claim_execute_invocation(
        files=files,
        root=root,
        invoked_at_epoch_millis=clock(),
        require_root_owned=require_root,
    )
    v274, authorization, authorization_sha, paths, window, child, child_sha, permit, permit_sha = (
        _load_v275_bundle_for_execute(
            files=files, root=root, require_root_owned=require_root
        )
    )
    persisted_invocation, persisted_invocation_sha = read_canonical_object(
        paths.execute_invocation,
        require_root_owned=require_root,
    )
    if (
        persisted_invocation != _invocation
        or persisted_invocation_sha != invocation_sha
    ):
        raise HorizonRefusal(
            "execute_invocation_identity_invalid",
            "pre-verifier execute invocation claim bytes changed",
        )
    now_ms = clock()
    if not (
        persisted_invocation["invoked_at_epoch_millis"] <= now_ms
        and permit["execute_not_before_epoch_millis"] <= now_ms
        <= permit["execute_not_after_epoch_millis"]
    ):
        raise HorizonRefusal(
            "execute_time_invalid",
            "attempt claim predates invocation or full verification finished outside entry band",
        )
    root_receipt: dict[str, Any] | None = None
    if require_root:
        root_receipt, _ = read_canonical_object(
            files.horizon_receipt,
            require_root_owned=True,
        )
        _validate_shared_lock_identity(
            root_receipt,
            require_root_owned=True,
        )
    if (
        _artifact_present(paths.terminal)
        or _artifact_present(paths.transport)
        or _artifact_present(paths.attempt)
    ):
        raise HorizonRefusal("global_attempt_already_terminal", "global attempt/terminal already exists")
    # A replacement after prepare/delegation/invocation must stop before the
    # globally consuming attempt claim exists.
    _revalidate_execution_public_identity(
        authorization,
        require_root_owned=require_root,
    )
    attempt = {
        "schema": ATTEMPT_SCHEMA,
        "parent_horizon_authorization_sha256": authorization_sha,
        "derived_child_authorization_sha256": child_sha,
        "window_index": child["window_index"],
        "permit_artifact_sha256": permit_sha,
        "execute_invocation_sha256": invocation_sha,
        "maximum_post_attempts": 1,
        "execute_once_invocations": 1,
        "transport_retry_enabled": False,
        "horizon_terminal_on_creation": True,
        "claimed_at_epoch_millis": now_ms,
    }
    attempt_sha = _write_exclusive_json(paths.attempt, attempt)
    stage: dict[str, Any]
    refusal: str | None = None
    try:
        # Once the attempt exists, identity drift is conservatively terminal
        # unknown, but must still refuse before frozen execute_once can POST.
        _revalidate_execution_public_identity(
            authorization,
            require_root_owned=require_root,
        )
        if root_receipt is not None:
            # The global attempt now makes every later outcome conservative
            # unknown.  Recheck the sealed inode once more immediately before
            # entering unchanged v274.execute_once.
            _validate_shared_lock_identity(
                root_receipt,
                require_root_owned=True,
            )
        result = v274.execute_once(
            paths=_v274_paths(v274, window),
            permit=permit,
            permit_artifact_sha256=permit_sha,
            now_epoch_millis=now_epoch_millis,
            runtime=runtime,
        )
        stage = v274.durable_execution_stage(_v274_paths(v274, window))
        result_sha = hashlib.sha256(v274.canonical_bytes(result, newline=True)).hexdigest()
    except Exception as exc:
        refusal = exc.code if hasattr(exc, "code") else type(exc).__name__
        stage = v274.durable_execution_stage(_v274_paths(v274, window))
        result_sha = None
    wrapper = {
        "schema": EXECUTE_WRAPPER_SCHEMA,
        "version": VERSION,
        "parent_horizon_authorization_sha256": authorization_sha,
        "derived_child_authorization_sha256": child_sha,
        "window_index": child["window_index"],
        "global_attempt_sha256": attempt_sha,
        "execute_invocation_sha256": invocation_sha,
        "permit_artifact_sha256": permit_sha,
        "execution_result_sha256": result_sha,
        "refusal": refusal,
        **stage,
        "retry_permitted": False,
        "completed_at_epoch_millis": clock(),
    }
    _write_exclusive_json(window.execute_wrapper_result, wrapper)
    return wrapper


def register_horizon_receipt(
    *,
    authorization_path: Path,
    schedule_path: Path,
    manifest_path: Path,
    output_path: Path,
    now_epoch_millis: int | None = None,
    require_root: bool = True,
) -> tuple[dict[str, Any], str]:
    if require_root and (now_epoch_millis is not None or os.geteuid() != 0):
        raise HorizonRefusal("registration_override_forbidden", "production registration uses root/wall clock")
    if require_root:
        for parent in (
            authorization_path.parent,
            schedule_path.parent,
            manifest_path.parent,
            output_path.parent,
            Path(__file__).resolve().parent,
        ):
            _require_trusted_directory_chain(parent)
    schedule, _ = read_canonical_object(schedule_path, require_root_owned=require_root)
    authorization, authorization_sha = read_canonical_object(authorization_path, require_root_owned=require_root)
    validate_authorization(authorization, schedule=schedule, expected_release_sha256=release_sha256())
    if require_root:
        _validate_installed_public_execution_identity(
            authorization,
            require_root_owned=True,
            require_canonical_path=True,
        )
        _validate_installed_release_contract(authorization)
    manifest, manifest_sha = read_canonical_object(manifest_path, require_root_owned=require_root)
    validate_manifest(
        manifest, authorization=authorization, authorization_sha256=authorization_sha,
        schedule=schedule,
    )
    registered = time.time_ns() // 1_000_000 if now_epoch_millis is None else now_epoch_millis
    if not (
        max(
            authorization["authorized_at_epoch_millis"],
            manifest["manifest_created_at_epoch_millis"],
        )
        <= registered
        < authorization["horizon_start_epoch"] * 1000
    ):
        raise HorizonRefusal(
            "registration_clock_invalid",
            "horizon receipt must be causally registered after the manifest and pre-start",
        )
    lock_identity = _shared_lock_identity(
        require_root_owned=require_root,
    )
    systemd_contract_sha = canonical_sha256(effective_systemd_contract())
    receipt = build_horizon_receipt(
        authorization_sha256=authorization_sha,
        schedule_sha256=canonical_sha256(schedule),
        manifest_sha256=manifest_sha,
        release_contract_sha256=authorization["release_contract_sha256"],
        public_execution_identity_artifact_sha256=authorization[
            "public_execution_identity_artifact_sha256"
        ],
        shared_execution_lock_device=lock_identity[
            "shared_execution_lock_device"
        ],
        shared_execution_lock_inode=lock_identity[
            "shared_execution_lock_inode"
        ],
        shared_execution_lock_group_gid=lock_identity[
            "shared_execution_lock_group_gid"
        ],
        effective_systemd_contract_sha256=systemd_contract_sha,
        registered_at_epoch_millis=registered,
    )
    return receipt, _write_exclusive_json(output_path, receipt)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("provision-public-identity-from-v274")
    schedule = commands.add_parser("schedule")
    schedule.add_argument("--horizon-start", type=int, required=True)
    authorization = commands.add_parser("authorization")
    authorization.add_argument("--schedule", type=Path, required=True)
    authorization.add_argument("--authorized-at-epoch-millis", type=int, required=True)
    authorization.add_argument("--expected-signer", required=True)
    authorization.add_argument("--expected-funder", required=True)
    authorization.add_argument("--signature-type", type=int, required=True)
    authorization.add_argument("--qualification-manifest-sha256", required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--authorization", type=Path, required=True)
    manifest.add_argument("--schedule", type=Path, required=True)
    manifest.add_argument("--created-at-epoch-millis", type=int, required=True)
    register = commands.add_parser("register")
    register.add_argument("--authorization", type=Path, required=True)
    register.add_argument("--schedule", type=Path, required=True)
    register.add_argument("--manifest", type=Path, required=True)
    register.add_argument("--output", type=Path, required=True)
    commands.add_parser("coordinate")
    commands.add_parser("prepare-next")
    commands.add_parser("execute-first-eligible")
    return parser


def _durable_cli_failure_status(command: str | None, exc: BaseException) -> dict[str, Any]:
    """Never print a false zero after the irreversible delegation boundary."""
    refusal = exc.code if isinstance(exc, HorizonRefusal) else type(exc).__name__
    base: dict[str, Any] = {
        "schema": TERMINAL_SCHEMA,
        "version": VERSION,
        "completed": False,
        "action": "none",
        "refusal": refusal,
        "orders": "none",
        "post_attempts": 0,
        "retry_permitted": False,
    }
    if command == "execute-first-eligible":
        # This systemd unit is reachable only after the coordinator has fsynced
        # the irreversible consuming delegation.  Even verifier/refusal output
        # is conservative if the wrapper itself failed before durable summary.
        return {
            **base,
            "orders": "one_fok_submission_unknown",
            "post_attempts": 1,
            "submission_unknown": True,
        }
    if command != "coordinate":
        return base
    try:
        files = ContractFiles(
            CANONICAL_AUTHORIZATION,
            CANONICAL_SCHEDULE,
            CANONICAL_MANIFEST,
            CANONICAL_HORIZON_RECEIPT,
        )
        (
            authorization, authorization_sha, schedule, schedule_sha,
            _manifest, manifest_sha, _receipt, _receipt_sha,
        ) = load_contract(files, require_root_owned=True)
        paths = horizon_paths(CANONICAL_ROOT, authorization_sha)
        if _artifact_present(paths.terminal):
            terminal, _ = read_canonical_object(paths.terminal, require_root_owned=True)
            return _validate_terminal(
                terminal,
                authorization_sha256=authorization_sha,
                schedule_sha256=schedule_sha,
                manifest_sha256=manifest_sha,
            )
        if (
            _artifact_present(paths.delegation)
            or _artifact_present(paths.execute_invocation)
            or _artifact_present(paths.attempt)
            or _artifact_present(paths.transport)
        ):
            return {
                **base,
                "parent_horizon_authorization_sha256": authorization_sha,
                "schedule_sha256": schedule_sha,
                "manifest_sha256": manifest_sha,
                "orders": "one_fok_submission_unknown",
                "post_attempts": 1,
                "submission_unknown": True,
            }
        window_stage = _first_window_execution_stage_artifact(
            paths,
            schedule,
        )
        if window_stage is not None:
            return {
                **base,
                "parent_horizon_authorization_sha256": authorization_sha,
                "schedule_sha256": schedule_sha,
                "manifest_sha256": manifest_sha,
                "orders": "one_fok_submission_unknown",
                "post_attempts": 1,
                "submission_unknown": True,
                **window_stage,
            }
        # With a valid contract and no consuming delegation/attempt/transport,
        # coordinator has no network and cannot have posted.
        return base
    except Exception as recovery_exc:
        # Contract corruption may itself have happened after delegation.  A
        # coordinator recovery that cannot prove the pre-delegation boundary
        # must not emit zero.
        return {
            **base,
            "orders": "one_fok_submission_unknown",
            "post_attempts": 1,
            "submission_unknown": True,
            "recovery_refusal": (
                recovery_exc.code
                if isinstance(recovery_exc, HorizonRefusal)
                else type(recovery_exc).__name__
            ),
        }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        print(json.dumps({
            "schema": "btc5m-v275-default-status-v1", "version": VERSION,
            "default_action": "none", "orders": "none", "post_attempts": 0,
            "coordinator_imports_broker": False,
        }, sort_keys=True))
        return 0
    try:
        if args.command == "provision-public-identity-from-v274":
            identity, digest = provision_public_execution_identity_from_v274()
            result = {
                "mode": "v275_public_execution_identity_provisioned",
                "public_execution_identity": identity,
                "public_execution_identity_artifact_sha256": digest,
                "credentials_read": False,
                "orders": "none",
                "post_attempts": 0,
            }
        elif args.command == "schedule":
            result = build_schedule(args.horizon_start)
        elif args.command == "authorization":
            schedule, _ = read_canonical_object(
                args.schedule,
                require_root_owned=False,
            )
            public_identity, public_identity_sha = (
                _read_public_execution_identity(
                    require_root_owned=True,
                    require_canonical_path=True,
                )
            )
            supplied_identity = build_public_execution_identity(
                expected_signer=args.expected_signer,
                expected_funder=args.expected_funder,
                signature_type=args.signature_type,
            )
            if supplied_identity != public_identity:
                raise HorizonRefusal(
                    "authorization_public_identity_mismatch",
                    "authorization CLI tuple differs from the installed public identity",
                )
            result = build_authorization(
                schedule=schedule,
                authorized_at_epoch_millis=args.authorized_at_epoch_millis,
                expected_signer=args.expected_signer,
                expected_funder=args.expected_funder,
                signature_type=args.signature_type,
                public_execution_identity_artifact_sha256=(
                    public_identity_sha
                ),
                qualification_manifest_sha256=args.qualification_manifest_sha256,
            )
        elif args.command == "manifest":
            schedule, _ = read_canonical_object(args.schedule, require_root_owned=False)
            authorization, authorization_sha = read_canonical_object(args.authorization, require_root_owned=False)
            validate_authorization(authorization, schedule=schedule, expected_release_sha256=release_sha256())
            _validate_installed_public_execution_identity(
                authorization,
                require_root_owned=True,
                require_canonical_path=True,
            )
            result = build_manifest(
                authorization, authorization_sha, schedule,
                created_at_epoch_millis=args.created_at_epoch_millis,
            )
        elif args.command == "register":
            receipt, digest = register_horizon_receipt(
                authorization_path=args.authorization, schedule_path=args.schedule,
                manifest_path=args.manifest, output_path=args.output,
            )
            result = {"receipt": receipt, "receipt_sha256": digest}
        elif args.command == "coordinate":
            result = coordinate()
        elif args.command == "prepare-next":
            result = prepare_next()
        elif args.command == "execute-first-eligible":
            result = execute_first_eligible()
        else:
            raise AssertionError("unknown command")
    except Exception as exc:
        result = _durable_cli_failure_status(args.command, exc)
        print(canonical_bytes(result).decode("ascii"))
        return 2
    print(canonical_bytes(result).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

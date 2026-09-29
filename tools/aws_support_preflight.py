#!/usr/bin/env python3
"""Check an isolated AWS support workspace; never run production workloads.

Only check names and booleans leave this client. SSH output is captured in
memory, validated, and discarded. This module uses only the Python standard
library and performs no work when imported.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile


SCHEMA = "aws-support-preflight/v1"
REMOTE_SCHEMA = "aws-support-remote/v1"
SUPPORT_USER = "poly-support"
WORKSPACE = "/srv/poly-chatgpt-support"
SECRET_NAMES = (
    "AWS_SUPPORT_HOST",
    "AWS_SUPPORT_USER",
    "AWS_SUPPORT_SSH_KEY",
    "AWS_SUPPORT_KNOWN_HOSTS",
)
REMOTE_CHECK_NAMES = (
    "workspace_is_directory",
    "workspace_is_owned",
    "workspace_private_mode",
    "workspace_writable",
    "sudo_denied",
    "fixture_written",
    "fixture_read",
    "fixture_removed",
)
RESPONSE_CHECK_NAMES = (
    "response_schema_valid",
    "remote_unprivileged_user",
    "remote_python_supported",
    *REMOTE_CHECK_NAMES,
    "fixture_echo_matches",
    "fixture_digest_matches",
)
CLIENT_CHECK_NAMES = (
    "required_inputs_present",
    "input_format_valid",
    "private_temporary_files",
    "ssh_completed_within_timeout",
    "ssh_exit_zero",
    *RESPONSE_CHECK_NAMES,
)


REMOTE_SOURCE = r'''
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

workspace = Path("/srv/poly-chatgpt-support")
checks = {name: False for name in (
    "workspace_is_directory", "workspace_is_owned", "workspace_private_mode",
    "workspace_writable", "sudo_denied", "fixture_written", "fixture_read",
    "fixture_removed",
)}
uid = os.getuid()
euid = os.geteuid()
username = pwd.getpwuid(euid).pw_name
supported = sys.version_info[:2] >= (3, 10)
identity_ok = uid == euid and uid > 0 and username == "poly-support"
echo = b""
fixture = None
if identity_ok and supported:
    try:
        info = workspace.lstat()
        checks["workspace_is_directory"] = stat.S_ISDIR(info.st_mode)
        checks["workspace_is_owned"] = info.st_uid == euid
        checks["workspace_private_mode"] = stat.S_IMODE(info.st_mode) == 0o700
        checks["workspace_writable"] = os.access(workspace, os.W_OK | os.X_OK)
    except OSError:
        pass
    try:
        sudo = subprocess.run(
            ["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
            check=False,
        )
        checks["sudo_denied"] = sudo.returncode > 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    if all(checks[name] for name in (
        "workspace_is_directory", "workspace_is_owned",
        "workspace_private_mode", "workspace_writable", "sudo_denied",
    )):
        try:
            fd, name = tempfile.mkstemp(prefix=".preflight-", dir=workspace)
            fixture = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())
            checks["fixture_written"] = True
            echo = fixture.read_bytes()
            checks["fixture_read"] = True
        except OSError:
            pass
        finally:
            if fixture is not None:
                try:
                    fixture.unlink()
                    checks["fixture_removed"] = not fixture.exists()
                except OSError:
                    pass
result = {
    "schema": "aws-support-remote/v1",
    "uid": uid,
    "effective_uid": euid,
    "username": username,
    "python_version": list(sys.version_info[:3]),
    "checks": checks,
    "echo_b64": base64.b64encode(echo).decode("ascii"),
    "echo_sha256": hashlib.sha256(echo).hexdigest(),
}
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
'''


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("nonfinite number")


def validate_remote_response(raw: bytes, expected: bytes) -> dict[str, bool]:
    """Validate a bounded response without returning any remote field values."""
    result = {name: False for name in RESPONSE_CHECK_NAMES}
    if not isinstance(raw, bytes) or len(raw) > 8192:
        return result
    if not isinstance(expected, bytes) or not 1 <= len(expected) <= 1024:
        return result
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        return result
    fields = {
        "schema", "uid", "effective_uid", "username", "python_version",
        "checks", "echo_b64", "echo_sha256",
    }
    if not isinstance(data, dict) or set(data) != fields:
        return result
    if data["schema"] != REMOTE_SCHEMA:
        return result
    remote_checks = data["checks"]
    if not isinstance(remote_checks, dict) or set(remote_checks) != set(REMOTE_CHECK_NAMES):
        return result
    if any(type(value) is not bool for value in remote_checks.values()):
        return result
    result["response_schema_valid"] = True
    uid, euid = data["uid"], data["effective_uid"]
    result["remote_unprivileged_user"] = (
        type(uid) is int and type(euid) is int
        and 0 < uid < 2**32 and uid == euid
        and data["username"] == SUPPORT_USER
    )
    version = data["python_version"]
    result["remote_python_supported"] = (
        isinstance(version, list) and len(version) == 3
        and all(type(part) is int and 0 <= part < 1000 for part in version)
        and tuple(version[:2]) >= (3, 10)
    )
    result.update(remote_checks)
    try:
        encoded = data["echo_b64"]
        if not isinstance(encoded, str) or len(encoded) > 2048:
            return result
        echoed = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError):
        return result
    result["fixture_echo_matches"] = hmac.compare_digest(echoed, expected)
    digest = data["echo_sha256"]
    result["fixture_digest_matches"] = (
        isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and hmac.compare_digest(digest, hashlib.sha256(echoed).hexdigest())
        and hmac.compare_digest(digest, hashlib.sha256(expected).hexdigest())
    )
    return result


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        labels = host.split(".")
        return (
            1 <= len(host) <= 253
            and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in labels)
        )


def _valid_pinned_hosts(text: str, host: str) -> bool:
    entries = [line.split() for line in text.splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    if not entries:
        return False
    for entry in entries:
        if len(entry) < 3 or entry[0] != host:
            return False
        if entry[1] not in {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}:
            return False
        try:
            if not base64.b64decode(entry[2], validate=True):
                return False
        except ValueError:
            return False
    return True


def _valid_key(text: str) -> bool:
    for kind in ("OPENSSH PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "PRIVATE KEY"):
        if text.startswith(f"-----BEGIN {kind}-----\n") and text.endswith(f"-----END {kind}-----"):
            return True
    return False


def _private_write(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value.rstrip("\n") + "\n")


def run_preflight(environ: dict[str, str] | os._Environ[str]) -> dict[str, bool]:
    """Attempt one bounded SSH probe. Never return output, identifiers or secrets."""
    result = {name: False for name in CLIENT_CHECK_NAMES}
    values = {name: environ.get(name, "").strip() for name in SECRET_NAMES}
    if not all(values.values()):
        return result
    result["required_inputs_present"] = True
    host, user, key, pins = (values[name] for name in SECRET_NAMES)
    key = key.replace("\r\n", "\n")
    if not (
        _valid_host(host) and user == SUPPORT_USER
        and _valid_key(key) and _valid_pinned_hosts(pins, host)
    ):
        return result
    result["input_format_valid"] = True
    expected = secrets.token_bytes(48)
    prefix = "import base64\nexpected = base64.b64decode(" + repr(base64.b64encode(expected).decode("ascii")) + ")\n"
    probe = (prefix + REMOTE_SOURCE).encode("utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="aws-support-preflight-") as directory:
            temporary = Path(directory)
            temporary.chmod(0o700)
            identity, known_hosts = temporary / "identity", temporary / "known_hosts"
            _private_write(identity, key)
            _private_write(known_hosts, pins)
            result["private_temporary_files"] = (
                stat.S_IMODE(temporary.stat().st_mode) == 0o700
                and stat.S_IMODE(identity.stat().st_mode) == 0o600
                and stat.S_IMODE(known_hosts.stat().st_mode) == 0o600
            )
            if not result["private_temporary_files"]:
                return result
            command = [
                "ssh", "-T", "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "ConnectTimeout=15", "-o", "ConnectionAttempts=1",
                "-i", str(identity), f"{user}@{host}", "python3", "-I", "-B", "-",
            ]
            completed = subprocess.run(
                command, input=probe, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=45, check=False,
                env={name: value for name, value in environ.items() if name not in SECRET_NAMES},
            )
            result["ssh_completed_within_timeout"] = True
            result["ssh_exit_zero"] = completed.returncode == 0
            result.update(validate_remote_response(completed.stdout, expected))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, help="Save booleans-only JSON locally; path must not already exist.")
    args = parser.parse_args()
    try:
        checks = run_preflight(os.environ)
    except Exception:
        checks = {name: False for name in CLIENT_CHECK_NAMES}
    receipt = {"schema": SCHEMA, "passed": all(checks.values()), "checks": checks}
    serialized = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if args.receipt is not None:
        try:
            _private_write(args.receipt, serialized)
        except (OSError, ValueError):
            print("receipt_saved failed")
            return 1
        print("receipt_saved passed")
    for name, passed in checks.items():
        print(f"{name} {'passed' if passed else 'failed'}")
    print(serialized)
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

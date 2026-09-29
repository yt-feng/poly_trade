"""Offline checks of authentication constraints and fail-closed response parsing."""

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


CLIENT_PATH = Path(__file__).resolve().parents[1] / "tools" / "aws_support_preflight.py"
SPEC = importlib.util.spec_from_file_location("aws_support_preflight", CLIENT_PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.expected = b"standalone-support-fixture\x00\xff"
        self.response = {
            "schema": probe.REMOTE_SCHEMA,
            "uid": 1001,
            "effective_uid": 1001,
            "username": "poly-support",
            "python_version": [3, 12, 3],
            "checks": {name: True for name in probe.REMOTE_CHECK_NAMES},
            "echo_b64": base64.b64encode(self.expected).decode("ascii"),
            "echo_sha256": hashlib.sha256(self.expected).hexdigest(),
        }

    def validate(self, response=None):
        raw = json.dumps(self.response if response is None else response).encode()
        return probe.validate_remote_response(raw, self.expected)

    def test_complete_valid_response_passes(self):
        self.assertTrue(all(self.validate().values()))

    def test_malformed_or_unbounded_response_is_rejected(self):
        for raw in (b"", b"not json", b"\xff", b"[]", b"null", b" " * 8193):
            with self.subTest(raw_length=len(raw)):
                result = probe.validate_remote_response(raw, self.expected)
                self.assertFalse(any(result.values()))

    def test_duplicate_fields_are_rejected(self):
        raw = json.dumps(self.response).replace('"uid": 1001', '"uid": 0, "uid": 1001').encode()
        self.assertFalse(any(probe.validate_remote_response(raw, self.expected).values()))

    def test_nonfinite_json_number_is_rejected(self):
        raw = json.dumps(self.response).replace('"uid": 1001', '"uid": NaN').encode()
        self.assertFalse(any(probe.validate_remote_response(raw, self.expected).values()))

    def test_missing_extra_or_wrong_schema_is_rejected(self):
        cases = []
        missing = copy.deepcopy(self.response)
        del missing["echo_sha256"]
        cases.append(missing)
        extra = copy.deepcopy(self.response)
        extra["unexpected"] = "must never be exposed"
        cases.append(extra)
        wrong = copy.deepcopy(self.response)
        wrong["schema"] = "other-schema"
        cases.append(wrong)
        for response in cases:
            with self.subTest(fields=list(response)):
                self.assertFalse(any(self.validate(response).values()))

    def test_root_mismatched_uid_and_boolean_uid_are_rejected(self):
        for uid, euid in ((0, 0), (1001, 0), (1001, 1002), (True, True), (-1, -1), (2**32, 2**32)):
            with self.subTest(uid=uid, euid=euid):
                self.response["uid"], self.response["effective_uid"] = uid, euid
                self.assertFalse(self.validate()["remote_unprivileged_user"])

    def test_other_account_is_rejected(self):
        self.response["username"] = "ubuntu"
        self.assertFalse(self.validate()["remote_unprivileged_user"])

    def test_unsupported_or_malformed_python_is_rejected(self):
        for version in ([3, 9, 99], [2, 10, 0], [3, 10], [3, True, 0], "3.12.3", [3, 12, -1]):
            with self.subTest(version=version):
                self.response["python_version"] = version
                self.assertFalse(self.validate()["remote_python_supported"])

    def test_python_minimum_is_accepted(self):
        self.response["python_version"] = [3, 10, 0]
        self.assertTrue(self.validate()["remote_python_supported"])

    def test_missing_and_nonboolean_remote_checks_are_rejected(self):
        for value in (1, "true", None):
            with self.subTest(value=value):
                self.response["checks"]["sudo_denied"] = value
                self.assertFalse(any(self.validate().values()))
        del self.response["checks"]["sudo_denied"]
        self.assertFalse(any(self.validate().values()))

    def test_each_remote_check_is_required(self):
        for name in probe.REMOTE_CHECK_NAMES:
            with self.subTest(check=name):
                response = copy.deepcopy(self.response)
                response["checks"][name] = False
                result = self.validate(response)
                self.assertFalse(result[name])
                self.assertFalse(all(result.values()))

    def test_wrong_echo_cannot_borrow_correct_digest(self):
        self.response["echo_b64"] = base64.b64encode(b"different bytes").decode()
        result = self.validate()
        self.assertFalse(result["fixture_echo_matches"])
        self.assertFalse(result["fixture_digest_matches"])

    def test_correct_echo_with_wrong_digest_is_rejected(self):
        self.response["echo_sha256"] = "0" * 64
        result = self.validate()
        self.assertTrue(result["fixture_echo_matches"])
        self.assertFalse(result["fixture_digest_matches"])

    def test_malformed_echo_is_rejected(self):
        for value in ("not valid base64!", "\u00ff", 123, None, "A" * 2049):
            with self.subTest(value_type=type(value).__name__):
                self.response["echo_b64"] = value
                result = self.validate()
                self.assertFalse(result["fixture_echo_matches"])
                self.assertFalse(result["fixture_digest_matches"])

    def test_invalid_expected_fixture_fails_closed(self):
        raw = json.dumps(self.response).encode()
        for value in (b"", b"A" * 1025, "wrong type"):
            with self.subTest(value_type=type(value).__name__):
                self.assertFalse(any(probe.validate_remote_response(raw, value).values()))

    def test_validation_returns_only_fixed_boolean_fields(self):
        result = self.validate()
        self.assertEqual(set(result), set(probe.RESPONSE_CHECK_NAMES))
        self.assertTrue(all(type(value) is bool for value in result.values()))


class ClientIsolationTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "AWS_SUPPORT_HOST": "support.example.invalid",
            "AWS_SUPPORT_USER": "poly-support",
            "AWS_SUPPORT_SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nNOT-A-REAL-KEY\n-----END OPENSSH PRIVATE KEY-----",
            "AWS_SUPPORT_KNOWN_HOSTS": "support.example.invalid ssh-ed25519 ZmFrZS1waW4=",
            "PATH": "/usr/bin:/bin",
        }

    def test_embedded_remote_program_compiles_without_execution(self):
        compile("expected = b'offline fixture'\n" + probe.REMOTE_SOURCE, "<remote-probe>", "exec")

    def test_missing_inputs_never_start_ssh(self):
        with mock.patch.object(probe.subprocess, "run") as ssh:
            result = probe.run_preflight({})
        ssh.assert_not_called()
        self.assertFalse(any(result.values()))

    def test_wrong_user_host_or_pin_never_starts_ssh(self):
        for name, value in (
            ("AWS_SUPPORT_USER", "root"),
            ("AWS_SUPPORT_HOST", "-oProxyCommand=anything"),
            ("AWS_SUPPORT_HOST", "example.invalid;whoami"),
            ("AWS_SUPPORT_KNOWN_HOSTS", "* ssh-ed25519 ZmFrZS1waW4="),
            ("AWS_SUPPORT_KNOWN_HOSTS", "other.example.invalid ssh-ed25519 ZmFrZS1waW4="),
            ("AWS_SUPPORT_SSH_KEY", "not a private key"),
        ):
            with self.subTest(name=name):
                environment = dict(self.environment)
                environment[name] = value
                with mock.patch.object(probe.subprocess, "run") as ssh:
                    result = probe.run_preflight(environment)
                ssh.assert_not_called()
                self.assertFalse(result["input_format_valid"])

    def test_ssh_uses_private_files_pinned_host_and_no_secret_environment(self):
        paths = []

        def inspect_call(command, **kwargs):
            identity = Path(command[command.index("-i") + 1])
            pin_option = next(value for value in command if value.startswith("UserKnownHostsFile="))
            pin = Path(pin_option.split("=", 1)[1])
            paths.extend((identity, pin, identity.parent))
            self.assertEqual(stat.S_IMODE(identity.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(pin.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(identity.parent.stat().st_mode), 0o700)
            for option in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=15", "ConnectionAttempts=1"):
                self.assertIn(option, command)
            self.assertEqual(command[-5:], ["poly-support@support.example.invalid", "python3", "-I", "-B", "-"])
            self.assertEqual(kwargs["timeout"], 45)
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin"})
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)
            self.assertNotIn("ProxyCommand", " ".join(command))
            self.assertNotIn("-F", command)
            return subprocess.CompletedProcess(command, 255, b"PRIVATE STDOUT", b"PRIVATE STDERR")

        output = io.StringIO()
        with mock.patch.object(probe.subprocess, "run", side_effect=inspect_call) as ssh:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = probe.run_preflight(self.environment)
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(result["private_temporary_files"])
        self.assertFalse(result["ssh_exit_zero"])
        self.assertTrue(all(not path.exists() for path in paths))

    def test_timeout_does_not_retry_or_expose_output(self):
        timeout = subprocess.TimeoutExpired(["ssh"], 45, output=b"PRIVATE HOST", stderr=b"PRIVATE ERROR")
        output = io.StringIO()
        with mock.patch.object(probe.subprocess, "run", side_effect=timeout) as ssh:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = probe.run_preflight(self.environment)
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(result["ssh_completed_within_timeout"])

    def test_local_receipt_is_private_and_contains_only_safe_data(self):
        checks = {name: True for name in probe.CLIENT_CHECK_NAMES}
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            output = io.StringIO()
            with mock.patch.object(probe, "run_preflight", return_value=checks):
                with mock.patch("sys.argv", ["aws_support_preflight.py", "--receipt", str(receipt)]):
                    with contextlib.redirect_stdout(output):
                        code = probe.main()
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            self.assertEqual(json.loads(receipt.read_text()), {"schema": probe.SCHEMA, "passed": True, "checks": checks})
            self.assertNotIn(str(receipt), output.getvalue())
            for value in self.environment.values():
                self.assertNotIn(value, output.getvalue())


if __name__ == "__main__":
    unittest.main()

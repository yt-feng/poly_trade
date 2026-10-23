"""Synthetic contracts for saved-input transport and table identity verification."""
from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import research_vault as vault
import scoped_research_dataset as dataset
import verify_saved_research_input as verifier

REPOSITORY = "yt-feng/poly_trade"
CODE = "a" * 40
SOURCE = {"repository": REPOSITORY, "commit": "b" * 40,
          "path": "research_vault/episodes/" + "c" * 64 + "/part-0000.vault"}
JOIN_KEYS = ["market_id", "run_name", "side", "decision_ts", "market_close_ts"]
ROWS = [
    ["synthetic-1", "run-1", "Up", "2020-01-01T00:01:00+00:00", "2020-01-01T00:05:00+00:00"],
    ["synthetic-2", "run-2", "Down", "2020-01-01T00:06:00+00:00", "2020-01-01T00:10:00+00:00"],
]


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def csv_bytes(columns, rows) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def table_contract(path, columns, rows=ROWS):
    return {"path": path, "rows": len(rows), "columns": columns,
            "ids_sha256": digest(vault.canonical(sorted(row[0] for row in rows)))}


def configuration(descriptor=None):
    return {"version": 1, "format": verifier.FORMAT, "expected_code_commit": CODE,
            "descriptor_sha256": digest(vault.canonical(descriptor or {})),
            "source": copy.deepcopy(SOURCE), "join_keys": JOIN_KEYS[:],
            "tables": [table_contract("features.csv", JOIN_KEYS + ["observed_feature"]),
                       table_contract("domains.csv", JOIN_KEYS + ["future_label"])]}


class ConfigContracts(unittest.TestCase):
    def test_valid(self):
        verifier.validate_config(configuration(), REPOSITORY, CODE)

    def test_invalid_config_and_identity(self):
        mutations = [
            ("version", True), ("format", "other"), ("expected_code_commit", "d" * 40),
            ("descriptor_sha256", "no"), ("tables", []), ("join_keys", []),
            ("join_keys", ["market_id", "market_id"]),
            ("join_keys", ["market_id", "missing"]),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                config = configuration(); config[key] = value
                with self.assertRaises((ValueError, TypeError, KeyError)):
                    verifier.validate_config(config, REPOSITORY, CODE)
        for repository, code in [("outside/other", CODE), (REPOSITORY, "main"), (REPOSITORY, "A" * 40)]:
            with self.subTest(repository=repository, code=code):
                with self.assertRaises((ValueError, TypeError)):
                    verifier.validate_config(configuration(), repository, code)
        config = configuration(); config["unexpected"] = "value"
        with self.assertRaises(ValueError): verifier.validate_config(config, REPOSITORY, CODE)

    def test_bad_immutable_source(self):
        mutations = [("commit", "main"), ("commit", "b" * 39),
                     ("repository", "outside/other"),
                     ("path", "research_vault/episodes/" + "c" * 64 + "/../part-0000.vault"),
                     ("path", "https://example.invalid/key"),
                     ("path", "research_vault/episodes/" + "c" * 64 + "/part-0001.vault")]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                config = configuration(); config["source"][key] = value
                with self.assertRaises(ValueError): verifier.validate_config(config, REPOSITORY, CODE)

    def test_bad_table_schema(self):
        mutations = [("rows", True), ("rows", 0), ("rows", 1000001),
                     ("path", "../features.csv"), ("ids_sha256", "x" * 64),
                     ("columns", ["market_id", "market_id"]), ("columns", ["other"])]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                config = configuration(); config["tables"][0][key] = value
                with self.assertRaises(ValueError): verifier.validate_config(config, REPOSITORY, CODE)
        config = configuration(); config["tables"][1]["path"] = "features.csv"
        with self.assertRaises(ValueError): verifier.validate_config(config, REPOSITORY, CODE)


class TableContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = configuration()
        self.write_rows()

    def write_rows(self, feature_rows=None, domain_rows=None, feature_columns=None, domain_columns=None):
        features = [row + ["0.25"] for row in ROWS] if feature_rows is None else feature_rows
        domains = [row + [""] for row in ROWS] if domain_rows is None else domain_rows
        (self.directory / "features.csv").write_bytes(csv_bytes(feature_columns or self.config["tables"][0]["columns"], features))
        (self.directory / "domains.csv").write_bytes(csv_bytes(domain_columns or self.config["tables"][1]["columns"], domains))

    def test_ordered_join_and_unobserved_label_retained(self):
        result = verifier.verify_tables(self.directory, self.config)
        self.assertEqual([item["rows"] for item in result], [2, 2])
        self.assertEqual(result[0]["ordered_join_sha256"], result[1]["ordered_join_sha256"])

    def test_same_instant_timezone_normalizes(self):
        rows = [row + [""] for row in ROWS]
        rows[0][3] = "2020-01-01T08:01:00+08:00"
        rows[0][4] = "2020-01-01T08:05:00+08:00"
        self.write_rows(domain_rows=rows)
        verifier.verify_tables(self.directory, self.config)

    def test_missing_duplicate_empty_and_extra_ids(self):
        variants = [[ROWS[0] + [""]], [ROWS[0] + [""], ROWS[0] + [""]],
                    [[""] + ROWS[0][1:] + [""], ROWS[1] + [""]],
                    [row + [""] for row in ROWS] + [["synthetic-3"] + ROWS[1][1:] + [""]]]
        for rows in variants:
            with self.subTest(rows=rows):
                self.write_rows(domain_rows=rows)
                with self.assertRaises(ValueError): verifier.verify_tables(self.directory, self.config)

    def test_same_ids_different_order_or_join_values_rejected(self):
        variants = [[row + [""] for row in reversed(ROWS)]]
        for index, value in [(1, "other-run"), (2, "Down"), (3, "2020-01-01T00:01:01+00:00"),
                             (4, "2020-01-01T00:05:01+00:00")]:
            rows = [row + [""] for row in ROWS]; rows[0][index] = value; variants.append(rows)
        for rows in variants:
            with self.subTest(rows=rows):
                self.write_rows(domain_rows=rows)
                with self.assertRaises(ValueError): verifier.verify_tables(self.directory, self.config)

    def test_columns_and_row_shapes(self):
        variants = [[row + ["", "extra"] for row in ROWS], [row[:-1] for row in ROWS]]
        for rows in variants:
            with self.subTest(rows=rows):
                self.write_rows(domain_rows=rows)
                with self.assertRaises(ValueError): verifier.verify_tables(self.directory, self.config)
        self.write_rows(domain_columns=list(reversed(self.config["tables"][1]["columns"])))
        with self.assertRaises(ValueError): verifier.verify_tables(self.directory, self.config)

    def test_timestamp_requires_timezone(self):
        self.assertRaises(ValueError, verifier.join_value, "decision_ts", "2020-01-01T00:01:00")
        self.assertRaises(ValueError, verifier.join_value, "decision_ts", "invalid")

    def test_nonzero_submicrosecond_precision_never_silently_collapses(self):
        for separator in (".", ","):
            with self.subTest(separator=separator):
                left = "2020-01-01T00:01:00" + separator + "0000001Z"
                right = "2020-01-01T00:01:00" + separator + "0000002Z"
                try:
                    normalized_left = verifier.join_value("decision_ts", left)
                    normalized_right = verifier.join_value("decision_ts", right)
                except ValueError:
                    continue
                self.assertNotEqual(normalized_left, normalized_right)


class FakeResponse:
    def __init__(self, data, status=200): self.data, self.status, self.read_sizes = data, status, []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size): self.read_sizes.append(size); return self.data[:size]


class FetchContracts(unittest.TestCase):
    def test_get_only_fixed_immutable_target_bounded_and_token(self):
        response = FakeResponse(b"abc")
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return response
        opener = Opener()
        with patch.object(verifier.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(verifier.fetch_ciphertext(SOURCE, 3, token="synthetic-token"), b"abc")
        self.assertIsInstance(build.call_args.args[0], verifier.NoRedirect)
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertEqual(opener.request.full_url,
            "https://api.github.com/repos/" + REPOSITORY + "/contents/" + SOURCE["path"] + "?ref=" + SOURCE["commit"])
        self.assertEqual(opener.request.headers["Authorization"], "Bearer synthetic-token")
        self.assertEqual(response.read_sizes, [4]); self.assertEqual(opener.timeout, 30)

    def test_no_redirect(self):
        self.assertIsNone(verifier.NoRedirect().redirect_request(None, None, 302, "moved", {}, "https://example.invalid"))

    def test_bad_path_ref_and_size_never_open(self):
        variants = [dict(SOURCE, commit="latest"), dict(SOURCE, path="../part-0000.vault"),
                    dict(SOURCE, repository="https://example.invalid"), dict(SOURCE, url="https://example.invalid")]
        with patch.object(verifier.urllib.request, "build_opener", side_effect=AssertionError("network forbidden")):
            for source in variants:
                with self.subTest(source=source):
                    with self.assertRaises((ValueError, TypeError)): verifier.fetch_ciphertext(source, 3)
            for maximum in [0, -1, True, vault.MAX_ENVELOPE + 1]:
                with self.subTest(maximum=maximum):
                    with self.assertRaises(ValueError): verifier.fetch_ciphertext(SOURCE, maximum)

    def test_short_excess_or_wrong_status_rejected(self):
        for response in [FakeResponse(b"ab"), FakeResponse(b"abcd"), FakeResponse(b"abc", 302)]:
            with self.subTest(size=len(response.data), status=response.status):
                opener = unittest.mock.Mock(); opener.open.return_value = response
                with patch.object(verifier.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(ValueError): verifier.fetch_ciphertext(SOURCE, 3)

    def test_bounded_same_target_retry_and_no_auth_error_retry(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = [urllib.error.URLError("synthetic timeout"), FakeResponse(b"abc")]
        with patch.object(verifier.urllib.request, "build_opener", return_value=opener), patch.object(verifier.time, "sleep") as sleep:
            self.assertEqual(verifier.fetch_ciphertext(SOURCE, 3), b"abc")
        self.assertEqual(opener.open.call_count, 2); sleep.assert_called_once_with(1)
        self.assertEqual(opener.open.call_args_list[0].args[0].full_url, opener.open.call_args_list[1].args[0].full_url)
        opener.open.reset_mock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.github.com", 401, "unauthorized", {}, None)
        with patch.object(verifier.urllib.request, "build_opener", return_value=opener), patch.object(verifier.time, "sleep") as sleep:
            with self.assertRaises(ValueError): verifier.fetch_ciphertext(SOURCE, 3)
        self.assertEqual(opener.open.call_count, 1); sleep.assert_not_called()


class IntegrationContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        inputs = self.root / "inputs"; inputs.mkdir()
        columns = {"features.csv": JOIN_KEYS + ["observed_feature"], "domains.csv": JOIN_KEYS + ["future_label"]}
        for name in columns:
            (inputs / name).write_bytes(csv_bytes(columns[name], [row + [""] for row in ROWS]))
        (inputs / "provenance.json").write_bytes(vault.canonical({"format": "synthetic-provenance-v1"}))
        private = X25519PrivateKey.generate(); recipient = vault.recipient_for(private)
        self.envelope, self.capability, self.descriptor = dataset.pack(
            {name: inputs / name for name in ["features.csv", "domains.csv", "provenance.json"]},
            recipient, recipient["recipient_id"])
        self.config = configuration(self.descriptor)

    def test_tiny_roundtrip_no_identity_unlock_no_strategy_and_no_key_in_report(self):
        calls = []
        def fetch(source, maximum, **kwargs):
            calls.append((source, maximum)); return self.envelope
        with patch.object(vault, "unlock_identity", side_effect=AssertionError("identity forbidden")):
            report = verifier.evaluate(self.config, self.capability, self.root / "restored",
                                       repository=REPOSITORY, code_commit=CODE, fetch=fetch)
        self.assertEqual(calls, [(SOURCE, len(self.envelope))]); self.assertTrue(report["input_verified"])
        self.assertFalse(report["strategy_evaluated"]); self.assertFalse(report["model_refit"])
        for key in ["actual_model_fit_calls", "actual_model_predict_calls", "replayed_cells", "actual_fills", "verified_canary_fills"]:
            self.assertEqual(report[key], 0)
        serialized = vault.canonical(report)
        self.assertNotIn(self.capability["content_key_b64"].encode(), serialized)
        self.assertNotIn(b"content_key_b64", serialized)
        self.assertNotIn(b"identity.keybundle", serialized)
        self.assertEqual(len(report["tables"]), 2)

    def test_descriptor_mismatch_prevents_fetch(self):
        config = copy.deepcopy(self.config); config["descriptor_sha256"] = "f" * 64
        fetch = unittest.mock.Mock(side_effect=AssertionError("network forbidden"))
        with self.assertRaises(ValueError):
            verifier.evaluate(config, self.capability, self.root / "restored", repository=REPOSITORY, code_commit=CODE, fetch=fetch)
        fetch.assert_not_called()

    def test_malformed_capability_prevents_fetch(self):
        variants = []
        for value in ["", base64.b64encode(bytes(31)).decode(), base64.b64encode(bytes(33)).decode(), True, {}]:
            capability = copy.deepcopy(self.capability); capability["content_key_b64"] = value; variants.append(capability)
        capability = copy.deepcopy(self.capability); del capability["content_key_b64"]; variants.append(capability)
        capability = copy.deepcopy(self.capability); capability["unexpected"] = "synthetic"; variants.append(capability)
        for capability in variants:
            with self.subTest(keys=sorted(capability), value_type=type(capability.get("content_key_b64")).__name__):
                config = copy.deepcopy(self.config)
                config["descriptor_sha256"] = digest(vault.canonical({k: v for k, v in capability.items() if k != "content_key_b64"}))
                fetch = unittest.mock.Mock(side_effect=AssertionError("network forbidden"))
                with self.assertRaises((ValueError, TypeError)):
                    verifier.evaluate(config, capability, self.root / "restored", repository=REPOSITORY, code_commit=CODE, fetch=fetch)
                fetch.assert_not_called()

    def test_cipher_mutation_rejected_and_no_success(self):
        changed = bytearray(self.envelope); changed[-2] ^= 1
        with self.assertRaises(ValueError):
            verifier.evaluate(self.config, self.capability, self.root / "restored", repository=REPOSITORY,
                              code_commit=CODE, fetch=lambda *args, **kwargs: bytes(changed))


if __name__ == "__main__":
    unittest.main()

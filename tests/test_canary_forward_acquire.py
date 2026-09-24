from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "analysis" / "forward_acquire.py"
spec = importlib.util.spec_from_file_location("forward_acquire_under_test", MODULE_PATH)
forward_acquire = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(forward_acquire)


def asset(name: str, body: bytes) -> tuple[dict, dict, dict[str, bytes]]:
    url = f"https://example.test/{name}"
    sha_url = url + ".sha256"
    digest = hashlib.sha256(body).hexdigest().encode("utf-8") + b"  " + name.encode("utf-8") + b"\n"
    data = {
        "name": name,
        "size": len(body),
        "browser_download_url": url,
        "created_at": "2026-09-24T00:00:00Z",
    }
    side = {
        "name": name + ".sha256",
        "size": len(digest),
        "browser_download_url": sha_url,
        "created_at": "2026-09-24T00:00:00Z",
    }
    return data, side, {url: body, sha_url: digest}


class ForwardAcquireBudgetTests(unittest.TestCase):
    def setUp(self):
        self.old_releases = forward_acquire.list_capture_releases
        self.old_assets = forward_acquire.list_release_assets
        self.old_request = forward_acquire.request
        forward_acquire.list_capture_releases = lambda n: [{
            "id": 1,
            "tag_name": "capture-v2-1-1",
            "published_at": "2026-09-24T00:00:00Z",
        }]

    def tearDown(self):
        forward_acquire.list_capture_releases = self.old_releases
        forward_acquire.list_release_assets = self.old_assets
        forward_acquire.request = self.old_request

    def test_byte_budget_is_explicit_skip_not_integrity_error(self):
        first, first_sha, first_map = asset("snapshots-000001.jsonl.gz", b"abcd")
        second, second_sha, second_map = asset("snapshots-000002.jsonl.gz", b"efgh")
        payloads = {**first_map, **second_map}
        forward_acquire.list_release_assets = lambda rid: [first, first_sha, second, second_sha]
        forward_acquire.request = lambda url: payloads[url]

        with tempfile.TemporaryDirectory() as td:
            result = forward_acquire.acquire(Path(td), max_releases=1, max_bytes=4)

        self.assertEqual(result["errors"], [])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["downloaded_bytes"], 4)
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "download byte budget")
        self.assertEqual(result["eligible_archives_total"], 2)
        self.assertEqual(result["releases"][0]["downloaded_snapshot_label_archives"], 1)
        self.assertEqual(result["releases"][0]["budget_skipped_snapshot_label_archives"], 1)

    def test_missing_sidecar_remains_integrity_error(self):
        good, good_sha, good_map = asset("snapshots-000001.jsonl.gz", b"abcd")
        bad = {
            "name": "snapshots-000002.jsonl.gz",
            "size": 3,
            "browser_download_url": "https://example.test/missing",
            "created_at": "2026-09-24T00:00:00Z",
        }
        forward_acquire.list_release_assets = lambda rid: [good, good_sha, bad]
        forward_acquire.request = lambda url: good_map[url]

        with tempfile.TemporaryDirectory() as td:
            result = forward_acquire.acquire(Path(td), max_releases=1, max_bytes=100)

        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["error"], "missing checksum sidecar")

    def test_invalid_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                forward_acquire.acquire(Path(td), max_releases=0, max_bytes=1)
            with self.assertRaises(ValueError):
                forward_acquire.acquire(Path(td), max_releases=1, max_bytes=0)


if __name__ == "__main__":
    unittest.main()

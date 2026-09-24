import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import canary_acquire as ca


def asset(name: str, body: bytes):
    url = "https://example.test/" + name
    digest = hashlib.sha256(body).hexdigest().encode() + b"  " + name.encode() + b"\n"
    item = {"name": name, "size": len(body), "browser_download_url": url}
    side = {
        "name": name + ".sha256",
        "size": len(digest),
        "browser_download_url": url + ".sha256",
    }
    return item, side, {url: body, url + ".sha256": digest}


class CanaryAcquireTests(unittest.TestCase):
    def setUp(self):
        self.old_releases = ca.list_capture_releases
        self.old_assets = ca.list_release_assets
        self.old_request = ca.request

    def tearDown(self):
        ca.list_capture_releases = self.old_releases
        ca.list_release_assets = self.old_assets
        ca.request = self.old_request

    def fixture(self, *, bad_snapshot_checksum=False):
        release = {
            "id": 1,
            "tag_name": "capture-v2-1-1",
            "published_at": "2026-09-24T00:00:00Z",
        }
        label, label_sha, lm = asset("labels-000001.jsonl.gz", b"ll")
        snap, snap_sha, sm = asset("snapshots-000001.jsonl.gz", b"ssss")
        raw1, raw1_sha, r1m = asset("raw-000001.jsonl.gz", b"rrrrr")
        raw2, raw2_sha, r2m = asset("raw-000002.jsonl.gz", b"ttttt")
        assets = [label, label_sha, snap, snap_sha, raw1, raw1_sha, raw2, raw2_sha]
        payloads = {**lm, **sm, **r1m, **r2m}
        if bad_snapshot_checksum:
            payloads[snap_sha["browser_download_url"]] = b"0" * 64 + b"  snapshots-000001.jsonl.gz\n"
        ca.list_capture_releases = lambda n=4: [release]
        ca.list_release_assets = lambda rid: assets

        def fake_request(url, limit=100 * 1024 * 1024):
            if url.startswith("https://raw.githubusercontent.com/"):
                return b"# collector source\n"
            return payloads[url]

        ca.request = fake_request

    def test_raw_budget_cannot_starve_snapshot_and_label(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as td:
            result = ca.acquire(Path(td), max_bytes=6, max_raw_bytes=5, max_releases=1, raw_files=2)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["research_downloaded_bytes"], 6)
        self.assertEqual(result["raw_downloaded_bytes"], 5)
        research = [x for x in result["files"] if x["kind"] == "research"]
        raw = [x for x in result["files"] if x["kind"] == "raw_event_evidence"]
        self.assertEqual({Path(x["path"]).name for x in research}, {
            "labels-000001.jsonl.gz", "snapshots-000001.jsonl.gz"
        })
        self.assertEqual(len(raw), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "raw byte budget")

    def test_checksum_failure_is_not_a_budget_skip(self):
        self.fixture(bad_snapshot_checksum=True)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(RuntimeError):
                ca.acquire(root, max_bytes=100, max_raw_bytes=20, max_releases=1, raw_files=0)
            manifest = json.loads((root / "manifest.json").read_text())
        self.assertTrue(any(x["name"] == "snapshots-000001.jsonl.gz" for x in manifest["errors"]))
        self.assertFalse(any(x["name"] == "snapshots-000001.jsonl.gz" for x in manifest["skipped"]))

    def test_asset_listing_is_explicitly_paginated(self):
        page1 = [{"name": f"a{i}"} for i in range(100)]
        page2 = [{"name": "tail"}]

        def fake_request(url, limit=100 * 1024 * 1024):
            if url.endswith("&page=1"):
                return json.dumps(page1).encode()
            if url.endswith("&page=2"):
                return json.dumps(page2).encode()
            raise AssertionError(url)

        ca.request = fake_request
        assets = ca.list_release_assets(99)
        self.assertEqual(len(assets), 101)
        self.assertEqual(assets[-1]["name"], "tail")

    def test_invalid_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                ca.acquire(Path(td), max_bytes=0)
            with self.assertRaises(ValueError):
                ca.acquire(Path(td), max_raw_bytes=-1)


if __name__ == "__main__":
    unittest.main()

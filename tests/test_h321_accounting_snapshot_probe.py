import io
import json
import zipfile
import unittest
from urllib.error import HTTPError

from analysis.h321_accounting_snapshot_probe import probe_accounts, summarize_response


class H321AccountingSnapshotProbeTests(unittest.TestCase):
    def test_summarizes_zip_tables_without_row_values(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("trades.csv", "slug,transactionHash,amount\nbtc-updown-5m-1,0xabc,5\nother,0xdef,2\n")
            archive.writestr("activity.json", json.dumps([{"type": "REDEEM", "cash": 1.2}]))
        result = summarize_response(buffer.getvalue(), "application/zip")
        self.assertEqual(result["byte_length"], len(buffer.getvalue()))
        self.assertEqual(len(result["archive_entries"]), 2)
        self.assertEqual(len(result["tables"]), 2)
        self.assertEqual(sum(t.get("btc_five_minute_row_count", 0) for t in result["tables"]), 1)
        self.assertNotIn("0xabc", json.dumps(result))

    def test_probe_uses_user_query_and_keeps_bundle_closed(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return 200, {"Content-Type": "application/json"}, b'[{"slug":"btc-updown-5m-1","transactionHash":"0xabc"}]'

        result = probe_accounts([{"label": "a", "owner": "0x" + "1" * 40}], fetcher=fetch)
        self.assertEqual(result["successful_accounts"], 1)
        self.assertEqual(result["btc_five_minute_rows"], 1)
        self.assertFalse(result["owner_scoped_bundle_available"])
        self.assertIn("/v1/accounting/snapshot?user=", seen[0])
        self.assertNotIn("0x1111", json.dumps(result))

    def test_probe_reports_fetch_failure_without_leaking_owner(self):
        def fetch(_url):
            raise TimeoutError("provider timeout")

        result = probe_accounts([{"label": "a", "owner": "0x" + "2" * 40}], fetcher=fetch)
        self.assertEqual(result["successful_accounts"], 0)
        self.assertEqual(result["accounts"][0]["failure_code"], "TimeoutError")
        self.assertNotIn("0x2222", json.dumps(result))

    def test_probe_preserves_only_bounded_http_failure_category(self):
        def fetch(_url):
            raise HTTPError(_url, 403, "private response body must not be retained", {}, io.BytesIO(b"secret"))

        result = probe_accounts([{"label": "a", "owner": "0x" + "3" * 40}], fetcher=fetch)
        self.assertEqual(result["accounts"][0]["failure_code"], "http_403")
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

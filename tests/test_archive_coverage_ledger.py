import csv
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from analysis import archive_coverage_ledger as ledger
from analysis.archive_baseline_audit import PRICE_FIELDS, SIZE_FIELDS


DATE = "2026-09-26"
START = int(datetime(2026, 9, 26, tzinfo=timezone.utc).timestamp())


def observation(start=START, offset=119, **changes):
    row = dict(zip(PRICE_FIELDS + SIZE_FIELDS, (51, 50, 50, 49, 10, 11, 12, 13)))
    row.update(slug=f"btc-updown-5m-{start}",
               ts_iso=datetime.fromtimestamp(start + offset, timezone.utc).isoformat())
    row.update(changes)
    return row


def write_csv(root, name, rows):
    path = root / "data" / "monthly_runs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["ts_iso", "slug"] + list(PRICE_FIELDS + SIZE_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path


class ArchiveCoverageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "source"
        self.database = self.base / "private.sqlite"

    def build(self):
        ledger.build(self.root, self.database)
        db = ledger.connect_readonly(self.database)
        self.addCleanup(db.close)
        return db, ledger.coverage_report(db)

    def test_utc_midnight_selection_uses_market_and_parsed_sample_dates(self):
        start = START + 16 * 3600
        path = write_csv(self.root, "next-local-day.csv", [
            observation(start, ts_iso="2026-09-27T00:01:59+08:00")])
        _, report = self.build()
        info = report["files"][0]
        self.assertEqual(info["sample_dates"], {DATE: 1})
        self.assertEqual(info["market_dates"], {DATE: 1})
        relative = str(path.relative_to(self.root))
        self.assertEqual(ledger.required_files(report["files"], [DATE]), {relative})
        self.assertEqual(ledger.required_files(report["files"], ["2026-09-27"]), set())
        self.assertEqual(ledger.utc_us("2026-09-27T00:01:59+08:00"),
                         ledger.utc_us("2026-09-26T16:01:59Z"))
        self.assertIsNone(ledger.utc_us("2026-09-26T16:01:59"))

    def test_invalid_sample_time_preserves_present_window_and_date_inventory(self):
        path = write_csv(self.root, "invalid-times.csv", [
            observation(ts_iso="not-a-time"),
            observation(offset=301),
            observation(ts_iso="2026-09-27T00:00:00Z")])
        db, report = self.build()
        window = report["window_ledger"][0]
        self.assertEqual(window["status"], "present_invalid_time_only")
        self.assertEqual((window["raw_rows"], window["unique_timestamps"], window["invalid_time_rows"]), (3, 0, 3))
        day = report["by_date"][DATE]
        self.assertEqual((day["scheduled_windows"], day["present_windows"], day["absent_windows"]), (288, 1, 287))
        grouped, prices, depths, present = ledger.load_dates(db, [DATE])
        self.assertEqual(dict(grouped), {})
        self.assertEqual((prices, depths), (set(), set()))
        self.assertEqual(present, {f"btc-updown-5m-{START}"})
        relative = str(path.relative_to(self.root))
        self.assertEqual(ledger.required_files(report["files"], [DATE]), {relative})
        self.assertEqual(ledger.required_files(report["files"], ["2026-09-27"]), {relative})

    def test_cross_file_timezone_duplicates_are_one_observation(self):
        row = observation()
        write_csv(self.root, "first.csv", [row])
        write_csv(self.root, "second.csv", [dict(row, ts_iso="2026-09-26T08:01:59+08:00")])
        db, report = self.build()
        window = report["window_ledger"][0]
        self.assertEqual((window["raw_rows"], window["unique_timestamps"], window["duplicate_rows"]), (2, 1, 1))
        self.assertEqual(window["source_files"], 2)
        self.assertFalse(window["price_conflict"])
        self.assertFalse(window["depth_conflict"])
        grouped, _, _, _ = ledger.load_dates(db, [DATE, DATE])
        self.assertEqual(len(grouped[row["slug"]]), 1)

    def test_price_and_depth_conflicts_remain_separate(self):
        price_row = observation()
        depth_row = observation(START + 300)
        write_csv(self.root, "first.csv", [price_row, depth_row])
        write_csv(self.root, "second.csv", [dict(price_row, buy_up_cents=52),
                                              dict(depth_row, buy_up_size=20)])
        db, report = self.build()
        grouped, price_conflicts, depth_conflicts, present = ledger.load_dates(db, [DATE])
        self.assertEqual(price_conflicts, {price_row["slug"]})
        self.assertEqual(depth_conflicts, {depth_row["slug"]})
        self.assertEqual(present, {price_row["slug"], depth_row["slug"]})
        self.assertEqual(sum(map(len, grouped.values())), 2)
        day = report["by_date"][DATE]
        self.assertEqual((day["price_conflict_windows"], day["depth_conflict_windows"]), (1, 1))
        self.assertEqual((day["any_valid_price_windows"], day["any_valid_depth_windows"]), (1, 0))

    def test_missing_depth_is_not_zero_or_missing_price(self):
        write_csv(self.root, "no-depth.csv", [observation(**{field: "" for field in SIZE_FIELDS})])
        db, report = self.build()
        grouped, _, _, _ = ledger.load_dates(db, [DATE])
        row = grouped[f"btc-updown-5m-{START}"][0]
        self.assertEqual(row["values"][4:], (None,) * 4)
        self.assertIsNone(row["price_reason"])
        self.assertEqual(row["depth_reason"], "missing_or_nonfinite_depth")
        day = report["by_date"][DATE]
        self.assertEqual((day["any_valid_price_windows"], day["any_valid_depth_windows"]), (1, 0))
        self.assertEqual(report["window_ledger"][0]["invalid_depth_rows"], 1)

    def test_unique_observations_sorted_and_invalid_first_future_retained(self):
        write_csv(self.root, "a.csv", [observation(offset=151), observation(offset=119)])
        write_csv(self.root, "b.csv", [observation(offset=150, buy_up_cents=""), observation(offset=104)])
        db, _ = self.build()
        grouped, _, _, _ = ledger.load_dates(db, [DATE])
        rows = grouped[f"btc-updown-5m-{START}"]
        self.assertEqual([row["ts"] for row in rows], [START + n for n in (104, 119, 150, 151)])
        self.assertEqual(rows[2]["price_reason"], "missing_or_nonfinite_price")
        self.assertIsNone(rows[3]["price_reason"])

    def test_unique_validity_counts_do_not_depend_on_conflicting_file_order(self):
        reports = []
        for i, rows in enumerate(((observation(), observation(buy_up_cents="")),
                                   (observation(buy_up_cents=""), observation()))):
            root = self.base / f"order-{i}"
            for name, row in zip(("a.csv", "b.csv"), rows):
                write_csv(root, name, [row])
            database = self.base / f"order-{i}.sqlite"
            ledger.build(root, database)
            with ledger.connect_readonly(database) as db:
                reports.append(ledger.coverage_report(db))
        self.assertEqual(reports[0]["window_ledger"], reports[1]["window_ledger"])
        self.assertEqual(reports[0]["by_date"], reports[1]["by_date"])

    def test_missing_file_detected_even_when_selected_counts_match(self):
        needed = write_csv(self.root, "needed.csv", [observation()])
        extra = write_csv(self.root, "wrong-day.csv", [observation(START - 86400)])
        _, report = self.build()
        previous = {"train_dates": [DATE], "validation_dates": [],
                    "files": [{"path": str(extra.relative_to(self.root))}]}
        audit = ledger.omission_audit(report["files"], previous)
        self.assertEqual((audit["expected_files"], audit["previous_files"]), (1, 1))
        self.assertEqual(audit["existing_but_omitted_files"], [str(needed.relative_to(self.root))])
        self.assertEqual(audit["unneeded_selected_files"], [str(extra.relative_to(self.root))])

    def test_same_length_bytes_mutation_and_inventory_mutation_are_rejected(self):
        path = write_csv(self.root, "source.csv", [observation()])
        _, report = self.build()
        files = report["files"]
        ledger.verify_sources(self.root, files)
        original = path.read_bytes()
        changed = original.replace(b"51", b"52")
        self.assertNotEqual(original, changed)
        self.assertEqual(len(original), len(changed))
        path.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "source_bytes_changed"):
            ledger.verify_sources(self.root, files)
        path.write_bytes(original)
        added = write_csv(self.root, "added.csv", [observation()])
        with self.assertRaisesRegex(ValueError, "inventory_paths_changed"):
            ledger.verify_sources(self.root, files)
        added.unlink()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "inventory_paths_changed"):
            ledger.verify_sources(self.root, files)

    def test_database_is_private_readonly_and_requires_completed_build(self):
        write_csv(self.root, "source.csv", [observation()])
        db, _ = self.build()
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM observations")
        incomplete = self.base / "incomplete.sqlite"
        with sqlite3.connect(incomplete) as raw:
            raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        with self.assertRaisesRegex(ValueError, "incomplete_coverage_ledger"):
            ledger.connect_readonly(incomplete)


if __name__ == "__main__":
    unittest.main()

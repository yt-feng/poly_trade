"""Offline UTC coverage ledger for existing CSV bytes; no model or network.

The complete discovered inventory is the input. Date prefixes in raw timestamp
strings and filenames are never selectors. Private SQLite keeps one observation
per market and UTC microsecond; repeated captures cannot increase sample size.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3

try:
    from archive_baseline_audit import PRICE_FIELDS, SIZE_FIELDS, market_start, number, write_private
except ImportError:
    from analysis.archive_baseline_audit import PRICE_FIELDS, SIZE_FIELDS, market_start, number, write_private


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def utc_us(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    except (ValueError, TypeError, OverflowError):
        return None


def utc_day(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()


def price_reason(prices):
    if any(x is None for x in prices):
        return "missing_or_nonfinite_price"
    if any(not 0 < x < 100 for x in prices):
        return "price_outside_binary_range"
    if prices[1] > prices[0] or prices[3] > prices[2]:
        return "crossed_price"
    return None


def depth_reason(sizes):
    if any(x is None for x in sizes):
        return "missing_or_nonfinite_depth"
    if any(x <= 0 for x in sizes):
        return "nonpositive_depth"
    return None


def connect_readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    if db.execute("SELECT value FROM meta WHERE key='complete'").fetchone() != ('true',):
        db.close()
        raise ValueError("incomplete_coverage_ledger")
    return db


def inventory(db):
    return [json.loads(x[0]) for x in db.execute("SELECT info FROM files ORDER BY path")]


def required_files(files, dates):
    """Select by parsed UTC market OR sample date, including malformed rows."""
    wanted = set(dates)
    return {f["path"] for f in files if wanted & (set(f["market_dates"]) | set(f["sample_dates"]))}


def omission_audit(files, previous_protocol):
    dates = previous_protocol["train_dates"] + previous_protocol["validation_dates"]
    expected = required_files(files, dates)
    previous = {x["path"] for x in previous_protocol["files"]}
    return {"existing_but_omitted_files": sorted(expected - previous),
            "unneeded_selected_files": sorted(previous - expected),
            "expected_files": len(expected), "previous_files": len(previous),
            "scope": "UTC identity/time selection; equal file counts do not imply equal inventories"}


def verify_sources(root, files):
    root = Path(root).resolve()
    actual = sorted(str(x.relative_to(root)) for x in (root / "data/monthly_runs").rglob("*.csv"))
    if actual != sorted(f["path"] for f in files):
        raise ValueError("inventory_paths_changed")
    for f in files:
        path = (root / f["path"]).resolve()
        if root not in path.parents:
            raise ValueError("source_outside_root")
        data = path.read_bytes()
        if len(data) != f["bytes"] or hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ValueError("source_bytes_changed")


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    repo = Path(__file__).resolve().parents[1]
    if output == repo or repo in output.parents:
        raise ValueError("PLAINTEXT_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")
    paths = sorted((root / "data/monthly_runs").rglob("*.csv"))
    if not paths:
        raise ValueError("empty_source_inventory")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    db = sqlite3.connect(output)
    try:
        db.executescript("""
          PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
          CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE files (path TEXT PRIMARY KEY, info TEXT);
          CREATE TABLE windows (slug TEXT PRIMARY KEY, start INTEGER, raw_rows INTEGER,
            invalid_time_rows INTEGER, invalid_price_rows INTEGER, invalid_depth_rows INTEGER, file_count INTEGER);
          CREATE TABLE observations (slug TEXT, ts_us INTEGER, start INTEGER,
            prices TEXT, sizes TEXT, price_reason TEXT, depth_reason TEXT,
            repeats INTEGER DEFAULT 0, price_conflict INTEGER DEFAULT 0, depth_conflict INTEGER DEFAULT 0,
            PRIMARY KEY (slug, ts_us)) WITHOUT ROWID;
        """)
        total = Counter()
        for path in paths:
            if root not in path.resolve().parents:
                raise ValueError("source_outside_root")
            data = path.read_bytes()
            reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
            sample_dates, market_dates, reasons = Counter(), Counter(), Counter()
            win = defaultdict(Counter)
            values, n = [], 0
            for row in reader:
                n += 1
                ts = utc_us(row.get("ts_iso"))
                slug, start = market_start(row)
                if ts is not None:
                    sample_dates[utc_day(ts / 1000000)] += 1
                else:
                    reasons["invalid_sample_time"] += 1
                if start is None:
                    reasons["invalid_market_identity"] += 1
                    continue
                market_dates[utc_day(start)] += 1
                w = win[(slug, start)]
                w["raw_rows"] += 1
                prices = tuple(number(row.get(x)) for x in PRICE_FIELDS)
                sizes = tuple(number(row.get(x)) for x in SIZE_FIELDS)
                pr, dr = price_reason(prices), depth_reason(sizes)
                w["invalid_price_rows"] += int(pr is not None)
                w["invalid_depth_rows"] += int(dr is not None)
                reasons.update(x for x in (pr, dr) if x)
                if ts is None or not start * 1000000 <= ts < (start + 300) * 1000000:
                    w["invalid_time_rows"] += 1
                    reasons["outside_or_invalid_window_time"] += 1
                    continue
                values.append((slug, ts, start, canonical(prices), canonical(sizes), pr, dr))
            db.executemany("""INSERT INTO observations
              (slug,ts_us,start,prices,sizes,price_reason,depth_reason) VALUES (?,?,?,?,?,?,?)
              ON CONFLICT(slug,ts_us) DO UPDATE SET repeats=observations.repeats+1,
                price_conflict=observations.price_conflict OR observations.prices != excluded.prices,
                depth_conflict=observations.depth_conflict OR observations.sizes != excluded.sizes""", values)
            db.executemany("""INSERT INTO windows VALUES (?,?,?,?,?,?,1)
              ON CONFLICT(slug) DO UPDATE SET raw_rows=windows.raw_rows+excluded.raw_rows,
                invalid_time_rows=windows.invalid_time_rows+excluded.invalid_time_rows,
                invalid_price_rows=windows.invalid_price_rows+excluded.invalid_price_rows,
                invalid_depth_rows=windows.invalid_depth_rows+excluded.invalid_depth_rows,
                file_count=windows.file_count+1""",
                [(slug, start, w["raw_rows"], w["invalid_time_rows"], w["invalid_price_rows"], w["invalid_depth_rows"])
                 for (slug, start), w in win.items()])
            info = {"path": str(path.relative_to(root)), "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "rows": n,
                    "fields": reader.fieldnames, "sample_dates": dict(sample_dates),
                    "market_dates": dict(market_dates), "row_reasons_overlap": dict(reasons)}
            db.execute("INSERT INTO files VALUES (?,?)", (info["path"], canonical(info)))
            total.update(rows=n, files=1)
            total.update(reasons)
            db.commit()
        files = inventory(db)
        verify_sources(root, files)
        db.execute("CREATE INDEX observation_start ON observations(start)")
        db.execute("INSERT INTO meta VALUES ('inventory_sha256',?)", (hashlib.sha256(canonical(files).encode()).hexdigest(),))
        db.execute("INSERT INTO meta VALUES ('totals',?)", (canonical(dict(total)),))
        db.execute("INSERT INTO meta VALUES ('complete','true')")
        db.commit()
    finally:
        db.close()


def coverage_report(db):
    files = inventory(db)
    meta = dict(db.execute("SELECT key,value FROM meta"))
    windows = {s: {"market": slug, "raw_rows": n, "invalid_time_rows": it,
                    "invalid_price_rows": ip, "invalid_depth_rows": idepth, "source_files": nf}
               for slug, s, n, it, ip, idepth, nf in db.execute("SELECT * FROM windows")}
    for start, n, repeats, prices, depths, pc, dc in db.execute("""SELECT start,COUNT(*),SUM(repeats),
        SUM(price_reason IS NULL AND NOT price_conflict),
        SUM(price_reason IS NULL AND depth_reason IS NULL AND NOT price_conflict AND NOT depth_conflict),
        MAX(price_conflict),MAX(depth_conflict) FROM observations GROUP BY start"""):
        windows[start].update(unique_timestamps=n, duplicate_rows=repeats,
                              valid_price_observations=prices, valid_depth_observations=depths,
                              price_conflict=bool(pc), depth_conflict=bool(dc))
    days, ledger = defaultdict(Counter), []
    if windows:
        first = min(windows) // 86400 * 86400
        last = max(windows) // 86400 * 86400 + 86400
        for start in range(first, last, 300):
            item = {"market": f"btc-updown-5m-{start}", "start": start, "utc_date": utc_day(start),
                    "raw_rows": 0, "unique_timestamps": 0, "duplicate_rows": 0,
                    "invalid_time_rows": 0, "invalid_price_rows": 0, "invalid_depth_rows": 0,
                    "valid_price_observations": 0, "valid_depth_observations": 0,
                    "price_conflict": False, "depth_conflict": False}
            item.update(windows.get(start, {}))
            item["status"] = ("completely_absent" if not item["raw_rows"] else
                              "present_invalid_time_only" if not item["unique_timestamps"] else
                              "present")
            ledger.append(item)
            c = days[item["utc_date"]]
            c.update(scheduled_windows=1, raw_rows=item["raw_rows"], unique_timestamps=item["unique_timestamps"],
                     duplicate_rows=item["duplicate_rows"], present_windows=int(item["raw_rows"] > 0),
                     invalid_time_rows=item["invalid_time_rows"], invalid_price_rows=item["invalid_price_rows"],
                     invalid_depth_rows=item["invalid_depth_rows"],
                     absent_windows=int(not item["raw_rows"]),
                     any_valid_price_windows=int(item["valid_price_observations"] > 0 and not item["price_conflict"]),
                     any_valid_depth_windows=int(item["valid_depth_observations"] > 0 and not item["price_conflict"] and not item["depth_conflict"]),
                     price_conflict_windows=int(item["price_conflict"]),depth_conflict_windows=int(item["depth_conflict"]))
    return {"version": 1, "scope": "UTC sample coverage, not independent market evidence",
            "inventory_sha256": meta["inventory_sha256"], "totals": json.loads(meta["totals"]),
            "files": files, "by_date": dict(sorted(days.items())), "window_ledger": ledger,
            "future_labels": "Not evaluated by coverage. Decision/label exclusions are separate in the diagnostic.",
            "row_validity_counts": "Raw overlapping reasons; valid unique counts exclude conflicting keys before whole-window quarantine."}


def load_dates(db, dates):
    """One daily chunk at a time; keep invalid observations for first-quote rules."""
    grouped = defaultdict(list)
    price_conflicts, depth_conflicts, present = set(), set(), set()
    for date in sorted(set(dates)):
        low = utc_us(date + "T00:00:00Z") // 1000000
        high = low + 86400
        present.update(x[0] for x in db.execute("SELECT slug FROM windows WHERE start>=? AND start<?", (low, high)))
        for slug, ts, prices, sizes, pr, dr, pc, dc in db.execute("""SELECT slug,ts_us,prices,sizes,
            price_reason,depth_reason,price_conflict,depth_conflict FROM observations
            WHERE start>=? AND start<? ORDER BY start,ts_us""", (low, high)):
            grouped[slug].append({"ts": ts / 1000000, "values": tuple(json.loads(prices) + json.loads(sizes)),
                                  "price_reason": pr, "depth_reason": dr})
            if pc:
                price_conflicts.add(slug)
            if dc:
                depth_conflicts.add(slug)
    return grouped, price_conflicts, depth_conflicts, present


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--poly-root", type=Path, required=True)
    p.add_argument("--database", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)
    build(args.poly_root, args.database)
    with connect_readonly(args.database) as db:
        write_private(args.output, coverage_report(db))
    print("PRIVATE_COVERAGE_COMPLETE; no empirical contents printed.")


if __name__ == "__main__":
    main()

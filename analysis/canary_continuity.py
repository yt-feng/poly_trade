"""Explicit sampled-second masks for offline BTC research, never inferred from jitter."""
from bisect import bisect_right
from datetime import date, datetime, timezone
import hashlib
import json
import re


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:raise ValueError('Duplicate quality report field: '+key)
        result[key] = value
    return result


class GapMask:
    """Known missing UTC seconds as half-open millisecond intervals."""

    def __init__(self, intervals=()):
        spans = []
        for start, end in intervals:
            if type(start) is not int or type(end) is not int or start >= end:
                raise ValueError('Invalid half-open gap interval')
            spans.append((start, end))
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        self.intervals = tuple(merged)
        self.starts = tuple(a for a, _ in merged)
        self.covered_days = None
        self.metadata = {'quality_report_supplied': False, 'known_gap_intervals': len(merged),
                         'quality_report_covered_utc_days': [], 'full_history_complete': False,
                         'gap_protection_scope': 'explicit_intervals_only' if merged else 'no_known_gap_mask'}

    @classmethod
    def from_report(cls, path=None):
        if path is None:
            return cls()
        raw = path.read_bytes()
        report = json.loads(raw, object_pairs_hook=unique_fields)
        if (not isinstance(report, dict) or type(report.get('schema_version')) is not int or report['schema_version'] != 2 or
                type(report.get('quality_contract_version')) is not int or report['quality_contract_version'] != 3 or
                report.get('report_kind') == 'daily_quality_failure' or
                report.get('quality_report_complete') is False or
                not isinstance(report.get('daily'), list) or
                not all(isinstance(d, dict) and isinstance(d.get('asset'), str) for d in report['daily'])):
            raise ValueError('Expected a quality_v2 schema 2 / contract 3 daily report')
        days = [d for d in report['daily'] if d['asset'] == 'btc']
        if not days:
            raise ValueError('Quality report has no BTC coverage')
        intervals = []; covered_days = {}; dates = []
        for day in days:
            day_text = day.get('date_utc')
            if not isinstance(day_text, str) or date.fromisoformat(day_text).isoformat() != day_text:
                raise ValueError('Expected an ISO UTC coverage date')
            day_start = int(datetime.fromisoformat(day_text).replace(tzinfo=timezone.utc).timestamp())
            if day_start in covered_days:
                raise ValueError('Duplicate BTC coverage date')
            covered_days[day_start] = day_text; dates.append(day_text)
            counts = [day.get(k) for k in ('expected_seconds', 'observed_seconds', 'missing_seconds')]
            if (any(type(v) is not int for v in counts) or counts[0] != 86400 or
                    not 0 <= counts[1] <= 86400 or counts[1] + counts[2] != 86400 or
                    not isinstance(day.get('missing_intervals'), list)):
                raise ValueError('Invalid daily distinct-second coverage')
            previous_end = day_start; missing = 0
            for gap in day['missing_intervals']:
                if not isinstance(gap, dict):raise ValueError('Invalid gap entry')
                start, end = gap.get('start_second'), gap.get('end_second_exclusive')
                if (type(start) is not int or type(end) is not int or
                        not day_start <= start < end <= day_start + 86400 or start < previous_end or
                        ('duration_seconds' in gap and (type(gap['duration_seconds']) is not int or gap['duration_seconds'] != end-start))):
                    raise ValueError('Invalid or overlapping UTC-day gap boundaries')
                intervals.append((start * 1000, end * 1000))
                previous_end = end; missing += end-start
            if missing != counts[2]:raise ValueError('Gap intervals disagree with missing_seconds')
        mask = cls(intervals)
        mask.covered_days = covered_days
        mask.metadata.update(quality_report_supplied=True, quality_report_path=str(path),
                             quality_report_sha256=hashlib.sha256(raw).hexdigest(),
                             quality_report_covered_utc_days=sorted(dates),
                             gap_protection_scope='reported_btc_days_only')
        return mask

    def intersects(self, first_ms, last_ms):
        """Does a closed dependency span touch a known half-open gap?"""
        i = bisect_right(self.starts, last_ms) - 1
        return i >= 0 and self.intervals[i][1] > first_ms

    def check_row(self, row):
        ms = row['sample_ms']
        if type(ms) is not int or ms < 0:raise ValueError('sample_ms must be a nonnegative integer')
        if self.covered_days is not None:
            if ms // 86400000 * 86400 not in self.covered_days:
                raise ValueError(f'Quality report does not cover snapshot UTC day at {ms}')
            match = re.fullmatch(r'btc-updown-5m-([0-9]+)', str(row.get('slug', '')))
            start = int(match[1]) * 1000 if match else -1
            if (row.get('asset') != 'btc' or start % 300000 != 0 or not start <= ms < start + 300000 or
                    ('window_start_ms' in row and row['window_start_ms'] != start)):
                raise ValueError('Snapshot asset/market/window does not match BTC 5m quality coverage')
        if self.intersects(ms, ms):
            raise ValueError(f'Snapshot {ms} conflicts with known missing-second mask')


def continuous_path(rows, first, last, gaps):
    """Future labels and valuations must stay within one observed market path."""
    if gaps.intersects(rows[first]['sample_ms'], rows[last]['sample_ms']):
        return False
    identity = (rows[first].get('asset'), rows[first]['slug'])
    return all((rows[k].get('asset'), rows[k]['slug']) == identity and
               0 < rows[k]['sample_ms'] - rows[k-1]['sample_ms'] <= 2500
               for k in range(first+1, last+1))

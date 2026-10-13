# Explicit quality-report input

The offline `canary_readiness.py`, `canary_shadow.py`, and
`forward_roll_conflict.py` entrypoints accept `--quality-report PATH`.
The file must be a local `quality_v2` JSON report with `schema_version=2`,
`quality_contract_version=3`, and BTC daily coverage records.

Each record must include `date_utc`, `expected_seconds=86400`,
`observed_seconds`, `missing_seconds`, and `missing_intervals`.
Intervals use integer UTC seconds: `start_second` is inclusive and
`end_second_exclusive` is exclusive. Counts and intervals must agree.

An explicitly supplied missing, malformed, conflicting, or incompatible report
causes an error. Every input BTC snapshot must fall on a reported UTC date and
in the BTC five-minute market named by its timestamp. Partial date coverage
does not silently fall back to unmasked processing. A snapshot inside a known
missing interval is a conflict requiring reconciliation.

Known gaps cut reference history and invalidate dependent future labels and
quote-valuation paths. Existing time-jitter tolerance is preserved; a short
timestamp interval alone does not establish a missing observation. Continuous
same-asset references can span market windows, while future labels and quote
paths must remain within one market. Official resolution labels do not enter
the causal feature calculation.

Coverage diagnostics are available in readiness/forward coverage and each
shadow output row's `quality_report` field. They include the report SHA256,
reported UTC dates, and `gap_protection_scope`:

- No report: `no_known_gap_mask`.
- A validated report: `reported_btc_days_only`.
- `full_history_complete` remains false in both cases.

**Protection is conditional on explicitly supplying a valid report.** The
default acquisition and entrypoint commands do not automatically generate or
forward this report. Use the same verified snapshot selection to generate
coverage for every input date; a single capture segment's report is not a
full-history report. `poly/quality_v2.py` can generate reports from already
available local snapshots. This change does not modify acquisition or Actions.

These checks establish neither complete exchange-event capture nor joint
source availability. A complete sampling window does not prove all lookbacks
or future dependencies are usable. In streaming mode, an error after output
begins leaves a partial prefix, not a successful complete run.

All new tests use synthetic observations. The entrypoints remain offline
diagnostics and do not submit orders. Run the full local suite with:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

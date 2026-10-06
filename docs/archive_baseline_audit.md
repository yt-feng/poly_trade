# Archive baseline audit

`analysis/archive_baseline_audit.py` inventories existing `poly` and
`poly_trade` CSV archives and runs a fixed, quote-only sensitivity replay. It
never imports a trading client, network library, wallet, or credential.

The registered protocol is kept outside the checkout and sealed with the result.
The public manifest is only a pointer and checksum:
`research/strategy/runs/archive_baseline_20261004_manifest.json`.
The encrypted artifact is
`research/strategy/private/archive_baseline_20261004.compact.json.enc`.
Only the authenticated ciphertext is public. Do not decrypt it in CI or print
its contents.

The fixed diagnostic compares `no_trade`, `fixed_up`, `fixed_down`, and
`mid_momentum` over positive fee/slippage sensitivities and 5/10-share
scenarios with $10 starting cash. Entry and exit use delayed archived quotes;
missing or unknown exits are charged the full entry cost and halt that scenario.
No `final_price`, `target_price`, or settlement outcome is used in a signal or
payoff. The replay is therefore a hypothetical quote sensitivity study, not
execution evidence or a profitability claim.

## Data limitations found

The archived monthly raw CSVs cover 149 UTC row dates and 41,751 distinct
five-minute windows (5,839,327 rows). The selected seven-day diagnostic slice
contains 1,968 windows after timestamp conflict handling and 553,109 raw rows
before deduplication. Sampling is irregular, mostly 1–3 seconds, with longer
gaps. The raw schema has no per-record receive timestamp, source event time,
fee rate, minimum order size, tick size, condition ID, or private receipt ID.
The first quote is a sequentially fetched snapshot, so exact side-by-side
availability is unknown. Existing reports and strategy files already reference
many of the same source runs, so this cannot be called pristine out-of-sample.

The exact empirical values are in the encrypted artifact. The next honest
experiment is a pre-registered forward block from previously unseen v3 capture
with source/receive timestamps, contemporaneous fee and market-rule metadata,
explicit minimum-size checks, and a fixed strategy selected before that block.

To run a future baseline, provide canonical JSONL explicitly:

```bash
python analysis/archive_baseline_audit.py \
  --poly-root ../poly --trade-root . \
  --protocol /private/protocol.json \
  --v3-input /private/observations-v3.jsonl \
  --output /private/result.json
```

The runner passes only records accepted by `analysis/v3_data_contract.py` into
the quote adapter. Legacy CSV rows are quarantined instead of silently replayed.

## Existing-data quality audit without strategy evaluation

Use `--quality-date YYYY-MM-DD` to audit the mounted legacy CSV archive alone.
This mode does not require a trading protocol and never fits a strategy,
simulates orders, infers outcomes, or calculates returns:

```bash
python analysis/archive_baseline_audit.py \
  --poly-root /path/to/poly \
  --quality-date YYYY-MM-DD \
  --output /private/quality-audit.json
```

Every CSV row is examined for scope using its sample timestamp and BTC5m slug.
For the chosen date the report checks timestamp/window alignment, finite binary
quotes, positive touch sizes, crossed quotes and duplicate/conflicting quote
identities. A conflict excludes its entire window. Retention denominators and
non-exclusive exclusion reason counts are explicit. Descriptive windows with
at least one retained quote do not establish complete windows or execution
quality. Source file bytes are unchanged and hashes are rechecked after audit.

Legacy sample timestamps cannot establish per-feed source/receive availability;
market slugs alone cannot certify condition/token identity. Independent label
availability and fee/tick/minimum-size provenance remain separate requirements.
OI missingness is reported separately and never disqualifies a book-only
baseline; factors using OI require observed OI. Old data remains retrospectively
exposed. A mounted CSV is never silently substituted for an unavailable release
asset. Preserve any downloaded raw archive, manifest and sidecar before parsing.

The detailed result is written outside the repository with owner-only
permissions. Seal it to the existing, verified public recipient before
publication; never commit plaintext findings, private identities or archive keys.

## Frozen historical quote-factor diagnostic

`analysis/quote_factor_diagnostic.py` runs a bounded descriptive experiment from
an existing private protocol and a hash-pinned local CSV file list. It reuses
the archive timestamp/identity helpers and private-output writer. It does not
add a data collector, execution path, parameter search or settlement inference.

```bash
python analysis/quote_factor_diagnostic.py \
  --poly-root /path/to/poly \
  --protocol /private/frozen-protocol.json \
  --output /private/diagnostic.json
```

Freeze the dates, timing, few candidate feature sets, baselines and ablations
before evaluating the real data; seal that protocol before publishing it. The
source files are hashed before and after processing. Features use only sample
timestamps strictly before the decision. Future bid/ask changes are scoring
targets within the same market, never fills or financial returns. Actual feed
availability is not recoverable from legacy sample timestamps.

The output retains one ledger record for every scheduled BTC5m market on the
frozen dates, including absent windows, rejected decision/lookback quotes,
conflicting identities and unknown future quotes. It selects the scheduled
quote before inspecting its quality and never skips an invalid future quote to
find a favorable later label. OI and settlement fields are not read. All models
share one common feature/label cohort; unknown labels remain counted and get
explicit conservative error bounds rather than imputation. Train-only scaling,
ridge coefficients and factor cutpoints are frozen for validation. Temporal
blocks, per-date effects, baseline comparisons and leave-one-factor-out changes
are reported without retuning or declaring significance from a few dates.

The CLI prints only a completion marker and writes detailed results outside the
repository. Protocols and findings must be sealed with the existing recipient;
public run metadata must contain no factor results or private configuration.

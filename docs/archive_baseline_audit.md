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
This older helper reproduces the caller's frozen file list; it does not certify
that list's completeness. New archive research uses the complete UTC inventory
entrypoint below. Keep historical protocols intact when producing corrections.

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

## Complete UTC inventory and historical walk-forward

Build coverage from every existing CSV before choosing a diagnostic interval:

```bash
python analysis/archive_coverage_ledger.py \
  --poly-root /path/to/poly \
  --database /private/coverage.sqlite \
  --output /private/coverage.json
python analysis/historical_walk_forward.py \
  --poly-root /path/to/poly \
  --database /private/coverage.sqlite \
  --protocol /private/frozen-walk-forward.json \
  --output /private/walk-forward.json
```

The inventory parses timezone-aware sample times into UTC and uses market-start
identities. Neither filename dates nor raw timestamp prefixes select inputs.
The ledger pins the complete source tree and bytes, deduplicates market/UTC-time
keys, and distinguishes omitted existing files, absent calendar windows,
invalid fields/times and conflicting captures. Invalid first observations are
retained, so a diagnostic cannot skip to a more convenient future quote. A
window with only invalid timestamps is present-but-invalid, not absent.
Repeated captures never add independent market samples. Header presence does
not establish depth availability; depth values must be finite and positive.

Freeze explicit chronological train/validation dates, the unchanged candidate
family and estimator settings, and decision-time quote bins before evaluation.
Each validation market appears once in the walk-forward results. Scaling and
coefficients fit only the fold's training labels. Common price features and
depth-complete features use separate cohorts; all candidates and baselines
within a cohort share the same training and scoring rows. Cross-cohort scores
are not evidence of an incremental factor. Future price targets need no future
depth; a separately identified legacy correction mode preserves the older
depth-at-label restriction solely for like-for-like correction comparisons.

Reports include every scheduled window, per-date/continuous-block/quote-bin
paired errors, unknown-price counts, selection exclusions and conservative
missing-label sensitivity bounds. A corrected old interval stays a correction
version; historical exposure is not reset by new code or a different split.
Source/receive availability remains unverified. Nothing estimates fills, PnL
or capital growth, and no canary gate is modified.

Coverage databases and plaintext reports stay outside the checkout. Large
JSON journals may be gzip-compressed before sealing; their public manifest
records `gzip+json` so local decryption can be followed by decompression.
CI uses synthetic counterexamples only and never decrypts empirical journals.

## Spread hurdle from frozen predictions

```bash
python analysis/spread_hurdle_diagnostic.py \
  --poly-root /path/to/poly \
  --database /private/coverage.sqlite \
  --predictions /private/walk-forward.json \
  --protocol /private/frozen-spread-hurdle.json \
  --output /private/spread-hurdle.json
```

This entrypoint verifies the prior prediction file, ledger, original model
coefficients, features and exact quote timestamps. It never trains a model.
Every old scheduled window remains accounted for; an unexpected feature,
label, market or timestamp mismatch stops the diagnostic instead of silently
dropping rows. First-future-quote quality rules stay unchanged.

The observed quantity is future recorded bid minus decision recorded ask on
the same token. Up and Down use their own archived books. A reversed Up score
may be registered as a Down directional hypothesis, but it is not a calibrated
Down price forecast and never substitutes complementary synthetic prices.
Directions remain separate hypotheses without position selection or a combined
capital path. Small prediction-error improvements alone cannot demonstrate
that movement exceeds the observed spread.

Freeze every signal rule and hypothetical cost scenario before evaluating the
quote paths. Report all attempts, including zero-signal and zero-cost failures.
The gross spread gate is identical across cost scenarios; assumptions subtract
costs from that same selection without searching for another threshold. The
zero-cost case is optimistic. Assumed fees/slippage never become historical
actual charges merely because they appear in a report.

Activity-weighted proxy comparisons use the same labeled market denominator
for every candidate and the always-same-side/no-signal controls. Selected-only
means have different subsets and are not paired skill comparisons. Unknown
future quotes receive binary-price lower/upper bounds. Separate calendar stress
bounds retain windows whose signals cannot be reconstructed, under an explicit
hypothetical missing one-share exposure; these are not realized losses.

Budget arithmetic is a static one-share/top-of-book illustration. Reported size
is treated as shares only under a stated assumption; minimum order, historical
fees, tick size, source/receive timing and execution remain unverified. No fill,
portfolio PnL, capital reuse, turnover or small-account growth is inferred.


## Fixed-signal robustness checks

```bash
python analysis/quote_robustness.py \
  --poly-root /path/to/poly \
  --database /private/coverage.sqlite \
  --predictions /private/walk-forward.json \
  --prior-report /private/spread-hurdle.json \
  --protocol /private/frozen-robustness.json \
  --output /private/robustness.json
```

Freeze the diagnostic protocol before running. This consumes the exact previous
predictions and quote paths; it verifies coefficients, causal decision/lookback
indices, market identity, label timing, duplicates, crossed books and depth.
It does not refit or reselect signals. The original entry and the first/third
actual records strictly after decision are compared at the original fixed exit
record. Bad records count as steps and are never skipped. An entry at or after
the scheduled exit is unknown. Recorded intervals are sampling sensitivity,
not source-event latency or verified availability at decision time.

Every prior candidate, side and hypothetical cost remains reported. Price-only
and positive displayed-depth views are separate. A depth failure is unknown in
the latter, with conservative quote-domain bounds; positive depth cannot prove
an executable size or a fill. Each scenario retains its own unknowns, original
calendar coverage and shared candidate denominator. An additional intersection
of all delay scenarios supports paired delay comparisons without hiding the
observations excluded from that intersection.

The report includes medians, nearest-rank quantiles, tails, and largest absolute
and positive contribution shares. Positive-to-net ratios can exceed one when
losses offset gains; they are distinct from gross-positive shares. Uniform
largest-positive removal and leave-one-UTC-day/six-hour-block-out calculations
keep models fixed. All calendar groups, including inactive/missing groups,
remain visible; active-removal counts are separate. These describe concentration,
not cross-validation, confidence intervals or a statistical significance test.
All measurements are quote proxies in cents per hypothetical share, with no
PnL, promotion, trading or account-growth claim. A failed or unproven family may
remain a descriptive observation without proving every future variant impossible.


## Small price and clock hypothesis

```bash
python analysis/price_clock_diagnostic.py \
  --poly-root /path/to/poly \
  --database /private/coverage.sqlite \
  --legacy /private/walk-forward.json \
  --protocol /private/frozen-state-plan.json \
  --output /private/state-result.json
```

This bounded diagnostic uses recorded quotes and scheduled window time. The
midpoint is only a state feature, never an execution price or authenticated
probability. No official opening value or settlement label is inferred.
Both sides use their actual bid/ask and future quoted changes. A state/clock
mechanism can motivate a volatility hypothesis without implying profitable
mean reversion or directional drift; the latter must face its own test.

Freeze the small candidate family, chronological dates, observation states,
costs and descriptive screening checks before any empirical run. All states of
a market stay in the same date split. New coefficients and standardization use
only training markets with every registered state labeled; excluded training
markets are counted. Validation does not require future completeness. Report
state panels separately, because repeated market states are dependent and are
not portfolio transactions or additional independent trades.

The old fit file is a read-only negative control. Original-state features,
labels and predictions must match; applying the fixed old model at other states
is explicitly a transfer diagnostic. New candidates are compared with every
legacy candidate and the no-signal/always-side controls on shared per-scenario
samples. A fixed algebraic assumed-cost buffer is not optimized on validation;
cost and delay scenarios never reselect signals. Bid and ask MSE, including the
unprojected prediction, are secondary to actual quoted spread hurdles.

Each report retains complete calendar denominators, per-scenario unknowns,
first subsequent recorded-ask sensitivity with the original exit unchanged,
positive-depth checks, daily/block contribution removals, fold economics and
all fixed screening failures. Screening is descriptive and cannot promote a
candidate or change canary gates. Actual fees, minimum size, source/receive
availability and fills remain unverified. All empirical outputs and protocols
must be sealed under the existing research-vault policy.

## Frozen measurement audit

```bash
python analysis/quote_measurement_audit.py \
  --poly-root /path/to/poly \
  --database /private/coverage.sqlite \
  --frozen-report /private/state-result.json \
  --protocol /private/frozen-measurement-plan.json \
  --output /private/measurement-audit.json
```

This diagnostic consumes the existing frozen result. It neither fits models nor
recomputes economic scores. Decision offset is measured from the market slug's
window start; the forecast horizon is a separate parameter. Exit matching uses
the first recorded row at or after decision plus horizon, within the frozen
tolerance. Delayed entry never moves that exit. Invalid first records are kept.

The independent integer-UTC matcher checks feature eligibility, first-row
selection, labels, frozen-fit predictions and signal gates against the prior
result. It separates no future record, a late first record, missing own bid,
invalid own ask/crossed side, invalid opposite book, and the complete-book label.
Side-only availability measures the effect of the common-label requirement;
it is not a replacement label, revised signal, or evidence of an executable exit.
Zero/nonpositive depth remains separate from absent price. A later valid quote
is a recovery observation, never an imputed exit.

Panels retain planned, feature-valid, prediction-available, selected and
observed-path denominators, including unavailable price bins, by UTC date and
fixed price bins. Raw CSV cells, line numbers, source hashes and file spans
support each matched record. File endings and continued empty rows describe
what was recorded; CSV alone cannot distinguish venue closure, an empty book,
API failure, parsing loss, or collector shutdown. The old collector requests
the two sides sequentially and does not preserve their separate source/receive
timestamps or HTTP/venue status in these CSVs. Current source inspection does
not authenticate the exact source revision that produced each historical row.

Publish only generic tooling and synthetic tests. Seal protocols, complete
measurement ledgers and factual reviews under the existing vault policy. Any
recomputation required by a demonstrated implementation defect is a correction
on exposed data, never independent OOS evidence. Observed-subset positives are
not established alpha; unresolved missingness also does not disprove all signal.

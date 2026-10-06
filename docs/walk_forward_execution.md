# Event-clustered walk-forward execution evaluation

`analysis/walk_forward_execution.py` is an offline evaluator for canonical v3
book observations. It is a research adapter, not an order client, and it never
uses credentials, wallets, network calls, or live actions.

The feature file and resolution-label file are separate. Feature rows must
pass `analysis/v3_data_contract.py`; labels must conform to
`research/strategy/schema/walk_forward_label.schema.json`. A label can be used
for fitting only when `label_available_time_ms <= train_end_ms`. Outcome,
settlement, target, and PnL fields are rejected in v3 feature rows. Each
`market_id` is an event cluster and cannot appear in both a training and test
fold.

Folds are chronological expanding windows with an explicit purge interval and
embargo interval. Fitting uses only `received_time_ms < train_end_ms` and the
pre-cutoff labels. The reference model is intentionally small: a smoothed
empirical probability by as-of book-price/imbalance bucket. It reports Brier
score and expected calibration error (ECE) on test markets only.

The simulator uses displayed ask levels for buys and bid levels for sells. It
applies latency before looking for a later book, consumes available depth,
records partial fills, cancels any remainder at the configured expiry, checks
the current tick and minimum order size, and applies the market's USDC taker
fee metadata. Midpoint or synthetic prices raise `MID_PRICE_NOT_EXECUTABLE`.
Unknown fee exponents, changed rules, off-tick prices, and missing books fail
closed. Net metrics include gross settlement PnL, fees, net PnL, and net return
on filled cost. Settlement labels are used only after the prediction and are
not evidence of a real fill.

The fee formula follows the [official Polymarket fee documentation](https://docs.polymarket.com/trading/fees):
`fee = C × feeRate × p × (1-p)`, rounded to five decimals for the simulator.
Order price/tick/minimum-size and lifecycle semantics are based on the
[official place-orders documentation](https://docs.polymarket.com/trading/place-orders).
The simulator's short TTL is an offline sensitivity, not a claim about a live
GTD expiration; private order acknowledgements, trade IDs, cancel events, and
position reconciliation remain required for any canary.

Example:

```bash
python analysis/walk_forward_execution.py \
  --observations /private/observations-v3.jsonl \
  --labels /private/walk-forward-labels.jsonl \
  --manifest research/strategy/experiments/EXP-0002-btc5m-preregistered-reference.json \
  --code-commit '<40 lowercase hex>' \
  --evaluation-dates 2026-10-03,2026-10-04 \
  --output /private/walk-forward-report.json
```

The evaluator blocks when the manifest, exact data hashes, code identity,
registered UTC dates or parameter set are absent or changed. Use the
pre-registration command below to inspect that gate without fitting a model.

The manifest's `execution_realism` contract fixes ask/bid/depth execution and
forbids midpoint fills. It preregisters a 64-cell matrix over fee multiplier,
slippage ticks, latency, available depth, partial-fill policy and TTL, plus a
minimum-edge gate. Each cell must publish `gross_pnl_usdc`, `fee_usdc`,
`net_pnl_usdc`, `fill_rate`, `brier` and `ece`; incomplete sensitivity or
missing cost/edge inputs blocks the run.

With the current checkout there is no canonical v3 observation block plus
separate resolution-label file sufficient for a complete fold. The checked-in
report therefore remains blocked and contains null metrics rather than
fabricated Brier, ECE, or PnL values. Even a future successful public replay
will keep canary promotion blocked until private execution evidence passes the
existing ledger gates.

When either input is absent, `run_files` emits both file-specific blockers when
applicable (`missing_observation_file` and/or `missing_label_file`) and writes
an `input_requirements` object. That object is the minimum user handoff:
canonical v3 JSONL book snapshots with source/receive timestamps, market and
condition IDs, token IDs, executable bid/ask depth, tick/minimum-size and fee
metadata plus immutable provenance; and one independently sourced JSONL
resolution label per market with resolution time, availability time and a
source SHA-256. Only basenames are recorded in the public report. A complete
chronological fold still has to exist after purge/embargo, and these public
inputs do not waive the private order/fill/cancel/fee/settlement/account gates.
The report also repeats the canary context of 300 independent windows, 7 UTC
dates and 100 execution-evidence records; those promotion gates are explicitly
not enforced by this public replay.

Run the intake contract first when a new capture and label package arrives:

```bash
python analysis/walk_forward_intake.py \
  --observations /private/observations-v3.jsonl \
  --labels /private/resolution-labels.jsonl \
  --expected-observations-sha256 '<64 lowercase hex>' \
  --expected-labels-sha256 '<64 lowercase hex>' \
  --output /private/intake-report.json
```

The intake report checks the complete file hashes, every accepted v3 row's
provenance hash format (the source bytes are not silently assumed available),
field-level label validity, per-market timestamp monotonicity,
condition identity, and whether a label was available before an observation or
an observation occurred at/after resolution. It records only basenames and
aggregate IDs/counts in the report. `--synthetic` is reserved for offline
fixtures and forces `evidence_qualifies=false`; it never creates OOS metrics.
The output also carries an immutable `manifest_sha256` over its canonical JSON
body (excluding that self-digest), plus deterministic set digests for markets,
conditions, token roles and market/condition pairs. A changed input or edited
manifest therefore cannot be mistaken for the frozen package.

Run the split audit after intake and before fitting:

```bash
python analysis/walk_forward_split_audit.py \
  --observations /private/observations-v3.jsonl \
  --labels /private/resolution-labels.jsonl \
  --expected-observations-sha256 '<64 lowercase hex>' \
  --expected-labels-sha256 '<64 lowercase hex>' \
  --output /private/split-audit.json
```

The split audit uses `(market_id, condition_id)` clusters, never splits a
cluster across train/validation/test, and reports every boundary, purge and
embargo interval. Training labels must be available by the train cutoff;
validation/test labels must remain unavailable at their feature receive time.
Any cluster overlap, boundary crossing, purge/embargo occupancy or label-time
violation is a blocker. It is a structural audit only and emits no OOS metrics.

Before fitting any candidate, freeze the pre-registration manifest and run its
gate against the exact input files and code commit:

```bash
python analysis/preregistered_strategy.py \
  --manifest research/strategy/experiments/EXP-0002-btc5m-preregistered-reference.json \
  --observations /private/observations-v3.jsonl \
  --labels /private/resolution-labels.jsonl \
  --code-commit '<40 lowercase hex>' \
  --evaluation-dates 2026-10-03,2026-10-04 \
  --phase pre_evaluation --output /private/preregistration.json
```

The manifest fixes the causal feature and label cutoffs, every candidate in
the parameter grid, a validation-only selection metric and tie-break, the
observation/label hashes, code commit and UTC evaluation dates. A candidate
outside the grid, changed input/code identity, date outside the registration,
or any test-set selection is a blocker. The post-evaluation gate additionally
requires a machine-readable CSCV/PBO result; missing or malformed results are
reported as both a blocker and a multiple-testing caution. This protects the
walk-forward result from silently becoming a post-hoc search.

After a replay, run the post gate with the chosen registered candidate and
the validation-only selection record. Supply the CSCV result and its PBO
probability; a test-tuned candidate, an unregistered parameter, or omitted
diagnostics remains blocked:

```bash
python analysis/preregistered_strategy.py \
  --manifest research/strategy/experiments/EXP-0002-btc5m-preregistered-reference.json \
  --phase post_evaluation \
  --observations /private/observations-v3.jsonl \
  --labels /private/resolution-labels.jsonl \
  --code-commit '<40 lowercase hex>' --evaluation-dates 2026-10-03,2026-10-04 \
  --parameters '{"edge_buffer":"0","latency_ms":1000,"order_size":"5","order_ttl_ms":5000,"train_duration_ms":3600000,"test_duration_ms":900000,"purge_ms":300000,"embargo_ms":1000,"min_training_events":10,"calibration_bins":10}' \
  --selection-stage validation_only_pre_registered \
  --selection-metric brier --selection-metric-source validation \
  --pbo-probability '<0..1>' --cscv-result /private/cscv.json \
  --stress-output /private/stress-output.json \
  --output /private/preregistration-post.json
```

The checked-in
[`preregistration_synthetic_blocked.json`](../reports/walk_forward_execution/preregistration_synthetic_blocked.json)
only exercises the contract. It is marked synthetic, has no OOS metrics, and
cannot qualify as canary evidence.

## Execution-path corrections

The execution CLI runs intake and audits the actual train/test folds before
fitting. Any quarantined observation or label, missing/mismatched identity,
reused condition/token identity, non-monotonic source/receive time, label known
at feature receipt, or observation received at/after resolution blocks the
whole evaluation. Feature extraction failures and invalid folds cannot shrink
the profit sample silently. Small historical samples may still be inspected;
the 300-window/7-date promotion thresholds remain reported in intake. Purge and
embargo exclusions are counted explicitly; used clusters cannot cross fold
boundaries. The separate three-way split audit remains available for candidate
selection; execution does not pretend it ran a validation slice it never used.

Book touches use only positive-size levels, sorted by executable price. A
zero-size level cannot create an edge or change the chosen limit price. A
post-order simulated fill requires source time at/after activation, source time
no later than receipt, and both source age and observation gap within 1,500 ms.
Missing coverage, pre-order delayed messages, absent positive depth, or changing
rules/fees yield `unknown_execution`. These attempts are counted; any unknown
attempt leaves aggregate cost/return/PnL null. A simulated unfilled expiry needs
fresh causal observations through TTL, not an empty quote list. Partial-fill
cancellation remains a simulation assumption, never a venue acknowledgement.

Open interest is not an input to this book-only reference policy. A historical
quality audit must report OI availability separately; only factors explicitly
using OI require it. No OI field is fabricated or added to the v3 book contract.

Regression commands (offline; no credentials):

```bash
python -m unittest discover -s tests -p 'test_execution_path_regressions.py' -v
python -m unittest discover -s tests -p 'test_*.py'
```

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
  --output /private/walk-forward-report.json
```

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

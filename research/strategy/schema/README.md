# v3 observation contract

`v3_observation.schema.json` defines the minimum record needed for a future
honest out-of-sample replay. Every record carries source event time, local
receive time, market and condition identity, both token IDs, quote/trade type,
book sides and levels (for snapshots), fee parameters, tick size, minimum order
size, and immutable provenance (`source`, `capture_id`, source SHA-256, and
retrieval time).

`analysis/v3_data_contract.py` validates the schema offline and emits a
quarantine report with timestamp latency percentiles and leakage checks. It
rejects missing rules, event timestamps arriving after receive time, excessive
latency, duplicate/conflicting IDs, and future-label fields such as
`final_price`, `outcome`, or `target_price`. Quarantined records are never
passed to a strategy replay. The historical CSV archives do not satisfy this
contract and remain untouched.

## Walk-forward labels

`walk_forward_label.schema.json` is deliberately separate from the v3 feature
records. A label identifies one market's resolved outcome and records both its
resolution time and the time it became available to the researcher. The
walk-forward evaluator joins a label only after its `label_available_time_ms`
and never allows outcome, settlement, or target fields inside feature rows.

## Pre-registered strategy manifests

`preregistered_strategy.schema.json` defines the immutable strategy hypothesis
manifest. It records the causal feature/label cutoffs, complete parameter
grid, validation-only selection rule, input hashes, code commit and UTC
evaluation dates. The manifest also requires CSCV/PBO multiple-testing control;
`analysis/preregistered_strategy.py` enforces these fields and emits no OOS
metrics.

`execution_realism.schema.json` is referenced by the pre-registration schema.
Its six-dimensional matrix covers fee multiplier, slippage ticks, latency,
available depth, partial-fill policy and TTL. Every cell has gross/fee/net PnL,
fill rate, Brier and ECE output slots; synthetic runs must leave all slots
`null`.

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

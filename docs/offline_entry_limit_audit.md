# Frozen-candidate entry-limit audit

This offline runner compares two execution policies on an unchanged, previously
recorded candidate stream. It does not fit or query a predictive model, collect
new market data, authenticate fills, or submit orders.

The control uses the existing delayed-entry replay. The intervention fixes an
economic buy-price ceiling from the original decision probability, registered
net-edge floor and fee rate. An arrival fill above that ceiling is cancelled
before sizing or debiting cash. Future exits and terminal outcomes do not enter
the entry cancellation rule. Both arms retain the same exit, sizing and cash
release assumptions.

For price `p`, probability `q`, edge floor `e` and fee rate `r`, the continuous
ceiling solves `p + r*p*(1-p) <= q-e`. It is a simulation bound. A real signed
order also requires market-specific tick, quantity, fee and execution metadata.
The runner does not infer these missing facts from a successful replay.

The private input binds source and code commits, the parent summary and candidate
CSV digests, a canonical digest of the exact projected candidate records, and the
original run/block order. Only the execution fields are projected; values retain
their original CSV strings until explicit validation and conversion. Quote data
comes from the pinned source run for that market, without rebuilding features or
selecting candidates again.

The complete control result must reproduce the registered parent fingerprint
and candidate counts before a paired comparison is admitted. A completed replay
with a control mismatch is retained as an invalid comparison. Empty or failed
runs cannot claim strategy evaluation. Model fit and prediction call counts must
remain zero for this workflow.

`offline-entry-limit-audit.yml` accepts a digest of the registered input. The
input itself is gzip-compressed and base64-encoded in the dedicated
`OFFLINE_ENTRY_LIMIT_CONFIG_B64` Actions secret. Decoding bounds both compressed
and expanded sizes and rejects trailing or truncated streams before checking
the exact plaintext digest. This secret is separate from other offline audits.

Evaluation stdout, stderr, frozen input and full results remain runner-local
until sealed. The workflow retains only ciphertext artifacts, then uses the
existing idempotent journal publisher and removes runner-local plaintext even
when setup or evaluation fails. Public status records contain no candidate,
model, wallet or outcome details.

Historical replay, authenticated live fills, official resolution and verified
cash availability are separate evidence classes. Related stress cells reuse
observations and must not be counted as independent trading trials.

# Strategy experiment ledger

- `experiments/` contains pre-registered JSON hypotheses and fixed protocols.
- `runs/` contains completed run manifests and output checksums.
- `private/` is local-only; only authenticated ciphertext (`*.enc`) may be retained.
- `schema/` records the required keys checked by offline CI.

`EXP-0002-btc5m-preregistered-reference.json` is the machine-readable
pre-registration for the BTC 5-minute reference hypothesis. It freezes the
causal feature/label cutoffs, parameter grid, validation-only selection rule,
input hashes, code commit and UTC dates. `analysis/preregistered_strategy.py`
must pass before a replay is fitted; its post-evaluation phase blocks
unregistered parameters, test-set or post-hoc selection, and missing CSCV/PBO
multiple-testing diagnostics. The checked-in synthetic report is structural
only and contains no OOS or canary metric.

Do not mix BTC 5-minute strategy evidence with `equity_daily`. Source public
capture releases from `yt-feng/poly` by immutable tag/commit and record that
identity in every run manifest.

The registered experiment separates `pre_canary_research` from
`post_canary_completion`: ten completed canary round-trips are measured only
after a canary run, while the statistical and data-quality gates remain
required before the first-canary review. Neither phase authorizes live orders.

`archive_baseline_20261004_manifest.json` points to the encrypted result of the
first fixed archive inventory and quote-only baseline. The sealed result records
source commits, date/window coverage, sampling gaps, missing execution metadata,
cost sensitivities, $10/5-or-10-share constraints, leakage limits, and the next
forward-data contract. It contains no execution evidence or trading approval.

# Strategy experiment ledger

- `experiments/` contains pre-registered JSON hypotheses and fixed protocols.
- `runs/` contains completed run manifests and output checksums.
- `private/` is local-only; only authenticated ciphertext (`*.enc`) may be retained.
- `schema/` records the required keys checked by offline CI.

Do not mix BTC 5-minute strategy evidence with `equity_daily`. Source public
capture releases from `yt-feng/poly` by immutable tag/commit and record that
identity in every run manifest.

The registered experiment separates `pre_canary_research` from
`post_canary_completion`: ten completed canary round-trips are measured only
after a canary run, while the statistical and data-quality gates remain
required before the first-canary review. Neither phase authorizes live orders.

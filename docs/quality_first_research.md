# Quality-first sealed research

The manual `canary-readiness-research` workflow calls `quality-first-research`.
It evaluates bounded public observations and delivers encrypted results. A
successful data audit alone does not mean a strategy was evaluated. This path
has no order, wallet or signing integration.

## Data and evaluation contract

Select complete snapshot shards with equal byte quotas across available UTC
dates and dispersed positions within each date. Fetch source checksums before
reusing cached public bytes. Preserve the acquisition plan, SHA256 identities,
duplicate counts and sampling scope. Missing history is not filled or repaired.
The loader retains a declared compact view after checking full-file integrity
and full-row conflicts; it fails explicitly at its row budget.
Evaluation scope is fixed by the selected archive frames: only whole markets
inside those frames can open new simulated positions. Leading/trailing partial
markets are counted as sampling-boundary exclusions. Missing observations
inside a frame do not split it or remove a failed trade. Training can still use
high-quality segments from a partial market.

Normalize identity, clocks, fees and real two-sided quotes separately from
candidate feature requirements. Fit only on uninterrupted high-quality segments
of at least 90 seconds. A training target must be in the same quality segment
and available before the fit cutoff. Split entire markets chronologically, with
a purge interval; validation and test decisions are never removed because a
later quote is missing. Quality segment membership is retrospective and can be
used only for fitting.

The initial fixed comparison is a regularized forecast of the next 30-second
received-quote midpoint change, using book features and optionally the causal
five-second spot return. It is a quote-residual experiment, not an official
resolution predictor. A no-move market baseline is deducted through current
spread, fees and a tick allowance. The small threshold set is fixed in source.
Select on validation, evaluate the chosen model once on the chronological
holdout, and freeze a no-trade model when none passes. No train/validation/test
partition of existing archives is described as prospective evidence.

The separate complete-set diagnostic records necessary two-sided quote
conditions, then checks their first observed arrival snapshot after one and
three seconds, including a further tick on each leg. It retains every original
candidate and failed leg. It cannot establish atomic fills, merge costs or cash timing.
See [primary research sources](quality_first_research_sources.md) for other
mechanisms and the conditions required to evaluate them.

## Small cash account model

The default initial cash is USD 10. Use observed CLOB-book minimum share units;
the Gamma minimum field alone does not establish a share quantity. Enforce known
notional and quantity-step constraints. Deduct entry principal and fees, require
fresh delayed quotes and sufficient displayed depth, and apply a limit-price
ceiling. Each market permits at most one intent, with one pending position at a
time. Cash cannot be reused until the modeled exit cash time. Unknown exits keep
their cost locked and remain in the conservative result. Fee changes and invalid
exit conditions are not silently dropped.

Report actual fills as zero. These are taker quote proxies, with a modeled cash
delay and no maker rebate credit. Compare one-second execution with a
three-second/extra-exit-tick stress, and show the result excluding the largest
win. A small account reaching a target in replay is not a forecast of the time
needed to reach it in an account. No test pass enables a live canary.

## Sealed delivery and frozen evaluation

Provide an existing X25519 public recipient JSON as base64 and its independently
verified fingerprint, or use the configured `QUALITY_FIRST_RECIPIENT_JSON_B64`
and `QUALITY_FIRST_RECIPIENT_ID` repository variables. Those variables contain
public encryption material only. The private identity stays with the recipient. Invalid
recipient input fails before acquisition; sealing failure never falls back to
plaintext. Public logs and artifacts contain only generic operational status and
encrypted parts. The analysis subprocess does not inherit the GitHub token.

To recover a delivery, decrypt each `part-XXXX.vault` with the recipient's
private identity, verify its part format/index/count and common bundle hash,
concatenate decoded `payload_base64` chunks in index order, and verify the
SHA256 of the resulting tar.gz before extracting. The package contains the
source acquisition plan/manifest, diagnostics, `results/research.json`, and a
hash-bound `results/frozen_model.json`.

The analysis CLI and runner accept `--frozen-model PATH`. They evaluate only
whole markets after the frozen timestamp and consumed-data watermark and do
not refit. The frozen file binds the exact policy and implementation hashes;
changed execution rules or starting cash require a new episode. Repeated
evaluations replay one episode and must not be added together as new evidence.
A bounded challenger's top-level quality funnel, window counts and observed
time range describe only fully contained market windows and their samples in
`[begin, end)`. `input_summary` retains the corresponding full-input audit.
Causal features keep their preceding history. `input_reaches_episode_end`
requires an in-episode observation in its final second; it does not establish
continuous coverage or strategy usability.
The runner validates `research.json` before reporting strategy evaluation.
Waiting and zero-observation episodes produce `complete` delivery status with
`strategy_evaluated=false`; unknown or inconsistent result metadata fails the
research stage. A bounded replay with observations is evaluated even when it
produces no trade attempts or actual fills.
A frozen file must be supplied through a private execution context;
never place it in public workflow inputs, source commits or plaintext artifacts.
The manual workflow currently performs discovery and produces the frozen file;
it does not automatically persist a confidential model across scheduled runs.

Run synthetic contracts with:

```sh
python -m unittest discover -s tests -p 'test_quality_first*.py' -v
```

Run substantial acquisition and evaluation in Actions. Keep the older strategy
registries and their evaluation criteria unchanged.

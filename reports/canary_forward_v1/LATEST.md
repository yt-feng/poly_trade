# Canary Forward v1 — latest checkpoint

Generated from GitHub Actions run `35836887536` on 2026-09-23 at 08:24:35 UTC.

- Registry: `canary_forward_v1`
- Primary: `roll_conflict / taker / 30s / 1s`
- Stress: same signal with 3s nominal entry/exit latency and an extra-exit-tick view
- Registry commit time: 2026-09-23 08:17:12 UTC
- Locked whole-window forward boundary: 2026-09-23 08:30:00 UTC
- Checksum-verified production v3 available to this run: 10,805 rows across 37 windows
- Eligible post-boundary rows/windows at this checkpoint: **0 / 0**
- Candidate attempts/valued attempts: **0 / 0**
- `human_review_evidence_ready`: **false**
- `live_canary_eligible`: **false**

The zero is expected because this checkpoint completed before the pre-registered forward boundary. No pre-boundary return is counted, even though it is available for causal warm-up. This is not evidence for or against the candidate.

The acquisition path explicitly paginated GitHub release assets, verified source SHA-256 sidecars, and acquired snapshot/label archives without downloading private data. Regression tests, acquisition, evaluation and the non-activating decision check all passed.

A later checkpoint must preserve the registered candidate, boundary, fee/depth/latency/missing-exit rules, block confidence intervals, fixed monitoring checkpoints and no-substitution rule. Real trading remains outside this module and requires separate human approval plus private execution reconciliation.

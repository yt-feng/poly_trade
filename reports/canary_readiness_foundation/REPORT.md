# Canary readiness foundation report

**As of 2026-10-04 UTC: blocked.** This report records the current documented
boundary; it is not a live-trading approval and does not claim that any order
was sent or filled.

The promotion contract requires at least **300 independent windows across 7 UTC
dates**, **100 execution-evidence attempts**, positive cost-adjusted performance
with a positive lower bound, positive 3-second-delay and extra-exit-tick stress
results, and at least **99% exit reconciliation**. It also requires private
order, fill, cancel, fee, settlement, and account reconciliation. Unknown exits
remain losses for the lower-bound calculation.

The repository evidence currently records **zero confirmed real fills and zero
private receipts**. Existing quote replays and paper/agent scenarios are useful
for research, but their negative or non-promotable results cannot be relabelled
as live performance. Public quotes alone are explicitly insufficient: a quote is
not an order acknowledgement, a fill, a fee receipt, or a settlement record.

The current engineering boundary also remains visible: poly PR #17 is still
documented as draft/open with offline tests passing while live API behavior is
unverified; logging PRs #7 and #9 are merged, while poly PR #6 (preserve live
health before capture shutdown) and issue #3 (REST books changing while WS is
inactive) remain open around WS freshness. CI success means the code and offline
contracts ran successfully, never that canary gates passed.

Re-evaluate this report only from a new manifest that pins the source commit,
UTC windows and dates, exact command, raw-input checksums, cost model, and
private execution reconciliation. Do not place real trades as part of this
foundation change.

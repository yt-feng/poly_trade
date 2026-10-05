# Walk-forward execution report

`blocked_report.json` is the current public-safe checkpoint for the
event-clustered evaluator. It records zero input observations and zero labels,
so Brier, ECE, and cost-adjusted PnL are intentionally `null`. The evaluator
cannot manufacture an out-of-sample result from the legacy CSV archive because
those rows lack the v3 event/receive, fee, rule, and provenance contract.

The `input_requirements` object is the machine-readable handoff for the next
run. It reports both missing files separately and lists the minimum canonical
observation and independent-resolution-label fields without publishing local
absolute paths. Historical CSV, midpoint-only quotes, public prices without
resolution provenance, and unresolved markets remain insufficient. The same
object records the purge/embargo and training-event settings used by the
evaluator.

Provide a previously unseen canonical v3 JSONL block and a separate, hashed
resolution-label JSONL file before generating a new report. Public replay
metrics will still not satisfy the existing private-fill canary gate.

Use `analysis/walk_forward_intake.py` as the reproducible handoff before
`walk_forward_execution.py`. Supply both file SHA-256 values when the capture
package is frozen. The intake fails closed on invalid v3 fields, invalid label
fields, mismatched condition IDs, non-monotonic market time, labels available
before feature receipt, observations at/after resolution, and future-label
fields. Synthetic fixture runs may be marked explicitly with `--synthetic`,
but the resulting report is always non-evidence and contains no OOS metrics.
The checked-in [`intake_synthetic_blocked.json`](intake_synthetic_blocked.json)
is one such fixture run: it proves the contract and leakage checks execute, but
its one window/one date is explicitly blocked and cannot be used as evidence.
The report's `manifest_sha256` covers the canonical report body; coverage set
digests make market/condition/token identity changes visible without publishing
the full private dataset.

`analysis/walk_forward_split_audit.py` is the next gate. It emits per-fold
train/validation/test counts and explicit purge/embargo/label-availability
blockers using market/condition clusters. The checked-in
[`split_audit_synthetic_blocked.json`](split_audit_synthetic_blocked.json)
contains no OOS metrics and cannot qualify as evidence.

The pre-registration gate is recorded in
[`preregistration_synthetic_blocked.json`](preregistration_synthetic_blocked.json).
It verifies the strategy ID, causal cutoffs, parameter grid, selection rule,
data/code identities and evaluation dates. Post-evaluation reports must also
provide CSCV/PBO multiple-testing diagnostics; missing diagnostics or a
parameter/test-set change is a machine-readable blocker.

The execution-realism section expands the registered matrix into stable cell
IDs. It requires ask/bid depth and rejects midpoint execution; every cell's
gross/fee/net PnL, fill rate, Brier and ECE fields remain `null` in the checked
in synthetic report.

The one bounded public-feed attempt is recorded in
[`public_acquisition_dry_run_blocked_20261005.json`](public_acquisition_dry_run_blocked_20261005.json).
DNS was unavailable, so it produced no canonical v3 observations and no
independent resolution labels. The corresponding minimum handoff package is
[`public_v3_data_handoff_request.json`](../../research/strategy/runs/public_v3_data_handoff_request.json).
This artifact records hashes of temporary failed-attempt archives only; it does
not make public quotes, quote markouts, or replay metrics eligible for canary
promotion.

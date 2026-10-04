# Walk-forward execution report

`blocked_report.json` is the current public-safe checkpoint for the
event-clustered evaluator. It records zero input observations and zero labels,
so Brier, ECE, and cost-adjusted PnL are intentionally `null`. The evaluator
cannot manufacture an out-of-sample result from the legacy CSV archive because
those rows lack the v3 event/receive, fee, rule, and provenance contract.

Provide a previously unseen canonical v3 JSONL block and a separate, hashed
resolution-label JSONL file before generating a new report. Public replay
metrics will still not satisfy the existing private-fill canary gate.

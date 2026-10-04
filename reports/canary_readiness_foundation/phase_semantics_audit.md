# Phase semantics audit

**Hypothesis:** Requiring ten completed canary round-trips before the first
canary is circular. The ten-round-trip requirement should measure completion
after a canary run while the registered statistical and data-quality gates stay
unchanged before review.

**Method:** Run `analysis/canary_evidence_ledger.py` on the checked-in empty
ledger and on offline fixtures. Compare `pre_canary_research` with
`post_canary_completion`; separately evaluate ten fully reconciled synthetic
receipts and one private receipt. No network, account, wallet, or order access
is used.

**Result:** The pre-canary report does not use
`ten_canary_roundtrips` as a blocker, while the post-canary report does. Both
phases retain the 300-window, 7-UTC-date, 100-evidence, cost-adjusted,
stress, and 99% exit-reconciliation gates and keep `promotion_allowed: false`.
Synthetic receipts remain at zero real fills even when their fields are fully
reconciled. The current public snapshot is blocked with zero private receipts.

Reproduce with:

```bash
python -m unittest tests.test_canary_evidence_ledger -v
python analysis/canary_evidence_ledger.py \
  --input evidence/ledger/current.json \
  --output reports/canary_readiness_foundation/evidence_ledger.json \
  --phase pre_canary_research
```

# Archived quote cash-domain evidence

`analysis/archived_quote_cash_domain_export.py` enriches an immutable archived
decision cohort with selected arrival and exit quote evidence. It reuses the
existing prefix admission, fixed market ownership and first valid price tick
selection. It never trains a model, predicts a policy, replays a cash ledger,
collects new quotes or submits an order.

Every admitted row remains in the output, including unknown future quotes,
conflicts, invalid selected snapshots and zero size. Sizes and future quality
cannot change tick selection or remove test rows. Selected raw numeric snapshots
allow a separate reader to check the full book-validity flags. Missing numeric
values remain unknown.

For complete valid quote pairs, the exporter records algebraic per-share costs
and the size interval compatible with the registered minimums and depth
assumption. These are observed quote-domain quantities. A nonempty interval is
neither a decision-time signal nor proof of an authenticated fill. The registered
cash release time remains an assumption rather than a spendable account receipt.

The private configuration freezes the archive, original protocol, source and
code hashes, original feature bytes, coverage and complete cohort identities.
`archived-quote-cash-domain-export.yml` accepts only the digest of bounded XZ
configuration supplied through its dedicated repository secret. Results and
failure details stay in runner-private files, are encrypted before artifact
upload and journal publication, and are removed during unconditional cleanup.
Successful evidence export always reports `strategy_evaluated=false`.

# Cached quality and capacity comparison

The manual `offline-quality-capacity-audit` workflow consumes previously
audited saved tables and a separate immutable projection of stored control
predictions. It does not acquire a new market archive or refit the control.
Both inputs use ciphertext-specific capabilities; the journal identity and
its recovery material never enter the runner.

`analysis/offline_quality_capacity_audit.py` first binds the saved bytes,
cohort identities, selected clocks, full-book label quality and control
predictions to the private registered configuration. It reconstructs every
control cost candidate and complete cash-ledger fingerprint before fitting.
`analysis/cached_quote_execution.py` preserves the recorded selector result,
including unavailable reasons; it does not invent missing raw ticks.

The registered comparison has two sequential interventions:

- A changes only past training-label admission for the fixed signed-residual
  price model. Future quality never filters a current test opportunity.
- B reuses A's forecasts and adds a decision-frozen quantity cap from a
  fixed lower-quantile exit-capacity model. It can shrink a position or
  abstain below the original minimum; it cannot use the future exit book to
  choose the position.

Both learners fit only earlier runs with labels available strictly before
the current run's first decision. Their train-only scalers and full numeric
tree structures are saved. Runs with insufficient labels retain all test
identities with null forecasts; predictions are never backfilled. Price
metrics use an explicitly reported common finite-prediction intersection.
Known zero capacity remains a label, while unknown future data remains
unknown. A capacity quantile is neither a fill probability nor a calibrated
guarantee for a sequence of trades.

The original fees, delays, cash-release assumption, minimum order, depth
haircut, strict exit-depth comparison and cash-stop rules remain fixed. A
capacity cap does not add partial exits or search later quotes for a better
fill. Each scope has one evolving cash ledger; independent diagnostic scopes
cannot be joined into a profitable sequence. These simulations are not
authenticated fills or observations of reusable account cash.

The workflow transports a separately frozen independent reader through a
private compressed input, bound by its exact source digest in the registered
configuration. Its `script_utf8` text is encoded to UTF-8 before checking the
262,144-byte source limit and digest; the registration has a separate canonical
JSON digest. The compressed secret is limited to 45,000 base64 characters and
its expanded bundle to 524,288 bytes. That standard-library reader runs in the same cloud job and
reconstructs training membership, scalers, raw/bin tree predictions and all
cash paths. Its ledger reconstructions are verification of the same trials,
not additional strategy experiments. The detailed report is sealed as
`diagnostics.txt`; a successful model command alone cannot claim verified
strategy evaluation when the independent audit fails.

Capabilities are removed before models or the reader run. Restored tables,
reader source and registration remain in a separate private bootstrap and
are deleted before sealing. Public artifacts contain only recipient-encrypted
evidence. Verification includes saved-data integrity and recorded simulation
arithmetic; it does not independently establish raw-feed ordering, cloud
execution traces, real fills, settlement or spendable cash.

Result delivery explicitly permits individual derived files up to 256 MiB,
with total source bytes capped at 512 MiB and the compressed bundle at 128 MiB.
Ciphertext is staged and published only after every expected part is complete.
If a known size limit prevents full delivery, an isolated compact package keeps
the run metadata, frozen input, available summary and independent diagnostics,
plus a manifest of original file sizes and hashes. It excludes large tables and
models, marks strategy and comparison validity false, and records that complete
evidence was not retained. Artifact retention and encrypted journal persistence
still run for this compact evidence; the final workflow gate fails until full
evidence has been retained. Original plaintext stays available through sealing
and fallback and is then removed by unconditional cleanup.

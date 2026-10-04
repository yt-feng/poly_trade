# Canary evidence ledger

`current.json` is the public-safe snapshot of the evidence ledger. It contains
no raw private order, fill, account, wallet, or credential data. A future
operator may add public-safe metadata or a digest/reference to a private receipt,
but the receipt itself must remain outside this public repository.

The validator accepts `private_execution_receipt` records only when entry and
exit identifiers, UTC timestamps, cost-adjusted lower-bound PnL, stress bounds,
and order/fill/cancel/fee/settlement/account reconciliation are explicit. The
provenances `public_quote`, `paper_simulation`, and `synthetic_receipt` are
retained as diagnostics and are always excluded from real-fill gates.

Run the current snapshot locally:

```bash
python analysis/canary_evidence_ledger.py \
  --input evidence/ledger/current.json \
  --output reports/canary_readiness_foundation/evidence_ledger.json
```

For private work, set `ARCHIVE_KEY` only in the process environment and use the
repository's authenticated archive utility. Never commit the passphrase, raw
receipts, decrypted files, wallet keys, or `.env` files.

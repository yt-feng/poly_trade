# Public acquisition dry-run (2026-10-05)

The repository recorded one bounded public-data attempt for the BTC 5-minute
scope. It used `poly/capture_v3.py` for eight seconds with no credentials, no
wallet, no order client, and no fallback or retry:

```bash
python capture_v3.py --assets btc --seconds 8 --output <temporary-output> \
  --require-core --require-microstructure
```

The adapter source was pinned to poly commit
`89f7b149cced7506dae94f2e2b0902933d273fc8`. DNS resolution failed for the
Polymarket, Binance, and related public hosts with `Temporary failure in name
resolution`. The run produced eight legacy snapshot rows and eight
`quote_markout_not_fill_pnl` rows, but zero canonical v3 observations, zero
market/condition/token identities, zero executable books, zero contemporaneous
fee/tick/minimum-order records, and zero independent resolution labels. The
temporary archives were not committed; their SHA-256 values are retained in
the machine report for auditability.

The machine-readable result is
[`public_acquisition_dry_run_blocked_20261005.json`](../reports/walk_forward_execution/public_acquisition_dry_run_blocked_20261005.json).
It keeps all walk-forward and canary metrics `null`, marks the attempt blocked,
and records each network and data-contract blocker. Public quote replay and
quote markouts cannot satisfy the private order, fill, cancel, fee, settlement,
and account reconciliation gates.

The smallest package needed to continue is the checked-in
[`public_v3_data_handoff_request.json`](../research/strategy/runs/public_v3_data_handoff_request.json).
It requests canonical v3 JSONL observations with source/receive timestamps,
market/condition/token IDs, full bid/ask depth, fee/tick/minimum-order metadata,
immutable source bytes and hashes, plus a separate official resolution-label
JSONL file. Legacy CSV, midpoint-only quotes, inferred outcomes, and quote
markouts are explicitly rejected. After a package is supplied, run intake,
split, pre-registration, and execution checks with its exact SHA-256 values;
the ten private reconciled canary fills remain a separate promotion gate.

To regenerate the report without network access:

```bash
python analysis/public_acquisition_dry_run.py \
  --output reports/walk_forward_execution/public_acquisition_dry_run_blocked_20261005.json
```

This command only writes the deterministic audit artifact. It does not contact
any endpoint and does not create credentials or orders.

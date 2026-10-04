# Archive baseline audit

`analysis/archive_baseline_audit.py` inventories existing `poly` and
`poly_trade` CSV archives and runs a fixed, quote-only sensitivity replay. It
never imports a trading client, network library, wallet, or credential.

The registered protocol is kept outside the checkout and sealed with the result.
The public manifest is only a pointer and checksum:
`research/strategy/runs/archive_baseline_20261004_manifest.json`.
The encrypted artifact is
`research/strategy/private/archive_baseline_20261004.compact.json.enc`.
Only the authenticated ciphertext is public. Do not decrypt it in CI or print
its contents.

The fixed diagnostic compares `no_trade`, `fixed_up`, `fixed_down`, and
`mid_momentum` over positive fee/slippage sensitivities and 5/10-share
scenarios with $10 starting cash. Entry and exit use delayed archived quotes;
missing or unknown exits are charged the full entry cost and halt that scenario.
No `final_price`, `target_price`, or settlement outcome is used in a signal or
payoff. The replay is therefore a hypothetical quote sensitivity study, not
execution evidence or a profitability claim.

## Data limitations found

The archived monthly raw CSVs cover 149 UTC row dates and 41,751 distinct
five-minute windows (5,839,327 rows). The selected seven-day diagnostic slice
contains 1,968 windows after timestamp conflict handling and 553,109 raw rows
before deduplication. Sampling is irregular, mostly 1–3 seconds, with longer
gaps. The raw schema has no per-record receive timestamp, source event time,
fee rate, minimum order size, tick size, condition ID, or private receipt ID.
The first quote is a sequentially fetched snapshot, so exact side-by-side
availability is unknown. Existing reports and strategy files already reference
many of the same source runs, so this cannot be called pristine out-of-sample.

The exact empirical values are in the encrypted artifact. The next honest
experiment is a pre-registered forward block from previously unseen v3 capture
with source/receive timestamps, contemporaneous fee and market-rule metadata,
explicit minimum-size checks, and a fixed strategy selected before that block.

To run a future baseline, provide canonical JSONL explicitly:

```bash
python analysis/archive_baseline_audit.py \
  --poly-root ../poly --trade-root . \
  --protocol /private/protocol.json \
  --v3-input /private/observations-v3.jsonl \
  --output /private/result.json
```

The runner passes only records accepted by `analysis/v3_data_contract.py` into
the quote adapter. Legacy CSV rows are quarantined instead of silently replayed.

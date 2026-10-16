# Public evidence collection with sealed output

This document contains generic tooling instructions only. No selected wallet cohort, empirical findings, trading parameters or personal key material is included.

## Publication control
Four legacy empirical publishers remain retired from public execution: canary-forward, polymarket-eda, import-run-24869603988-and-analyze, and run-24869603988-analysis. They emit only a constant publication-hold notice. The canary-research entry point now delegates to the recipient-verified sealed quality-first pipeline described in `continuous_research.md`; it does not publish plaintext results. Existing analysis source and historical records are unchanged. The six-hour canary-forward-audit PUBLIC market-data acquisition and the separate assistant research task are not removed. Contract CI success indicates synthetic checks only, never a profitable strategy.

A trusted recipient has to be initialized locally before real sealed evidence is produced. Missing keys do not trigger a plaintext fallback. Old plaintext commits, comments, artifacts and external copies remain public; this change does not erase history. Other repositories, local installations, historical workflow reruns and third-party exporters are not certified by these checks. Rerunning an old revision can still use its old publisher: do not rerun historical empirical workflows. Do not upload restricted code in encrypted form to circumvent any security decision.

## One-time identity setup
From the repository root, use the existing local-only command:

```bash
python3 -m pip install -r requirements-vault.txt
python3 tools/research_vault.py init --identity-dir "$HOME/.config/poly-research-vault"
```

Enter the chosen PIN interactively; do not pass it in command arguments, environment variables, files in the repository, or CI. Preserve both private files outside Git. Only `research_vault/recipient.json` is publishable. Independently record/check the displayed recipient fingerprint before use. The tool does not run personal initialization in CI and does not overwrite an existing identity. Public-key encryption does not authenticate the author of a record.

## Collect evidence
Create a private JSON request file outside the repository. Its schema is `{ "endpoint": ..., "filters": {...}, "max_pages": ... }`. Endpoints are restricted to `/v2/trades` and `/v2/activity`; no credentials or order methods are supported. Use the current official v2 documentation to fill filters; do not place a confidential address list in public workflow inputs. The collector resends all filters on every cursor page and seals raw UTF-8 responses, hashes, HTTP status, local start/receive timestamps, and completion reason together.

```bash
python3 tools/sealed_public_feed.py \
  --request "$HOME/.config/poly-research-vault/request.json" \
  --recipient research_vault/recipient.json \
  --recipient-id '<independently verified public fingerprint>' \
  --output "$HOME/.config/poly-research-vault/evidence.vault"
```

Inspect and decrypt locally with the existing research_vault commands. Review encrypted files before publishing them to a neutral path. No real plaintext responses are printed or uploaded by the collector. The legacy collector has fixed byte/page budgets, timeout, redirect refusal and its own transport behavior. The dedicated cloud probe described below supplies an ordinary environment-aware HTTP client; it does not alter local networking. Access or rate errors stop the walk; there is no fallback to other hosts or API versions. A written envelope can contain a partial/failed fetch, so writing ciphertext does not certify successful or complete acquisition. `CURSOR_EXHAUSTED_FOR_REQUEST` means only the served walk ended, not all history exists or was returned.

## Measurement contracts
Official reference: https://data-api.polymarket.com/v2/docs

* v2 uses `{data,pagination}` with cursor paging, not v1 arrays/offset. Feed cursors do not bind all filters; resend the same values to avoid silent cohort changes. Empty or short pages can still have a next cursor; follow that cursor within the fixed budget and reject cursor loops.
* Non-user `/v2/trades` shapes ignore explicit start/end. This collector rejects such filters rather than falsely claiming a bounded historical cohort. Collect raw records, then use their explicit timestamps locally with the correct availability rules.
* Bare size/volume is in shares. A notional calculation requires price and is not PnL. Unavailable values remain unknown, not zero.
* `user-stats.trades` counts distinct markets. The separate canonical fill count is not interchangeable. Neither is the number of completed funding cycles.
* Raw wallet-side labels are not automatically aggressor directions. The existing public_observation_contract still needs explicit event identity and role/availability evidence; this collector does not invent those fields or identify real people.
* Public holdings are not complete account equity, realized PnL is not ROI, and redeemable assets are not free cash. Public records alone cannot establish private order acknowledgments, queue position, cancellations, or actual cash reconciliation.

## Bounded cloud probe
The manual `sealed-feed-probe` workflow uses the existing `QUALITY_FIRST_RECIPIENT_JSON_B64` and `QUALITY_FIRST_RECIPIENT_ID` repository variables. It requires no wallet credentials or query secrets. The fixed sequence is status, at most two global taker-trade pages of size 100, then status. The entry point requires a GitHub Actions environment; local verification uses mocked transport only.

Each HTTP attempt records bounded response bytes, digest, HTTP status, UTC request/response times and monotonic elapsed time inside ciphertext. Transport failures have a finish time without inventing a response receipt. The trade walk remains an inner encrypted envelope in the outer delivery bundle. The outer generic status describes sealing and delivery only, with `strategy_evaluated=false`. Completed main-branch manual probes use the same durable `sealed-research-journal` publication and remote verification path as research runs.

A successful HTTP response can carry stale data. Inspect `/v2/status` snapshot age separately from serving lag and ingestion progress; missing fields remain unknown. The global prefix does not establish complete history, a frozen wallet cohort, first public visibility, account activity availability or any profitable strategy. References: [official freshness endpoint](https://docs.polymarket.com/api-reference/service/get-data-freshness), [official pagination contract](https://docs.polymarket.com/api-reference/data-api/overview).

The default manual input `probe_mode=availability` preserves that sequence. The optional `probe_mode=market-constraints` (CLI `--mode market-constraints`) adds at most four GET requests before the final status request: the selected market's Gamma record, compact CLOB configuration, and two outcome order books. The complete run remains capped at eight requests, each with a one MiB response-body limit and an eight-second deadline. There are no wallet, URL, market-identifier or order inputs. Routes are restricted to the documented Data API, Gamma and CLOB hosts; a missing current market does not trigger historical, endpoint or pagination fallback.

This mode selects only a unique, currently running BTC five-minute identity already present in the bounded global trade prefix. Selection uses the documented v2 `condition_id`, `slug`, `token_id` and `outcome_index`, without ranking price or returns. The complete received prefix must precede selection; market requests must follow selection, and all request/receipt/finish clocks must preserve sequential order through the final status response. Unverified clocks retain raw evidence without starting a market lookup or certifying a current observation. Gamma's explicit string `version` determines token mapping: `v1` uses the JSON string `clobTokenIds`, while `v2` uses the `positionIds` array. Missing or unsupported versions stop further market requests. Gamma identity, the compact CLOB token/outcome list, and each book's condition/token identity are cross-checked. A run crossing the selected market's end retains historical evidence without substituting the next market.

Private `results/market-constraints.json` preserves separate field values, units, JSON paths and HTTP request references. The order-book `min_order_size` has a documented shares unit. Gamma's `orderMinSize` separately retains the official description as USDC notional, with its runtime unit unresolved; it is not silently converted into shares or accepted as a verified dollar floor. CLOB `mos` remains a separate field with no assumed unit. No universal dollar minimum, quantity step or exchange timestamp unit is invented. `minimum_notional_verified=false` and `order_acceptance_observed=false` remain explicit. Gamma and CLOB fee rate/exponent/taker-only fields are compared without replacing either source; tick-size differences between separately received responses are observations, not proof of a service defect. This mode does not change frozen strategy or data-processing code.

Source contracts: [Gamma market versions and trading constraints](https://docs.polymarket.com/market-data/market-details), [compact CLOB market information](https://docs.polymarket.com/api-reference/markets/get-clob-market-info), [order books](https://docs.polymarket.com/api-reference/market-data/get-order-book), and [order-size units](https://docs.polymarket.com/trading/place-orders).

## Local/CI scope
Run `python3 -m unittest discover -s tests -p 'test_sealed_public_feed.py' -v` and the corresponding `test_public_feed_probe.py` and `test_public_market_constraints.py` tests. All shipped fixtures and pull-request contract checks are synthetic and do not call live feeds or initialize personal keys. Only an explicit manual cloud probe performs bounded public GET requests. The tooling is evidence collection, not a trading model or backtest. Completed recipient setup is required before producing encrypted journals.

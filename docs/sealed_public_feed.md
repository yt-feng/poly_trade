# Public evidence collection with sealed output

This document contains generic tooling instructions only. No selected wallet cohort, empirical findings, trading parameters or personal key material is included.

## Publication control
Five legacy empirical publishers have been retired from public execution: canary-research, canary-forward, polymarket-eda, import-run-24869603988-and-analyze, and run-24869603988-analysis. They now emit only a constant publication-hold notice; they do not execute analysis, upload plaintext artifacts, append result summaries, or commit results. Existing analysis source and historical records are unchanged. The six-hour canary-forward-audit PUBLIC market-data acquisition and the separate assistant research task are not removed. Contract CI success indicates synthetic checks only, never a profitable strategy.

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

Inspect and decrypt locally with the existing research_vault commands. Review encrypted files before publishing them to a neutral path. No real plaintext responses are printed or uploaded by the collector. Fixed byte/page budgets, timeout, redirect refusal and disabled environment proxies are deliberate. Access or rate errors stop the walk; there is no fallback to other hosts or API versions. A written envelope can contain a partial/failed fetch, so writing ciphertext does not certify successful or complete acquisition. `CURSOR_EXHAUSTED_FOR_REQUEST` means only the served walk ended, not all history exists or was returned.

## Measurement contracts
Official reference: https://data-api.polymarket.com/v2/docs

* v2 uses `{data,pagination}` with cursor paging, not v1 arrays/offset. Feed cursors do not bind all filters; resend the same values to avoid silent cohort changes.
* Non-user `/v2/trades` shapes ignore explicit start/end. This collector rejects such filters rather than falsely claiming a bounded historical cohort. Collect raw records, then use their explicit timestamps locally with the correct availability rules.
* Bare size/volume is in shares. A notional calculation requires price and is not PnL. Unavailable values remain unknown, not zero.
* `user-stats.trades` counts distinct markets. The separate canonical fill count is not interchangeable. Neither is the number of completed funding cycles.
* Raw wallet-side labels are not automatically aggressor directions. The existing public_observation_contract still needs explicit event identity and role/availability evidence; this collector does not invent those fields or identify real people.
* Public holdings are not complete account equity, realized PnL is not ROI, and redeemable assets are not free cash. Public records alone cannot establish private order acknowledgments, queue position, cancellations, or actual cash reconciliation.

## Local/CI scope
Run `python3 -m unittest discover -s tests -p 'test_sealed_public_feed.py' -v`. All shipped data are synthetic fixtures. CI does not call live feeds or initialize personal keys. The current addition is a data collector and measurement contract, not a trading model or backtest. Completed local setup is still required before producing user-specific encrypted journals.

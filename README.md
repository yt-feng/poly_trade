# poly_trade

Research and execution-planning tools for BTC Up/Down five-minute Polymarket markets.

## Current research path

Run **canary-readiness-research** in GitHub Actions. It uses the repository's
verified public-recipient variables, or an explicitly supplied X25519 public
recipient JSON (base64) and independently checked fingerprint. It calls the
**quality-first-research** workflow. The default research cash account is USD 10.
No account credentials or order API are used by this workflow.

The pipeline:

- Samples complete recent capture archives across UTC dates from `yt-feng/poly`, verifies their checksums, and reuses only freshly reverified public cache bytes.
- Keeps continuous high-quality segments for fitting, without filling historical gaps. Model features, data integrity and execution conditions have separate checks.
- Splits whole markets into chronological training, validation and holdout partitions. Evaluation scope excludes partial markets at the preselected archive boundaries; failures inside that scope remain in the ledger.
- Tests a small fixed comparison set against costs, latency, minimum order constraints and a cash ledger. A failed validation produces a frozen no-trade model.
- Encrypts models, results, source manifests and diagnostics before artifact delivery. Public logs contain only operational status. Quote proxies are never counted as actual fills.

A green **canary-forward-audit** run establishes a data-acquisition result, not a
profitable strategy or ten executed canary fills. Read the sealed research
result to distinguish model fitting, validation and the final decision.

See [workflow and evaluation contract](docs/quality_first_research.md) and
[primary research sources](docs/quality_first_research_sources.md).

## Core files

- `analysis/quality_first_data.py`: checksum verification, compact input projection, causal quality layers, training segments and evaluation sampling scope.
- `analysis/quality_first_research.py`: bounded fitting, chronological selection, cash-constrained quote replay and frozen-model evaluation.
- `tools/run_quality_first_research.py`: cloud acquisition, bounded execution and sealed delivery.
- `tools/research_vault.py`: recipient verification and encrypted envelopes.
- `research_vault/POLICY.md`: public/private research publication contract.

Run the targeted synthetic contracts with:

```sh
python -m unittest discover -s tests -p 'test_quality_first*.py' -v
```

Use Actions for substantial acquisition and backtesting. The frozen-model CLI
supports a separately supplied private model file; it does not automatically
persist a confidential model or aggregate forward evidence across scheduled runs.

## Historical research and execution tools

The first-two-minute models based on run `24869603988`, older parameter searches,
reports and execution adapters remain historical material. Their published
results are not fresh out-of-sample evidence for the current market. Retired
plaintext-publishing workflows do not run the new evaluation path.

The older `canary_core.py` / `forward_roll_conflict.py` registries keep their
existing fixed definitions. Local `live_paper_trader.py` and `live_execution.py`
are separate tools; this research workflow does not activate them. Their setup
and account prerequisites are documented in `docs/live_trading_setup.md`.

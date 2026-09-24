# BTC 5m forward counterevidence checkpoint — 2026-09-24

## Scope

This checkpoint preserves negative evidence from the already-registered legacy `roll_veto` shortlist and the current public-data health state. It does **not** register a new alpha version, does not reuse these windows as a future holdout for any new mechanism, and does not change live execution. All fills below are public-quote replay/proxy observations, not private Polymarket fills, positions, or cash receipts.

The new multi-mechanism work whose standing test boundary is 2026-09-24 00:00 UTC remains separate. This file must not be used to relabel the legacy `roll_veto` continuation as confirmation of that newer research protocol.

## Acquisition repair and coverage

Read-only acquisition was repaired on main so raw event archives use a separate byte budget and cannot starve snapshot/label research; release assets are explicitly paginated and checksum/integrity errors remain fail-closed. No strategy or order path was changed.

`canary-readiness-research` run `36014467189` then completed successfully on the PR merge ref with 58 regression tests passing. Artifact `10813932497`, ZIP SHA256 `ab16eb67636223d7405852ce12f7cf2a89ef7b12808526c579c68ad46029996e`, contains:

- 126 acquired files, 189,947,864 research bytes plus 16,655,919 raw-evidence bytes, 0 acquisition errors, 0 budget skips in this readiness acquisition.
- 51,307 production-v3 snapshot rows, 174 distinct 5-minute windows, 2 UTC dates.
- UTC coverage `2026-09-23 22:05:08.175` through `2026-09-24 14:27:58.766`.
- 43 official resolved-label matches and 35,783 decision-ready rows.
- `LIVE_CANARY_ELIGIBLE=false`.

This is bounded recent coverage, not full history or proof of gap-free tape completeness.

## Frozen legacy shortlist: continued forward result

The repository's existing lock remains:

- candidate: `roll_veto`
- mode: taker replay
- hold: 30 seconds
- nominal latency: 1 second, with 3-second stress configuration
- original lock excludes all slugs through `btc-updown-5m-1790141700`

Across the newly acquired 174 forward windows, the fixed 1-second configuration produced:

| Metric | Result |
|---|---:|
| Attempts | 174 |
| Entry proxy fills | 139 |
| Valued exits | 138 |
| Unknown exits | 1 |
| Entry limit rejects | 35 |
| Mean net, cents/share | **-3.6420** |
| One-tick stress mean, cents/share | **-4.6562** |
| Bootstrap 2.5th percentile of mean, cents/share | **-5.8750** |
| Median net, cents/share | -3.1794 |
| Positive valued observations | 60 / 138 |
| Valued replay PnL | -$25.12979 |
| Conservative lower-bound ledger | **-$28.945415** |

The 3-second configuration was also negative: 120 entry proxies, 118 valued, 2 unknown, mean net `-3.8156` cents/share, one-tick stress `-4.8274`, bootstrap lower percentile `-6.4439`, conservative ledger `-$28.531705`.

These results materially strengthen the case **against** promoting the old `roll_veto` shortlist. They do not establish that every other mechanism lacks alpha.

## Post-2026-09-24 00:00 UTC descriptive slice

For comparability with the standing 2026-09-24 test boundary, the same unchanged `roll_veto / taker / 30s / 1s` replay was filtered to windows starting at or after `2026-09-24 00:00:00 UTC`. This slice was not used to retune the strategy.

- 151 attempts.
- 118 entry proxy fills; 117 valued, 1 unknown; 33 entry limit rejects.
- Mean net `-4.1519` cents/share.
- One-tick stress mean `-5.1652` cents/share.
- Median `-3.4944` cents/share.
- 50 / 117 valued observations positive.
- Removing the single best valued observation: mean `-4.3840` cents/share.
- Descriptive 2.5th-percentile bootstrap of the window mean: `-6.4918` cents/share. Same-day/time dependence is not modeled by this bootstrap.
- Valued replay PnL `-$24.288345`; conservative lower-bound ledger `-$28.103970`.

This is strong counterevidence for that **legacy fixed candidate**, not a private-account PnL statement.

## No winner rotation

Among the 40 legacy forward configurations, the only positive raw mean was `roll_conflict / maker_trade_through_proxy / 30s / 3s`: only 8 proxy fills, mean `+1.8927` cents/share and stress mean `+0.8717`. It has no >=30-observation interval, is a crossed-quote passive-fill proxy rather than real FIFO execution, and removing its single best observation changes the mean to `-1.0639` cents/share. It is therefore **not** promoted or substituted for the failed registered shortlist.

## Latest raw-event health sample

`canary-forward-audit` run `36014466991` also passed all read-only tests and sampled the newest two checksum-verified closed raw segments from `capture-v2-35996038217-1`:

- `raw-2026-09-24-000176.jsonl.gz`: 113,870 rows, 40,722 `polymarket_ws`, 94 `polymarket_rest_book`, ~47.30s received interval.
- `raw-2026-09-24-000177.jsonl.gz`: 111,520 rows, 44,001 `polymarket_ws`, 78 `polymarket_rest_book`, ~39.46s received interval.

This means the earlier acute symptom of 0/1 Polymarket WS records across ~293 seconds while REST books changed is not present in these latest sampled segments. The historical root cause remains open; these two segments do not certify 12-hour continuity, queue completeness, or an event-complete tape.

## Fee-asset correction kept separate from alpha

Current Polymarket documentation states that taker fees are applied at match time, the crypto fee rate is `0.07`, fees are calculated in USDC, and fee precision is five decimals. The active Polymarket V2 Python SDK also adjusts BUY size when a supplied USDC balance would otherwise leave insufficient balance for fees.

Main now defaults the plan-only canary sizing helper to `cash_usdc_official`; the older share-deduction convention remains only as the explicitly named `shares_sensitivity`. At `price=0.50`, `tick=0.01`, `min_order_size=5`, `fee_rate=0.07`, the official-default plan is 5.00 matched shares plus a planned USDC taker fee of $0.08750, all-in $2.58750. No private receipt is inferred from this arithmetic.

This engineering correction does not repair or improve the negative alpha result above.

## Research interpretation

The current evidence says:

1. The legacy `roll_veto` shortlist should remain rejected for funding.
2. A tiny positive maker-proxy cell is not a replacement candidate because it fails robustness and execution-identification requirements.
3. Data acquisition coverage materially improved, while full-tape completeness remains uncertified.
4. Public order-book/trade direction should not be treated as authoritative aggressor direction; queue/FIFO fills cannot be identified from aggregate L2 alone; latency regimes must be evaluated separately; probability calibration is conditional and noisy; Kelly sizing remains a separate long-run risk problem.
5. No new alpha version is registered in this checkpoint. Any new mechanism must be registered first and scored only on future windows.

## Sources

- `https://github.com/yt-feng/poly_trade/actions/runs/36014467189`
- `https://github.com/yt-feng/poly_trade/actions/runs/36014466991`
- `https://github.com/yt-feng/poly/issues/3`
- `https://docs.polymarket.com/trading/fees`
- `https://docs.polymarket.com/trading/place-orders`
- `https://docs.polymarket.com/trading/positions/manage`
- `https://arxiv.org/abs/2604.24366`
- `https://arxiv.org/abs/2602.19520`
- `https://arxiv.org/abs/2609.13597`
- `https://arxiv.org/abs/2504.00846`
- `https://arxiv.org/abs/1603.06183`

## Later same-day continuation: 192-window checkpoint

A later unchanged shadow refresh, GitHub Actions run `36025038821`, incorporated the completed four-hour production capture `35996038217`. This is a continuation of the **same legacy registered family**, not a new alpha registration and not evidence for the separate 2026-09-24 multi-mechanism protocol.

Coverage at this checkpoint:

- 56,706 production-v3 rows.
- 192 distinct five-minute windows across 2 UTC dates.
- 51 matched official resolved labels, no label conflicts.
- 39,504 decision-ready rows.
- 0 duplicate or conflicting snapshot timestamps.
- `LIVE_CANARY_ELIGIBLE=false`.

The frozen `roll_veto / taker / 30s` results continued to deteriorate rather than recover:

| Metric | 1s | 3s |
|---|---:|---:|
| Attempts | 192 | 192 |
| Entry proxy fills | 148 | 130 |
| Valued exits | 147 | 128 |
| Unknown exits | 1 | 2 |
| Entry limit rejects | 44 | 58 |
| Entry capacity rejects | 0 | 4 |
| Mean net, cents/share | **-3.8394** | **-4.0064** |
| One-tick stress, cents/share | **-4.8534** | **-5.0181** |
| Bootstrap lower percentile, cents/share | **-6.1019** | **-6.5938** |
| Mean after removing best observation, cents/share | **-4.0661** | **-4.2440** |
| Valued replay PnL | -$28.21957 | -$25.64072 |
| Conservative lower-bound ledger | **-$32.03520** | **-$31.66050** |

This checkpoint is retained as additional counterevidence. It is not a reason to refit the threshold, swap in a different cell from the same 40-configuration family, or reinterpret public quote proxies as private fills. New alpha work still requires a new pre-registration and future windows.


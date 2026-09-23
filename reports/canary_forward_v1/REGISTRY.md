# Canary Forward v1 registry

**Research/shadow only. No live orders or automatic activation.**

The single forward primary is `roll_conflict / taker / 30s`, with 1s nominal entry/exit latency and a separately reported 3s latency stress. The whole-window forward boundary is **2026-09-23 08:30:00 UTC** (`1790152200000` ms). Windows starting before the boundary can warm causal histories but cannot contribute a return to forward v1.

This candidate was selected from the earlier exploratory study and is now frozen. v1 does not permit substituting another candidate, changing its 0.10 USD/s roll threshold, 1bp adverse five-second spot condition, 30s holding period, direction, or taker execution model after observing forward returns. A new mechanism requires a new registry version and a new future boundary.

## Evidence gate for a recommendation to human review

A positive mean alone is insufficient. The fixed final evidence gate requires at least 300 forward windows over at least 7 UTC dates, at least 100 valued primary attempts, at least 99% valued exits among entered attempts, positive cost-adjusted primary mean, positive one-sided 95% lower bounds using both UTC-day clusters and fixed two-hour UTC blocks, positive 3s mean and day-cluster lower bound, a positive extra-exit-tick mean, and a positive ledger after worst-case treatment of missing exits. Fixed 25/50/100 valued-attempt and 50/100/300-window checkpoints are monitoring only and cannot trigger early acceptance.

There is exactly one candidate in the v1 confirmatory family. Other signals cannot replace it inside this version. This avoids turning repeated exploration into a confirmatory success.

## Execution and fee treatment

The replay inherits the causal prefix, fresh quote, quote-skew, delayed-new-quote, capacity, one-tick chase, path-gap and unknown-exit controls from the existing canary research. Unknown/stale market fee metadata rejects the attempt. Rebates are zero. Public fee reporting follows the current Polymarket documentation formula `fee = shares × feeRate × p × (1-p)` for taker matches and adds a five-decimal USDC fee-precision view. Current public documentation describes the fee as USDC-denominated, so the public replay keeps the purchased share count unchanged; actual fill fees, partial fills and resulting private position/net-share reconciliation remain mandatory before any real-money decision.

Reference: https://docs.polymarket.com/trading/fees

## What a future pass would mean

`human_review_evidence_ready=true` would mean only that the pre-registered public-data evidence gate has passed and the user should be notified for a separate manual decision. `live_canary_eligible` remains false in this research code. A real-money canary would still require explicit human approval plus private order/fill/cancel/partial-fill/fee/position reconciliation and a separate executor change outside this research module.

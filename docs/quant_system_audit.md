# Polymarket Quant System Audit

## Current Status

This repository is no longer only a dashboard. It contains a research-to-execution stack:

- Historical CLOB quote ingestion and feature building.
- Strategy research over BTC 5-minute Up/Down markets.
- Walk-forward validation, stress tests, stability buckets, calibration metrics, and trade logs.
- A selected robust system based on calibrated `q_model`, edge gates, liquidity filters, fractional Kelly sizing, and event caps.
- A live runner that discovers the current BTC 5-minute market, reads Polymarket order books, reads BTC reference price, generates signals, and writes state/dashboard files.
- A live execution adapter for Polymarket CLOB V2 with `paper`, `plan`, and `live` modes.

## Coverage Against A Complete Quant System

| Area | Status | Notes |
|---|---:|---|
| Market data | Partial | REST polling for Gamma/CLOB and BTC spot reference is working. CLOB WebSocket is still the next speed upgrade. |
| Signal model | Good | Current model blends market prior, BTC move, liquidity, interactions, and boundary factors. |
| Backtesting | Good | Includes train/validation/test, walk-forward, execution scans, stress tests, and paper replay. |
| Execution simulation | Good | Live paper runner simulates delayed fills, slippage gate, partial fills, and settlement. |
| Live order placement | Connected | `analysis/live_execution.py` uses official `polymarket-client` and only submits in explicit `live` mode. |
| Position/account sync | Partial | Live order responses are logged. Full user WebSocket/order/position reconciliation is still needed. |
| Multi-layer controls | Good baseline | Edge threshold, depth/spread/overround filters, event cap, Kelly shrink, duplicate-market block, live per-order/day caps. |
| Monitoring | Baseline | HTML dashboard plus CSV/JSON state. Alerts are not yet connected. |
| Ops | Baseline | Local MacBook workflow is documented. No daemon/launch agent yet. |

## What Changed For Live Trading

- Added `analysis/live_execution.py`.
- Added `--execution-mode paper|plan|live`.
- Added `--allow-live-trading`, `--max-live-order-usd`, `--max-live-orders-per-day`, and `--max-live-notional-usd-per-day`.
- Added live order event fields: token ID, order type, max price, order ID, accepted/rejected status, trade IDs, and executor latency.
- Added market `min_order_size` and `tick_size` capture.
- Added `requirements-live.txt`.
- Added `.gitignore` entries for local secrets and virtual environments.

## What I Need From You

For true live trading, you need to provide locally:

- Python 3.11+ environment.
- `POLYMARKET_PRIVATE_KEY`.
- `POLYMARKET_DEPOSIT_WALLET` or `POLYMARKET_WALLET`.
- Funded Polymarket account with enough USDC/pUSD and any platform approvals needed by the official SDK.

Chrome login is useful for visual confirmation of the account, balance, and current market, but the bot should use the SDK/API for execution.

## Next Upgrades

- CLOB WebSocket market-data adapter to replace REST polling.
- User/order WebSocket reconciliation for real fills, resting orders, and positions.
- Live PnL/settlement reconciliation against Polymarket account data.
- Alerting for live order accepted/rejected/blocked events.
- A lightweight local launcher so the system can run while your MacBook is open without manually keeping a terminal command around.

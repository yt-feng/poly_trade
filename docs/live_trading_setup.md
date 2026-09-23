# Polymarket Live Trading Setup

This repo now has three execution modes in `analysis/live_paper_trader.py`:

- `paper`: default mode. Reads live market data, emits signals, simulates delayed fills, and settles paper PnL.
- `plan`: reads live market data and writes the exact live order plan, but never signs or submits an order.
- `live`: signs and submits BUY market orders through the official Polymarket CLOB V2 SDK.

## Requirements

Live execution uses the official `polymarket-client` package, which requires Python 3.11+.

```bash
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements-live.txt
```

If your shell does not have `python3.11`, install a modern Python first. The macOS system `/usr/bin/python3` is often 3.9 and is not enough for the official SDK.

## Local Secrets

Copy the template and fill only your local file:

```bash
cp config/external_data.env.example .env.local
```

Required for `--execution-mode live`:

```bash
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_DEPOSIT_WALLET=0x...
```

`POLYMARKET_DEPOSIT_WALLET` is the Polymarket deposit wallet address. Chrome can be used to visually confirm the logged-in wallet/balance, but browser login cannot replace SDK signing credentials.

## Dry Live Order Plan

This is the first command to run when checking a real market. It writes `order_plan` events only.

```bash
python analysis/live_paper_trader.py \
  --env-file .env.local \
  --execution-mode plan \
  --once
```

The dashboard is written to:

```text
reports/live_paper_trading/index.html
```

## Live Execution

Live mode requires an explicit flag and has hard caps:

```bash
python analysis/live_paper_trader.py \
  --env-file .env.local \
  --execution-mode live \
  --allow-live-trading \
  --max-live-order-usd 1 \
  --max-live-orders-per-day 5 \
  --max-live-notional-usd-per-day 5
```

The live executor:

- Uses FAK market BUY orders by default.
- Sets `max_price = signal_price + paper_max_adverse_slippage`.
- Refuses an order when the expected shares are below the market `min_order_size`.
- Records accepted/rejected/blocked order responses in `paper_events.csv`.
- Keeps default single-order notional at `$1`.

## Useful Options

```bash
--market-url https://polymarket.com/zh/event/btc-updown-5m-1776752100
--sample-seconds 4
--duration-minutes 60
--paper-max-adverse-slippage 0.01
--live-order-type FAK
```

For the MacBook-local setup, `sample-seconds` around `2-4` is a reasonable middle-frequency starting point. The current runner still polls REST endpoints; a CLOB WebSocket market-data adapter is the next speed upgrade.

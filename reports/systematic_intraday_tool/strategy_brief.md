# Polymarket 5m systematic intraday strategy brief

- Data window: 2026-05-03T23:45:00+08:00 to 2026-06-02T23:45:00+08:00
- Events: 5989 usable 5m markets from 1460 CSV files
- Strategy variants: 609
- Starting bankroll: $100.00
- Fee model: $0.01 per share round-trip adjustment in this simulation

## Current winners

- Best research score: `session_breakout_london_fixed_4pct__tp06_sl03_buf3s`: $111.33, return 11.33%, trades 52, drawdown 1.46%, PF 2.254
- Highest ending bankroll: `portfolio_micro_ts_mix__settle`: $140.53, return 40.53%, trades 377, drawdown 59.83%, PF 1.049
- Highest intraday ending bankroll: `session_breakout_london_fixed_12pct__trail08_gap03_buf5s`: $130.39, return 30.39%, trades 50, drawdown 15.52%, PF 1.426
- Best intraday risk-adjusted score: `session_breakout_london_fixed_4pct__tp06_sl03_buf3s`: $111.33, return 11.33%, trades 52, drawdown 1.46%, PF 2.254

## Prior selected strategy rerun

- `classic_breakout_up__tp06_sl03_buf3s`: $91.98, return -8.02%, trades 98, drawdown 15.42%, PF 0.777; prior label `prior_validation_selected`
- `v1_active_fill_mix__tp06_sl03_buf3s`: $63.62, return -36.38%, trades 182, drawdown 40.17%, PF 0.550; prior label `prior_final_v1_mix;prior_mixed_candidate`
- `v1_adaptive_mix__tp06_sl03_buf3s`: $72.72, return -27.28%, trades 115, drawdown 30.92%, PF 0.617; prior label `prior_mixed_candidate`
- `v1_balanced_mix__tp06_sl03_buf3s`: $75.43, return -24.57%, trades 115, drawdown 28.54%, PF 0.657; prior label `prior_mixed_candidate`
- `v1_conservative_mix__tp08_sl04_buf5s`: $79.58, return -20.42%, trades 115, drawdown 26.38%, PF 0.720; prior label `prior_mixed_candidate`

## Interpretation

- The old mixed candidates are no longer competitive on the latest 30-day window under this execution model.
- Intraday sell exits with 3-5 second buffers reduce drawdown on many setups, but they do not automatically improve every old mixed strategy.
- Current best candidates are session-specific London breakout variants; the raw-return winner still has materially higher drawdown.

## Execution caveats

- This is still a replay backtest, not a live order-placement simulator.
- Entry and exit use top-of-book price and size checks from captured quotes.
- The 3-5 second buffers avoid end-of-window micro-timing, but failed orders, queue position, partial fills, and API latency are not fully modeled.

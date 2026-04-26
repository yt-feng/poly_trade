# First-2-minute predictive research for 24869603988_attempt1

## Research question

Given the **first 2 minutes** of BTC move and quote information, can we predict the final 5-minute Up/Down result or choose a better trading rule?

## Data summary

- Source files: **50**
- Clean quotes: **36399**
- Market feature rows: **302**
- Fee used in rule evaluation: **0.0100**
- Up rate on resolved markets: **0.4805**

## First-2-minute feature sample

| slug                     | window_text   |   btc_move_2m |   mid_up_prob_2m |   outcome_up |   realized_pnl_buy_up_from_2m |
|:-------------------------|:--------------|--------------:|-----------------:|-------------:|------------------------------:|
| btc-updown-5m-1776998700 | 10:45-10:50   |        nan    |            0.015 |          nan |                       nan     |
| btc-updown-5m-1776999000 | 10:50-10:55   |        -90.69 |            0.075 |            0 |                        -0.085 |
| btc-updown-5m-1776999300 | 10:55-11:00   |        -29.38 |            0.285 |            0 |                        -0.295 |
| btc-updown-5m-1776999600 | 11:00-11:05   |          6.4  |            0.595 |            1 |                         0.395 |
| btc-updown-5m-1776999900 | 11:05-11:10   |        nan    |            0.375 |          nan |                       nan     |
| btc-updown-5m-1777000200 | 11:10-11:15   |        nan    |            0.285 |          nan |                       nan     |
| btc-updown-5m-1777000500 | 11:15-11:20   |        -72.66 |            0.105 |            0 |                        -0.115 |
| btc-updown-5m-1777000800 | 11:20-11:25   |        -45.81 |            0.165 |            0 |                        -0.175 |
| btc-updown-5m-1777001100 | 11:25-11:30   |         77.5  |            0.905 |            1 |                         0.085 |
| btc-updown-5m-1777001400 | 11:30-11:35   |        -90.69 |            0.105 |            0 |                        -0.115 |
| btc-updown-5m-1777001700 | 11:35-11:40   |         73.97 |            0.815 |            1 |                         0.175 |
| btc-updown-5m-1777002000 | 11:40-11:45   |        -65.43 |            0.155 |            0 |                        -0.165 |
| btc-updown-5m-1777002300 | 11:45-11:50   |          7.58 |            0.495 |            1 |                         0.495 |
| btc-updown-5m-1777002600 | 11:50-11:55   |         49.56 |            0.855 |            1 |                         0.135 |
| btc-updown-5m-1777002900 | 11:55-12:00   |        nan    |            0.475 |          nan |                       nan     |
| btc-updown-5m-1777003200 | 12:00-12:05   |        -36.44 |            0.245 |            0 |                        -0.255 |
| btc-updown-5m-1777003500 | 12:05-12:10   |        nan    |            0.465 |          nan |                       nan     |
| btc-updown-5m-1777003800 | 12:10-12:15   |         -5.13 |            0.515 |            1 |                         0.475 |
| btc-updown-5m-1777004100 | 12:15-12:20   |        nan    |            0.285 |          nan |                       nan     |
| btc-updown-5m-1777004400 | 12:20-12:25   |        nan    |            0.865 |          nan |                       nan     |

## Threshold-rule comparison

| strategy       |   threshold_usd |   trades |   win_rate |   avg_entry_prob |   avg_pnl |   cum_pnl |
|:---------------|----------------:|---------:|-----------:|-----------------:|----------:|----------:|
| momentum       |              10 |       95 |     0.7368 |           0.7287 |   -0.0018 |    -0.175 |
| mean_reversion |              10 |       95 |     0.2632 |           0.7287 |   -0.0182 |    -1.725 |
| momentum       |              20 |       71 |     0.7887 |           0.758  |    0.0207 |     1.47  |
| mean_reversion |              20 |       71 |     0.2113 |           0.758  |   -0.0407 |    -2.89  |
| momentum       |              30 |       48 |     0.8125 |           0.7944 |    0.0081 |     0.39  |
| mean_reversion |              30 |       48 |     0.1875 |           0.7944 |   -0.0281 |    -1.35  |
| momentum       |              40 |       35 |     0.8571 |           0.8243 |    0.0229 |     0.8   |
| mean_reversion |              40 |       35 |     0.1429 |           0.8243 |   -0.0429 |    -1.5   |
| momentum       |              50 |       23 |     0.8261 |           0.8533 |   -0.0372 |    -0.855 |
| mean_reversion |              50 |       23 |     0.1739 |           0.8533 |    0.0172 |     0.395 |
| momentum       |              75 |       12 |     0.9167 |           0.895  |    0.0117 |     0.14  |
| mean_reversion |              75 |       12 |     0.0833 |           0.895  |   -0.0317 |    -0.38  |
| momentum       |             100 |        3 |     1      |           0.965  |    0.025  |     0.075 |
| mean_reversion |             100 |        3 |     0      |           0.965  |   -0.045  |    -0.135 |

## Missingness

| column               |   missing_ratio |   non_null |
|:---------------------|----------------:|-----------:|
| trade_volume_1s      |          1      |          0 |
| trade_count_1s       |          1      |          0 |
| btc_move_from_target |          0.2783 |      26269 |
| target_price         |          0.2783 |      26269 |
| mid_sum_cents        |          0.0568 |      34332 |
| mid_overround_cents  |          0.0568 |      34332 |
| mid_down_cents       |          0.0565 |      34342 |
| mid_down_prob        |          0.0565 |      34342 |
| spread_down_cents    |          0.0565 |      34342 |
| spread_up_cents      |          0.0558 |      34367 |
| mid_up_prob          |          0.0558 |      34367 |
| mid_up_cents         |          0.0558 |      34367 |
| ask_depth_down_5     |          0.0301 |      35303 |
| buy_down_cents       |          0.0301 |      35303 |
| buy_down_size        |          0.0301 |      35303 |

## Generated figures

- `reports/run_24869603988_attempt1_predictive/figures/btc_move_2m_hist.png`
- `reports/run_24869603988_attempt1_predictive/figures/up_rate_by_btc_move_bucket.png`
- `reports/run_24869603988_attempt1_predictive/figures/avg_pnl_by_threshold_strategy.png`

# v1_active_fill_mix 最近72小时快照

这份报告只看 `v1_active_fill_mix`，并回答一个问题：

- 如果从当前数据末端往回看 72 小时，在这段最近窗口里，这个策略表现如何？

## 当前 monthly_runs 覆盖

| run_name             |   file_count |   raw_row_count |
|:---------------------|-------------:|----------------:|
| 24949259015_attempt1 |           48 |           39535 |
| 24952032748_attempt1 |           48 |           36019 |
| 24986940107_attempt1 |           48 |           40793 |
| 25044896828_attempt1 |           48 |           39723 |
| 25100490855_attempt1 |           48 |           40583 |
| 25157440693_attempt1 |           48 |           40774 |
| 25208977834_attempt1 |           19 |           15716 |

## 当前数据末端时间

- latest timestamp: **2026-05-02 15:25:01+00:00**

## 最近72小时结果

- 是否数据充足：**True**
- 最近72小时覆盖的5分钟窗口数：**864**
- 最近72小时交易笔数：**40**
- 最近72小时总收益率：**-13.44%**
- 最近72小时期末本金（从100起算）：**86.56 USD**
- 最近72小时最大回撤：**38.15%**
- 最近72小时胜率：**65.00%**
- 最近72小时利润因子：**0.8298**

## 最近72小时交易明细

| strategy           | first_quote_ts            | run_name             | market_id                                      | slug                     | session_et   | component             |   entry_minute | side     |   fraction |   target_cost |   entry_price |   pnl_usd |   bankroll_after |   event_ret |   sim_max_drawdown |
|:-------------------|:--------------------------|:---------------------|:-----------------------------------------------|:-------------------------|:-------------|:----------------------|---------------:|:---------|-----------:|--------------:|--------------:|----------:|-----------------:|------------:|-------------------:|
| v1_active_fill_mix | 2026-04-29 17:15:00+00:00 | 25044896828_attempt1 | 25044896828_attempt1::btc-updown-5m-1777482900 | btc-updown-5m-1777482900 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       19.1263 |          0.69 |  -19.4035 |          219.675 |     -0.0812 |             0.0825 |
| v1_active_fill_mix | 2026-04-29 17:35:01+00:00 | 25044896828_attempt1 | 25044896828_attempt1::btc-updown-5m-1777484100 | btc-updown-5m-1777484100 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       17.574  |          0.6  |  -17.8669 |          201.809 |     -0.0813 |             0.1559 |
| v1_active_fill_mix | 2026-04-29 19:20:01+00:00 | 25044896828_attempt1 | 25044896828_attempt1::btc-updown-5m-1777490400 | btc-updown-5m-1777490400 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       16.1447 |          0.63 |  -16.4009 |          185.407 |     -0.0813 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 00:25:02+00:00 | 25044896828_attempt1 | 25044896828_attempt1::btc-updown-5m-1777508700 | btc-updown-5m-1777508700 | asia         | asia_breakout         |              4 | buy_up   |       0.08 |       14.8326 |          0.78 |    3.9934 |          189.401 |      0.0215 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 06:20:02+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777530000 | btc-updown-5m-1777530000 | london       | london_milddrop       |              2 | buy_down |       0.06 |       11.3641 |          0.63 |    6.4937 |          195.895 |      0.0343 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 06:40:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777531200 | btc-updown-5m-1777531200 | london       | london_milddrop       |              2 | buy_down |       0.04 |        7.8358 |          0.75 |    2.5075 |          198.402 |      0.0128 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 06:55:00+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777532100 | btc-updown-5m-1777532100 | london       | london_milddrop       |              2 | buy_down |       0.06 |       11.9041 |          0.66 |    5.9521 |          204.354 |      0.03   |             0.2245 |
| v1_active_fill_mix | 2026-04-30 07:45:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777535100 | btc-updown-5m-1777535100 | london       | london_milddrop       |              2 | buy_down |       0.06 |       12.2613 |          0.56 |  -12.4802 |          191.874 |     -0.0611 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 08:10:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777536600 | btc-updown-5m-1777536600 | london       | london_milddrop       |              2 | buy_down |       0.06 |       11.5124 |          0.55 |    9.21   |          201.084 |      0.048  |             0.2245 |
| v1_active_fill_mix | 2026-04-30 08:20:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777537200 | btc-updown-5m-1777537200 | london       | london_milddrop       |              2 | buy_down |       0.06 |       12.065  |          0.65 |    6.3109 |          207.395 |      0.0314 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 08:55:00+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777539300 | btc-updown-5m-1777539300 | london       | london_milddrop       |              2 | buy_down |       0.06 |       12.4437 |          0.75 |    3.982  |          211.377 |      0.0192 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 16:50:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777567800 | btc-updown-5m-1777567800 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       16.9101 |          0.75 |    5.4112 |          216.788 |      0.0256 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 17:05:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777568700 | btc-updown-5m-1777568700 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       17.343  |          0.58 |  -17.6421 |          199.146 |     -0.0814 |             0.2245 |
| v1_active_fill_mix | 2026-04-30 17:25:02+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777569900 | btc-updown-5m-1777569900 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       15.9317 |          0.54 |  -16.2267 |          182.919 |     -0.0815 |             0.2349 |
| v1_active_fill_mix | 2026-04-30 18:05:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777572300 | btc-updown-5m-1777572300 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       14.6335 |          0.53 |   12.7008 |          195.62  |      0.0694 |             0.2349 |
| v1_active_fill_mix | 2026-04-30 18:20:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777573200 | btc-updown-5m-1777573200 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       15.6496 |          0.66 |    7.8248 |          203.445 |      0.04   |             0.2349 |
| v1_active_fill_mix | 2026-04-30 18:55:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777575300 | btc-updown-5m-1777575300 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       16.2756 |          0.82 |    3.3742 |          206.819 |      0.0166 |             0.2349 |
| v1_active_fill_mix | 2026-04-30 19:45:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777578300 | btc-updown-5m-1777578300 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       16.5455 |          0.64 |  -16.8041 |          190.015 |     -0.0812 |             0.2349 |
| v1_active_fill_mix | 2026-04-30 23:30:00+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777591800 | btc-updown-5m-1777591800 | asia         | asia_breakout         |              4 | buy_up   |       0.08 |       15.2012 |          0.65 |  -15.4351 |          174.58  |     -0.0812 |             0.2698 |
| v1_active_fill_mix | 2026-05-01 04:50:01+00:00 | 25100490855_attempt1 | 25100490855_attempt1::btc-updown-5m-1777611000 | btc-updown-5m-1777611000 | asia         | asia_breakout         |              4 | buy_up   |       0.08 |       13.9664 |          0.48 |  -14.2574 |          160.323 |     -0.0817 |             0.3294 |
| v1_active_fill_mix | 2026-05-01 07:30:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777620600 | btc-updown-5m-1777620600 | london       | london_milddrop       |              2 | buy_down |       0.04 |        6.4129 |          0.55 |   -6.5295 |          153.793 |     -0.0407 |             0.3567 |
| v1_active_fill_mix | 2026-05-01 09:00:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777626000 | btc-updown-5m-1777626000 | london       | london_milddrop       |              2 | buy_down |       0.06 |        9.2276 |          0.71 |    3.639  |          157.432 |      0.0237 |             0.3567 |
| v1_active_fill_mix | 2026-05-01 09:20:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777627200 | btc-updown-5m-1777627200 | london       | london_milddrop       |              2 | buy_down |       0.06 |        9.4459 |          0.77 |   -9.5686 |          147.864 |     -0.0608 |             0.3815 |
| v1_active_fill_mix | 2026-05-01 09:25:02+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777627500 | btc-updown-5m-1777627500 | london       | london_milddrop       |              2 | buy_down |       0.04 |        5.9145 |          0.75 |    1.8927 |          149.756 |      0.0128 |             0.3815 |
| v1_active_fill_mix | 2026-05-01 09:35:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777628100 | btc-updown-5m-1777628100 | london       | london_milddrop       |              2 | buy_down |       0.06 |        8.9854 |          0.6  |    5.8405 |          155.597 |      0.039  |             0.3815 |
| v1_active_fill_mix | 2026-05-01 11:35:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777635300 | btc-updown-5m-1777635300 | london       | london_milddrop       |              2 | buy_down |       0.06 |        9.3358 |          0.72 |    3.5009 |          159.098 |      0.0225 |             0.3815 |
| v1_active_fill_mix | 2026-05-01 13:25:02+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777641900 | btc-updown-5m-1777641900 | london       | london_milddrop       |              2 | buy_down |       0.04 |        6.3639 |          0.73 |    2.2666 |          161.364 |      0.0142 |             0.3815 |
| v1_active_fill_mix | 2026-05-01 15:25:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777649100 | btc-updown-5m-1777649100 | us_open      | us_open_breakout      |              4 | buy_up   |       0.04 |        6.4546 |          0.83 |    1.2443 |          162.608 |      0.0077 |             0.3815 |
| v1_active_fill_mix | 2026-05-01 16:10:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777651800 | btc-updown-5m-1777651800 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       13.0087 |          0.66 |    6.5043 |          169.113 |      0.04   |             0.3815 |
| v1_active_fill_mix | 2026-05-01 16:15:00+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777652100 | btc-updown-5m-1777652100 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       13.529  |          0.59 |    9.1722 |          178.285 |      0.0542 |             0.3815 |
| v1_active_fill_mix | 2026-05-01 17:20:02+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777656000 | btc-updown-5m-1777656000 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       14.2628 |          0.65 |    7.4605 |          185.746 |      0.0418 |             0.3815 |
| v1_active_fill_mix | 2026-05-01 17:40:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777657200 | btc-updown-5m-1777657200 | us_afternoon | us_afternoon_milddrop |              2 | buy_down |       0.08 |       14.8596 |          0.74 |    5.0202 |          190.766 |      0.027  |             0.3815 |
| v1_active_fill_mix | 2026-05-02 02:15:02+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777688100 | btc-updown-5m-1777688100 | asia         | asia_breakout         |              4 | buy_up   |       0.08 |       15.2613 |          0.35 |   27.9063 |          218.672 |      0.1463 |             0.3815 |
| v1_active_fill_mix | 2026-05-02 03:55:01+00:00 | 25157440693_attempt1 | 25157440693_attempt1::btc-updown-5m-1777694100 | btc-updown-5m-1777694100 | asia         | asia_breakout         |              4 | buy_up   |       0.08 |       17.4938 |          0.63 |    9.9964 |          228.668 |      0.0457 |             0.3815 |
| v1_active_fill_mix | 2026-05-02 06:55:01+00:00 | 25208977834_attempt1 | 25208977834_attempt1::btc-updown-5m-1777704900 | btc-updown-5m-1777704900 | london       | london_milddrop       |              2 | buy_down |       0.04 |        3.2    |          0.32 |   -3.3    |          225.369 |     -0.0144 |             0.3815 |
| v1_active_fill_mix | 2026-05-02 08:55:02+00:00 | 25208977834_attempt1 | 25208977834_attempt1::btc-updown-5m-1777712100 | btc-updown-5m-1777712100 | london       | london_milddrop       |              2 | buy_down |       0.04 |        9.0147 |          0.93 |    0.5816 |          225.95  |      0.0026 |             0.3815 |
| v1_active_fill_mix | 2026-05-02 10:45:01+00:00 | 25208977834_attempt1 | 25208977834_attempt1::btc-updown-5m-1777718700 | btc-updown-5m-1777718700 | london       | london_milddrop       |              2 | buy_down |       0.04 |        9.038  |          0.46 |   -9.2345 |          216.716 |     -0.0409 |             0.3815 |
| v1_active_fill_mix | 2026-05-02 11:10:01+00:00 | 25208977834_attempt1 | 25208977834_attempt1::btc-updown-5m-1777720200 | btc-updown-5m-1777720200 | london       | london_milddrop       |              2 | buy_down |       0.06 |       13.0029 |          0.84 |    2.322  |          219.038 |      0.0107 |             0.3815 |
| v1_active_fill_mix | 2026-05-02 11:25:01+00:00 | 25208977834_attempt1 | 25208977834_attempt1::btc-updown-5m-1777721100 | btc-updown-5m-1777721100 | london       | london_milddrop       |              2 | buy_down |       0.06 |       13.1423 |          0.89 |    1.4767 |          220.514 |      0.0067 |             0.3815 |
| v1_active_fill_mix | 2026-05-02 11:40:01+00:00 | 25208977834_attempt1 | 25208977834_attempt1::btc-updown-5m-1777722000 | btc-updown-5m-1777722000 | london       | london_milddrop       |              2 | buy_down |       0.06 |       13.2309 |          0.4  |  -13.5616 |          206.953 |     -0.0615 |             0.3815 |

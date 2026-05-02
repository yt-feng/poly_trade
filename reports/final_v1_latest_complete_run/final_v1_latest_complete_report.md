# Final v1 on latest complete monthly run

这份报告只回测 `monthly_runs` 中**最新的完整 run**。完整的定义是：

- 先看当前 `monthly_runs/*` 每个 run 的文件数
- 以**最大文件数 = 48** 作为“完整 run”的标准
- 在所有完整 run 中，取 `run_name` 最新的一个：**25157440693_attempt1**

## 当前 monthly_runs 覆盖

| run_name             |   file_count |   raw_row_count |
|:---------------------|-------------:|----------------:|
| 24949259015_attempt1 |           48 |           39535 |
| 24952032748_attempt1 |           48 |           36019 |
| 24986940107_attempt1 |           48 |           40793 |
| 25044896828_attempt1 |           48 |           39723 |
| 25100490855_attempt1 |           48 |           40583 |
| 25157440693_attempt1 |           48 |           40774 |
| 25208977834_attempt1 |           18 |           14783 |

## 仅针对 `25157440693_attempt1` 的 final v1 候选结果

| strategy             |   trades |   ending_bankroll |   total_return |   avg_trade_return_on_cost |   median_trade_return_on_cost |   win_rate |   profit_factor |   max_drawdown |   avg_fraction |   worst_144_window_return |   median_144_window_return |   pct_positive_144_windows |   pct_over_10pct_144_windows |   active_144_window_rate |   num_144_windows |   score_end |   score_dd |   score_36h |   score_36h_10 |   score_active |   score_pf |   v1_score | meets_dd_lt_30   | meets_36h_gt_10_all   | meets_v1_goal   |
|:---------------------|---------:|------------------:|---------------:|---------------------------:|------------------------------:|-----------:|----------------:|---------------:|---------------:|--------------------------:|---------------------------:|---------------------------:|-----------------------------:|-------------------------:|------------------:|------------:|-----------:|------------:|---------------:|---------------:|-----------:|-----------:|:-----------------|:----------------------|:----------------|
| v1_balanced_mix      |       11 |           158.471 |         0.5847 |                     0.458  |                        0.5    |     0.9091 |          7.9945 |         0.081  |         0.0891 |                    0.2286 |                     0.2673 |                          1 |                            1 |                        1 |               146 |         1   |        0.4 |         1   |            0.7 |            0.7 |        0.8 |       0.77 | True             | True                  | True            |
| v1_conservative_mix  |       10 |           143.637 |         0.4364 |                     0.4845 |                        0.5115 |     0.9    |          8.0136 |         0.0608 |         0.072  |                    0.1706 |                     0.1983 |                          1 |                            1 |                        1 |               146 |         0.6 |        0.8 |         0.4 |            0.7 |            0.7 |        1   |       0.66 | True             | True                  | True            |
| v1_adaptive_mix      |       10 |           151.823 |         0.5182 |                     0.4845 |                        0.5115 |     0.9    |          6.9321 |         0.0847 |         0.0882 |                    0.193  |                     0.2309 |                          1 |                            1 |                        1 |               146 |         0.8 |        0.2 |         0.8 |            0.7 |            0.7 |        0.6 |       0.63 | True             | True                  | True            |
| v1_active_fill_mix   |       14 |           142.63  |         0.4263 |                     0.3354 |                        0.3847 |     0.8571 |          5.2456 |         0.0777 |         0.0629 |                    0.1822 |                     0.226  |                          1 |                            1 |                        1 |               146 |         0.4 |        0.6 |         0.6 |            0.7 |            0.7 |        0.4 |       0.58 | True             | True                  | True            |
| v1_logit_overlay_mix |        0 |           100     |         0      |                   nan      |                      nan      |   nan      |        nan      |         0      |       nan      |                    0      |                     0      |                          0 |                            0 |                        0 |               146 |         0.2 |        1   |         0.2 |            0.2 |            0.2 |        0.2 |       0.36 | True             | False                 | False           |

## session / component 拆分

| strategy            | session_et   | component             |   trades |   avg_pnl_usd |   total_pnl_usd |   avg_fraction |   win_rate |
|:--------------------|:-------------|:----------------------|---------:|--------------:|----------------:|---------------:|-----------:|
| v1_active_fill_mix  | asia         | asia_breakout         |        2 |       11.8208 |         23.6415 |         0.08   |     1      |
| v1_active_fill_mix  | london       | london_milddrop       |        7 |        0.0928 |          0.6497 |         0.0514 |     0.7143 |
| v1_active_fill_mix  | us_afternoon | us_afternoon_milddrop |        4 |        4.3907 |         17.5629 |         0.08   |     1      |
| v1_active_fill_mix  | us_open      | us_open_breakout      |        1 |        0.7761 |          0.7761 |         0.04   |     1      |
| v1_adaptive_mix     | asia         | asia_breakout         |        2 |       14.3661 |         28.7322 |         0.0938 |     1      |
| v1_adaptive_mix     | london       | london_milddrop       |        4 |        0.6207 |          2.4829 |         0.0816 |     0.75   |
| v1_adaptive_mix     | us_afternoon | us_afternoon_milddrop |        4 |        5.1521 |         20.6082 |         0.092  |     1      |
| v1_balanced_mix     | asia         | asia_breakout         |        2 |       15.8699 |         31.7398 |         0.1    |     1      |
| v1_balanced_mix     | london       | london_milddrop       |        4 |        0.6791 |          2.7165 |         0.08   |     0.75   |
| v1_balanced_mix     | us_afternoon | us_afternoon_milddrop |        4 |        5.7068 |         22.827  |         0.1    |     1      |
| v1_balanced_mix     | us_open      | breakout_filler       |        1 |        1.188  |          1.188  |         0.06   |     1      |
| v1_conservative_mix | asia         | asia_breakout         |        2 |       11.9042 |         23.8084 |         0.08   |     1      |
| v1_conservative_mix | london       | london_milddrop       |        4 |        0.5354 |          2.1417 |         0.06   |     0.75   |
| v1_conservative_mix | us_afternoon | us_afternoon_milddrop |        4 |        4.4217 |         17.6868 |         0.08   |     1      |

## 当前最新完整 run 的最优候选

- 策略：**v1_balanced_mix**
- 期末本金：**158.47 USD**
- 最大回撤：**8.10%**
- 36小时最差窗口收益：**22.86%**
- 36小时 >10% 窗口占比：**100.00%**
- active 36小时窗口占比：**100.00%**

## 图表

![Final v1 candidate score](final_v1_latest_complete_figures/final_v1_scores.png)

![36小时最差窗口收益](final_v1_latest_complete_figures/final_v1_worst144.png)

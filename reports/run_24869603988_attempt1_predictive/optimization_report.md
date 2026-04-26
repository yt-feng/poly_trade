# 优化策略回测补充报告

## 修正说明

上一版优化报告把多条策略放在同一条资金路径里再按策略名切片汇总，口径不对。这一版已经改成**每条策略独立回测**，因此现在可以和 `rule_drop10_down + fixed_20pct` 做正面对比。

## 这版优化在搜什么

- 基线：`rule_drop10_down`
- 过滤器：price cap、size imbalance、流动性阈值、组合过滤
- 仓位：fixed 10%、fixed 20%、历史命中率版 Kelly / capped Kelly

## 候选策略-仓位结果

| strategy                         | sizing                 |   trades |   ending_bankroll |   total_return |   avg_trade_return_on_cost |   max_drawdown |
|:---------------------------------|:-----------------------|---------:|------------------:|---------------:|---------------------------:|---------------:|
| rule_drop10_to50_down            | full_kelly             |       42 |           635.343 |        5.35343 |                   0.225918 |      0.878792  |
| rule_drop10_down                 | fixed_20pct            |       79 |           526.164 |        4.26164 |                   0.134403 |      0.467419  |
| rule_drop10_to50_down            | fixed_20pct            |       54 |           481.827 |        3.81827 |                   0.180096 |      0.495034  |
| rule_drop10_down_combo75_neg     | full_kelly             |        8 |           442.075 |        3.42075 |                   0.494465 |      0.108631  |
| rule_drop10_to50_down            | half_kelly_capped20    |       42 |           434.844 |        3.34844 |                   0.225918 |      0.456895  |
| rule_drop10_to50_down            | half_kelly             |       42 |           429.923 |        3.29923 |                   0.225918 |      0.652987  |
| rule_drop10_down_pricecap80      | fixed_20pct            |       47 |           409.375 |        3.09375 |                   0.185058 |      0.548903  |
| rule_drop10_down_size_strong_neg | full_kelly             |       20 |           341.768 |        2.41768 |                   0.226334 |      0.704536  |
| rule_drop10_to30_down            | fixed_20pct            |       26 |           333.923 |        2.33923 |                   0.295906 |      0.409611  |
| rule_drop10_down                 | half_kelly_capped20    |       59 |           314.605 |        2.14605 |                   0.136501 |      0.535376  |
| rule_drop10_down_pricecap80      | half_kelly_capped20    |       33 |           295.594 |        1.95594 |                   0.21692  |      0.542932  |
| rule_drop10_down_pricecap80      | half_kelly             |       33 |           289.291 |        1.89291 |                   0.21692  |      0.665223  |
| rule_drop10_down_size_strong_neg | half_kelly             |       20 |           288.042 |        1.88042 |                   0.226334 |      0.347647  |
| rule_drop10_down_pricecap75      | fixed_20pct            |       34 |           282.243 |        1.82243 |                   0.189063 |      0.612268  |
| rule_drop10_to50_down            | quarter_kelly_capped20 |       42 |           278.29  |        1.7829  |                   0.225918 |      0.369321  |
| rule_drop10_to50_down            | quarter_kelly          |       42 |           278.29  |        1.7829  |                   0.225918 |      0.369321  |
| rule_drop10_down                 | fixed_10pct            |       79 |           277.556 |        1.77556 |                   0.134403 |      0.233971  |
| rule_drop10_down_good_liq250     | fixed_20pct            |       39 |           272.238 |        1.72238 |                   0.159289 |      0.446937  |
| rule_drop10_down_pricecap80      | full_kelly             |       33 |           270.162 |        1.70162 |                   0.21692  |      0.922733  |
| rule_drop10_down_combo80_neg     | fixed_20pct            |       24 |           267.232 |        1.67232 |                   0.254252 |      0.387232  |
| rule_drop10_down_size_strong_neg | fixed_20pct            |       36 |           264.121 |        1.64121 |                   0.170873 |      0.203676  |
| rule_drop20_down                 | fixed_20pct            |       68 |           261.072 |        1.61072 |                   0.085555 |      0.469228  |
| rule_drop10_to50_down            | fixed_10pct            |       54 |           257.361 |        1.57361 |                   0.180096 |      0.260865  |
| rule_drop10_down_combo75_neg     | fixed_20pct            |       17 |           242.37  |        1.4237  |                   0.31964  |      0.387232  |
| rule_drop10_down_combo75_neg     | half_kelly             |        8 |           241.348 |        1.41348 |                   0.494465 |      0.0543155 |
| rule_drop10_down_combo80_liq     | fixed_20pct            |       23 |           239.524 |        1.39524 |                   0.235954 |      0.47669   |
| rule_drop10_down_size_neg        | fixed_20pct            |       40 |           237.659 |        1.37659 |                   0.142898 |      0.352394  |
| rule_drop10_down_pricecap80      | fixed_10pct            |       47 |           232.77  |        1.3277  |                   0.185058 |      0.302896  |
| rule_drop10_down                 | half_kelly             |       59 |           230.425 |        1.30425 |                   0.136501 |      0.814611  |
| rule_drop10_down                 | quarter_kelly_capped20 |       59 |           228.595 |        1.28595 |                   0.136501 |      0.467507  |

## 当前最佳优化结果

- 策略：**rule_drop10_to50_down**
- 仓位：**full_kelly**
- 交易笔数：**42**
- 期末本金：**635.34 USD**
- 总收益率：**535.34%**
- 最大回撤：**87.88%**

## 图表

### 优化策略Top期末本金

![优化策略Top期末本金](optimization_figures/optimizer_top_endings.png)

### 最佳优化策略本金曲线

![最佳优化策略本金曲线](optimization_figures/optimizer_best_curve.png)

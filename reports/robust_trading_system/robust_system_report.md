# Polymarket 5min 稳健交易系统

## 研究过程和约束

- 目标不是再堆一个固定策略，而是按 workbook 做成 `q -> edge -> execution gate -> sizing -> validation search -> test` 的交易系统。
- `q` 是校准后的胜率估计；实际下单只看 `q - 可成交买价 - fee` 是否超过阈值。
- 当前入口模式是 dense4s：从 quote path 中按 4 秒节流生成候选，不代表机械地每 4 秒下单；是否下单仍由 edge、depth、spread、risk gate 决定。
- 多次交易被放在执行层穷举：每个 5min market 最多 1/2/4 次，最小入场间隔 4/8/16 秒；同一时间只选一边最高 edge。
- 每个 5min market 有总 event cap；多次交易会把单笔 cap 拆小，避免同一个事件里隐性加杠杆。
- `market_prior_only` 只作为市场基准展示，不允许被选成交易模型，因为它通常没有扣除价差和 fee 后的可交易 edge。
- 模型/参数只用 train + validation；test split 不参与选模型、不参与选执行参数。

## 数据和切分

- 数据窗口：2026-05-03T23:45:00+08:00 到 2026-06-02T23:45:00+08:00
- Market 数：5989
- 候选入场数：761427
- Train/validation/test 候选：662478 / 30499 / 68450
- 模型训练/校准抽样：120000 / 30499
- Embargo：300 秒
- 本次执行配置评估：1 组，模式 `fixed_selected_execution`；完整执行网格是 1080 组。
- Rolling walk-forward：2 个 fold，执行参数固定为主报告选中配置。

## 选中的交易系统

- 模型类型：`mixed_blend`
- 模型因子组合：`mixed_top2_logit_blend`（15 个底层因子），L2 0.01
- 执行配置：`q_edge_ne0.02_be0.06_d50_cap0.01_mt1_gap4s__settle`
- 普通价格 edge 阈值：0.0200
- 边界价格 edge 阈值：0.0600
- 最小盘口深度：50
- 单 market 总 cap：1.00%
- 每个 market 最多交易次数：1
- 最小入场间隔：4 秒
- 出场策略：`settle`
- 边界价格定义：p>=0.90 或 p<=0.10

## 模型混合结构

- 这是 mixed/blended 模型：上层输入不是原始因子，而是几个基础模型的 raw logit。
- `mixed_meta` `intercept`：weight -0.0241，features 0，L2 0.01
- `mixed_member` `prior_plus_btc@l2=0.01`：weight 0.5663，features 4，L2 0.01
- `mixed_member` `prior_btc_liquidity@l2=0.01`：weight 0.5576，features 15，L2 0.01

## 最终 OOS 结果

- Validation：$209.25，收益 109.25%，交易 162，DD 4.03%
- Test：$1,023.63，收益 923.63%，交易 559，DD 6.98%

## 概率质量

- train：model logloss 0.4994，market baseline 0.5078，delta -0.0084，AUC 0.835，ECE 0.006
- validation：model logloss 0.5044，market baseline 0.5169，delta -0.0125，AUC 0.827，ECE 0.011
- test：model logloss 0.4673，market baseline 0.4878，delta -0.0205，AUC 0.855，ECE 0.012

## 因子组合 / mixed 搜索

- `mixed_top2_logit_blend`：kind mixed_blend，validation logloss 0.5044，selection loss 0.5052，penalty 0.0008，因子数 15，L2 0.01
- `prior_plus_btc`：kind single，validation logloss 0.5063，selection loss 0.5063，penalty 0.0000，因子数 4，L2 0.01
- `mixed_top3_logit_blend`：kind mixed_blend，validation logloss 0.5051，selection loss 0.5067，penalty 0.0016，因子数 17，L2 0.01
- `prior_btc_liquidity`：kind single，validation logloss 0.5070，selection loss 0.5070，penalty 0.0000，因子数 15，L2 0.01
- `no_timing_boundary`：kind single，validation logloss 0.5073，selection loss 0.5073，penalty 0.0000，因子数 16，L2 0.01
- `core_no_timing`：kind single，validation logloss 0.5073，selection loss 0.5073，penalty 0.0000，因子数 17，L2 0.01
- `full`：kind single，validation logloss 0.5073，selection loss 0.5073，penalty 0.0000，因子数 26，L2 0.01
- `prior_btc_interactions`：kind single，validation logloss 0.5086，selection loss 0.5086，penalty 0.0000，因子数 9，L2 0.01

## 最大模型权重

- `prior_plus_btc@l2=0.01::logit_market_p`: 0.9726
- `prior_btc_liquidity@l2=0.01::logit_market_p`: 0.7769
- `mixed_member::prior_plus_btc@l2=0.01`: 0.5663
- `mixed_member::prior_btc_liquidity@l2=0.01`: 0.5576
- `prior_plus_btc@l2=0.01::signed_btc_move_2m`: 0.5354
- `prior_plus_btc@l2=0.01::signed_btc_move_entry`: 0.4533
- `prior_btc_liquidity@l2=0.01::signed_btc_move_2m`: 0.3509
- `prior_btc_liquidity@l2=0.01::signed_btc_move_entry`: 0.2635
- `prior_btc_liquidity@l2=0.01::btc_move_entry_x_depth`: 0.2517
- `prior_btc_liquidity@l2=0.01::btc_move_2m_x_uncertainty`: 0.2258

## 选中交易的因子组贡献

- `btc`：avg logit contribution 0.3493，avg abs 0.3718，正贡献比例 89.98%
- `intercept`：avg logit contribution -0.1691，avg abs 0.1691，正贡献比例 0.00%
- `interactions`：avg logit contribution 0.1280，avg abs 0.1733，正贡献比例 80.32%
- `market_prior`：avg logit contribution 0.0404，avg abs 0.2274，正贡献比例 66.19%
- `liquidity_micro`：avg logit contribution 0.0111，avg abs 0.0209，正贡献比例 64.94%
- `boundary`：avg logit contribution -0.0010，avg abs 0.0010，正贡献比例 0.00%

## 稳定因子提示

- `btc_move_2m_x_uncertainty`：train pnl corr 0.156，validation 0.150，test 0.232
- `signed_btc_move_2m`：train pnl corr 0.118，validation 0.080，test 0.155
- `btc_move_entry_x_uncertainty`：train pnl corr 0.047，validation 0.041，test 0.041
- `signed_btc_move_entry`：train pnl corr 0.032，validation 0.012，test 0.024
- `signed_size_imbalance_2m`：train pnl corr -0.026，validation -0.005，test -0.030
- `btc_move_entry_x_depth`：train pnl corr 0.025，validation 0.010，test 0.018
- `log_depth_side`：train pnl corr -0.015，validation -0.004，test -0.020
- `logit_market_p`：train pnl corr -0.009，validation -0.025，test -0.001
- `overround_median_first2m`：train pnl corr -0.011，validation -0.013，test -0.011
- `session_us_open`：train pnl corr -0.000，validation nan，test -0.001
- `entry_minute_2`：train pnl corr 0.000，validation 0.000，test 0.001
- `entry_minute_4`：train pnl corr -0.000，validation -0.000，test -0.001

## 多交易执行组合摘要

- max_trades=1，gap=4s，exit=settle：validation score 1.202，ending $209.25，trades 162

## 决策漏斗

- validation 候选：30499（候选占比 100.00%，较上一层保留 100.00%）
- validation 过 edge：6735（候选占比 22.08%，较上一层保留 22.08%）
- validation 过 depth：4935（候选占比 16.18%，较上一层保留 73.27%）
- validation 过 spread：4934（候选占比 16.18%，较上一层保留 99.98%）
- validation 过 overround：4927（候选占比 16.15%，较上一层保留 99.86%）
- validation 实际执行：162（候选占比 0.53%，较上一层保留 3.29%）
- test 候选：68450（候选占比 100.00%，较上一层保留 100.00%）
- test 过 edge：15724（候选占比 22.97%，较上一层保留 22.97%）
- test 过 depth：11407（候选占比 16.66%，较上一层保留 72.55%）
- test 过 spread：11391（候选占比 16.64%，较上一层保留 99.86%）
- test 过 overround：11377（候选占比 16.62%，较上一层保留 99.88%）
- test 实际执行：559（候选占比 0.82%，较上一层保留 4.91%）

## 成本/滑点压力测试

- base：ending $1,023.63，return 923.63%，trades 559，DD 6.98%
- fee_plus_0.5c：ending $906.55，return 806.55%，trades 555，DD 7.02%
- fee_plus_1.0c：ending $935.53，return 835.53%，trades 546，DD 7.39%
- edge_plus_1.0c：ending $1,103.53，return 1003.53%，trades 546，DD 7.22%

## 价格桶执行表现

- test 0.10-0.25：交易 39，总 PnL 21.63，胜率 28.21%，avg edge 0.0356
- test 0.25-0.75：交易 511，总 PnL 891.11，胜率 72.60%，avg edge 0.0876
- test 0.75-0.90：交易 9，总 PnL 10.89，胜率 100.00%，avg edge 0.0776
- train 0.10-0.25：交易 95，总 PnL -130.73，胜率 18.95%，avg edge 0.0373
- train 0.25-0.75：交易 786，总 PnL 1392.75，胜率 67.18%，avg edge 0.0810
- train 0.75-0.90：交易 8，总 PnL 12.79，胜率 100.00%，avg edge 0.0858
- train p<=0.10：交易 3，总 PnL 6.82，胜率 33.33%，avg edge 0.0665
- validation 0.10-0.25：交易 14，总 PnL 25.55，胜率 35.71%，avg edge 0.0367
- validation 0.25-0.75：交易 145，总 PnL 81.96，胜率 68.97%，avg edge 0.0794
- validation 0.75-0.90：交易 3，总 PnL 1.74，胜率 100.00%，avg edge 0.1572

## 稳健性解释

- 如果 validation 很强但 test 变弱，不应该反向调 test；下一步应简化模型或等更多月份数据做 walk-forward。
- 边界价格单独 gate，因为 p>=0.90 / p<=0.10 的尾部风险不对称。
- 低买高卖 exit 已进入 validation 搜索；如果最终没选中，说明在当前数据和风控下不如被选配置。
- 当前仍没有真实队列位置、订单失败率和滑点模型；paper trading 前不建议放大仓位。

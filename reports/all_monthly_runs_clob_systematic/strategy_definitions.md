# all_monthly_runs_clob_systematic 策略定义

这份文档专门说明 `analysis/all_monthly_clob_systematic_research_v3.py` 里当前回测过的策略。

---

## 一、研究目标

这一层只使用 `data/monthly_runs/*` 里已经从 Polymarket CLOB 拉下来的真实事件数据，强调：

- 价格路径
- 流动性
- 深度
- spread
- overround
- trade activity
- 以及收益/风险平衡

回测输出不是只看 `ending_bankroll`，还会同时看：

- `max_drawdown`
- `win_rate`
- `profit_factor`
- `max_consecutive_losses`
- `robustness_score`

---

## 二、当前明确使用的 CLOB 指标

- `buy_up_size_2m / buy_down_size_2m`
- `sell_up_size_2m / sell_down_size_2m`
- `bid_depth_imbalance_updown_2m`
- `book_pressure_up_2m / book_pressure_down_2m`
- `spread_up_median_first2m / spread_down_median_first2m`
- `overround_median_first2m`
- `trade_count_sum_first2m / trade_volume_sum_first2m`
- `quote_count_first2m`
- `realized_vol_first2m / path_efficiency_first2m / max_drawdown_first2m / max_rebound_first2m`

---

## 三、静态规则策略

### 1. `static_m1_drop20_down`

- 条件：第 1 分钟 BTC 相对目标价下跌超过 20 美元
- 行为：买 Down
- 直觉：早期急跌延续

### 2. `static_m2_drop10_down_liq`

- 条件：
  - 第 2 分钟 BTC 下跌超过 10 美元
  - `buy_down_size_2m >= 120`
  - `spread_down_median_first2m <= 0.06`
- 行为：买 Down
- 直觉：不是所有跌势都追，只在 Down 侧流动性尚可、spread 不太差时做

### 3. `static_m2_sharpdrop_up_liq`

- 条件：
  - 第 2 分钟 BTC 跌幅处于 `(-50, -30]`
  - `buy_up_size_2m >= 120`
  - `spread_up_median_first2m <= 0.06`
  - `buy_up_price_2m <= 0.45`
- 行为：买 Up
- 直觉：大幅下跌后做反弹，但只在 Up 赔率还不太贵且流动性足够时介入

### 4. `static_m2_milddrop_down_book`

- 条件：
  - 第 2 分钟 BTC 跌幅处于 `(-30, -10]`
  - `size_imbalance_updown_2m <= 0`
  - `book_pressure_down_2m >= -0.2`
- 行为：买 Down
- 直觉：温和下跌 + Down 侧盘口没有明显变差，继续做 Down

### 5. `static_m2_extremeup_fade_down`

- 条件：
  - 第 2 分钟 BTC 上涨超过 50 美元
  - `buy_down_price_2m <= 0.20`
- 行为：买 Down
- 直觉：极端上涨后做反向 fade，但只在 Down 很便宜时做

### 6. `static_m4_breakout_up_tight`

- 条件：
  - 第 4 分钟 BTC 上涨处于 `(30, 50]`
  - `buy_up_size_4m >= 100`
  - `buy_up_price_4m <= 0.90`
- 行为：买 Up
- 直觉：偏晚的突破追涨，只在 Up 侧仍有一定可成交量时做

---

## 四、动态 state selector 策略

### 7. `state_selector_pnl`

- 候选微策略：
  - `early_drop_cont`
  - `sharp_drop_rev`
  - `mild_drop_cont`
  - `extreme_up_fade`
  - `late_breakout`
- 方法：
  - 对当前事件，先识别其 `regime`
  - 再从历史同类状态中统计实际实现 PnL
  - 选平均 PnL 最优的一边
- 直觉：不用一个固定规则打天下，而是在历史相似状态里选择表现最好的一种交易

### 8. `state_selector_robust`

- 和 `state_selector_pnl` 类似
- 但要求更严格：
  - `support >= 20`
  - `win_rate >= 55%`
  - `mean_pnl > 0.02`
- 直觉：偏向牺牲部分收益，换取更稳的样本支持和更高胜率

---

## 五、滚动逻辑回归策略

### 9. `logistic_selector_edge`

- 用现有 CLOB 特征滚动训练逻辑回归，输出 `pred_prob_up_logit`
- 然后比较：
  - 买 Up 时：`pred_prob_up_logit - buy_up_price`
  - 买 Down 时：`(1 - pred_prob_up_logit) - buy_down_price`
- 再扣除：
  - fee
  - spread penalty
  - safety margin
- 只在 edge 足够大时交易

### 10. `logistic_selector_robust`

- 和 `logistic_selector_edge` 相同
- 但额外更保守：
  - 边际要求更高
  - 对过贵的 Up 不做
- 直觉：降低过拟合和高价追逐的风险

---

## 六、当前结论（就这层而言）

从当前全历史回测看：

- **收益最高**的主线是：`static_m2_milddrop_down_book`
- **稳健性最好**的主线是：`static_m4_breakout_up_tight`

但两者各有问题：

- `static_m2_milddrop_down_book` 收益高，但回撤偏大
- `static_m4_breakout_up_tight` 很稳，但交易笔数偏少、覆盖范围偏窄

因此下一步更值得测试的是：

1. 时间分段版策略（例如按美股开盘前后）
2. 12 小时滚动窗口最差收益约束
3. 仓位自适应 / 降杠杆版组合策略
4. 只在 spread / liquidity / overround 同时达标时交易

# 所有已测试策略的定义总览

这份文档把 repo 里目前出现过的主要策略层统一整理到一起，方便你知道：

- 每条策略到底在哪个脚本里定义
- 它是基于价格、盘口、量能、历史条件概率，还是 rolling filter
- 它买 Up 还是买 Down
- 它和其他策略层之间是什么关系

---

## 1. 策略层总览

目前 repo 里主要有 5 层策略：

1. **bankroll 基础层**
   - 脚本：`analysis/build_run_24869603988_bankroll.py`
   - 目的：用最基础的价格/方向规则，验证 100 美元本金滚动回测下哪些主线最强

2. **optimizer 过滤层**
   - 脚本：`analysis/optimize_run_24869603988_bankroll.py`
   - 目的：围绕最强主线（尤其是 `drop10_down`）做更细的过滤器搜索

3. **classic 经典策略层**
   - 脚本：`analysis/classic_run_24869603988_strategies.py`
   - 目的：把常见的趋势确认、value、rolling filter、固定仓位/capped Kelly 全部系统化测试

4. **extended classic 扩展经典层**
   - 脚本：`analysis/extended_classic_run_24869603988.py`
   - 目的：进一步覆盖波动率/路径过滤、盘口压力、regime filter、rolling 健康度、分层仓位、价格区间搜索

5. **latest regime 最新状态切换层**
   - 脚本：`analysis/discover_latest_regime_strategies.py`
   - 目的：针对最新数据集里暴露出的新结构，做“分钟感知 + 状态切换”的专门策略

---

## 2. bankroll 基础层策略

### 2.1 rule_drop10_down

- 条件：`btc_move_2m <= -10`
- 行为：买 Down
- 直觉：前 2 分钟已经跌超 10 美元，继续做 Down 动量

### 2.2 rule_drop20_down

- 条件：`btc_move_2m <= -20`
- 行为：买 Down
- 直觉：更强下跌动量版本

### 2.3 rule_drop30_down

- 条件：`btc_move_2m <= -30`
- 行为：买 Down
- 直觉：更极端的下跌动量版本

### 2.4 rule_rise30to50_up

- 条件：`30 < btc_move_2m <= 50`
- 行为：买 Up
- 直觉：上涨侧并不是普遍追涨，只测试研究里较有希望的中等上涨区间

### 2.5 rule_interval_best

- 条件：
  - 若 `-30 < btc_move_2m <= -10`，买 Down
  - 若 `30 < btc_move_2m <= 50`，买 Up
  - 其余跳过
- 直觉：只保留前面研究里最像“甜蜜区间”的涨跌区域

### 2.6 model_value_baseline / logistic / random_forest

- 先做 walk-forward 预测 `pred_prob_up`
- 再判断：
  - 如果 `pred_prob_up > buy_up_price + fee + edge_buffer`，买 Up
  - 如果 `(1 - pred_prob_up) > buy_down_price + fee + edge_buffer`，买 Down
  - 否则跳过
- 其中 `edge_buffer` 可能是 0 或 2%

---

## 3. optimizer 过滤层策略

这一层是围绕 `drop10_down` 做更细的组合过滤。

### 3.1 rule_drop10_down

- 基线版本：`btc_move_2m <= -10` 就买 Down

### 3.2 rule_drop10_to50_down

- 条件：`-50 < btc_move_2m <= -10`
- 行为：买 Down
- 直觉：只留中等下跌，避免过于极端的情况

### 3.3 rule_drop10_to30_down

- 条件：`-30 < btc_move_2m <= -10`
- 行为：买 Down

### 3.4 rule_drop10_down_pricecap80 / pricecap75

- 条件：
  - `btc_move_2m <= -10`
  - `buy_down_price_2m <= 0.80` 或 `<= 0.75`
- 行为：买 Down
- 直觉：避免 Down 已经太贵

### 3.5 rule_drop10_down_size_neg / size_strong_neg

- 条件：
  - `btc_move_2m <= -10`
  - `size_imbalance_updown_2m <= 0` 或更强的负值阈值
- 行为：买 Down
- 直觉：盘口 size 更偏向 Down 时才做

### 3.6 rule_drop10_down_good_liq250 / good_liq400

- 条件：
  - `btc_move_2m <= -10`
  - `buy_down_size_2m >= 250` 或 `>= 400`
- 行为：买 Down
- 直觉：流动性足够深时才做

### 3.7 rule_drop10_down_combo80_neg / combo75_neg / combo80_liq

- 条件：
  - 下跌动量 + 价格约束 + size imbalance / liquidity 组合过滤
- 行为：买 Down
- 直觉：把多个有利条件叠加，尝试提高质量

### 3.8 rule_rise30to50_up 及其过滤版本

- 上涨侧对称策略，测试价格上限、正向 size、流动性过滤

### 3.9 state_policy

- 用历史条件分桶状态（move bucket、price bucket、liquidity / size bucket）来决定当前更适合买 Up 还是 Buy Down
- 若历史平均 PnL 不为正，则跳过

---

## 4. classic 经典策略层

这一层已经有更详细的单独文档：

- `reports/run_24869603988_attempt1_predictive/classic_strategy_definitions.md`

这里给一个简要索引：

### 4.1 趋势类

- `classic_trend_down_basic`
- `classic_trend_down_score3`
- `classic_trend_down_score4`
- `classic_trend_down_midrange`
- `classic_trend_down_liq`
- `classic_trend_down_price`
- `classic_trend_down_combo`
- `classic_trend_up_basic`
- `classic_trend_up_score3`

### 4.2 中性/价值类

- `classic_neutral_value_up`

### 4.3 动态 value 类

- `value_down_margin2`
- `value_down_margin5`
- `value_down_margin2_book`
- `value_up_margin2`

### 4.4 Rolling 过滤类

- `rolling_rule_drop10_down`

### 4.5 仓位类

- `fixed_10pct / fixed_15pct / fixed_20pct / fixed_25pct`
- `full_kelly_capped20 / half_kelly_capped20 / quarter_kelly_capped20`

---

## 5. extended classic 扩展经典层

这一层是在 classic 基础上，系统加入了 6 类增强方向。

### 5.1 波动率 / 路径过滤

- `vol_filter_down_clean`
- `vol_filter_down_midrange`
- `vol_filter_up_clean`

共同特征：

- 不只看净涨跌幅
- 还要求路径更“干净”：
  - `path_efficiency_2m` 高
  - `path_choppiness_2m` 低

### 5.2 盘口压力 / 价格冲击

- `pressure_down_confirm`
- `pressure_down_strong`
- `pressure_up_confirm`

共同特征：

- 不是只看涨跌
- 还看 `pressure_imbalance_2m`
- 结合价格和可成交 size 一起判断方向压力

### 5.3 Regime filter

- `regime_trend_down`
- `regime_trend_down_price`
- `regime_trend_up`
- `regime_neutral_value_up`

这里会先把市场状态分成：

- `trend_down`
- `trend_up`
- `choppy`
- `neutral`

然后只在指定 regime 下启用对应策略。

### 5.4 Rolling 健康度过滤

- `rolling_pnl_filter_down`
- `rolling_win_filter_down`
- `rolling_sharpe_filter_down`

共同特征：

- 基线还是 `drop10_down`
- 但要看最近 20 笔是否仍然健康
  - 平均 PnL 是否足够高
  - 胜率是否足够高
  - Sharpe 代理是否为正

### 5.5 分层仓位

- `tiered_down_score_signal`
- `tiered_score`

逻辑：

- 先给当前 Down 方向打分 `down_score_ext`
- 分数越高，仓位越大
  - 低分：10%
  - 中分：15% / 20%
  - 高分：25%

### 5.6 价格区间搜索

- `price_interval_down_60_80`
- `price_interval_down_65_80`
- `price_interval_down_55_75`
- `price_interval_up_45_65`

直觉：

- 不一定是“胜率最高”的地方最好
- 更可能是“赔率仍然合适”的价格区间最好

---

## 6. latest regime 最新状态切换层

这层是针对最新数据里出现的“状态切换”专门做的分钟感知策略。

### 6.1 latest_m1_drop20_down

- 条件：第 1 分钟 `move_m1 <= -20`
- 行为：买 Down
- 直觉：早期大跌在最新数据里仍然可能有延续

### 6.2 latest_m2_sharpdrop_reversal_up

- 条件：第 2 分钟 `-50 < move_m2 <= -30`
- 行为：买 Up
- 直觉：中等偏大下跌在最新数据里更像反弹区间

### 6.3 latest_m2_milddrop_down

- 条件：第 2 分钟 `-30 < move_m2 <= -10`
- 行为：买 Down
- 直觉：较温和的下跌仍然可能延续

### 6.4 latest_m2_neutral_down

- 条件：第 2 分钟 `-10 < move_m2 <= 10`
- 行为：买 Down
- 直觉：接近平盘状态下，最新数据里 Down 偏 value

### 6.5 latest_m2_extremeup_fade_down

- 条件：第 2 分钟 `move_m2 >= 50`
- 行为：买 Down
- 直觉：过度上涨后做反向 fade

### 6.6 latest_m4_rise30to50_up

- 条件：第 4 分钟 `30 < move_m4 <= 50`
- 行为：买 Up
- 直觉：上涨突破在更晚入场时更有效

### 6.7 latest_switch_v1

按分钟和状态切换：

- 若第 1 分钟大跌且 `buy_down_price_m1 <= 0.90`，买 Down
- 否则若第 2 分钟中等大跌且 `buy_up_price_m2 <= 0.40`，买 Up
- 否则若第 2 分钟温和下跌且 `buy_down_price_m2 <= 0.75`，买 Down
- 否则若第 2 分钟极端上涨且 `buy_down_price_m2 <= 0.20`，买 Down
- 否则若第 4 分钟上涨突破且 `buy_up_price_m4 <= 0.90`，买 Up
- 否则跳过

**直觉：** 这是“把不同分钟的局部 edge 拼接起来”的组合策略。

### 6.8 latest_switch_v2

和 `latest_switch_v1` 类似，但更偏保守：

- 更优先做第 2 分钟 sharp-drop reversal
- 对早期 down continuation 的价格要求更严格
- 仍保留第 4 分钟 breakout up 和第 2 分钟 extreme-up fade

---

## 7. 不同策略层的关系

可以把 5 层理解成：

- **bankroll 基础层**：找最简单且有效的主线
- **optimizer 层**：围绕最强主线做细过滤
- **classic 层**：把常见量化思想系统化测试
- **extended classic 层**：把更细的价量路径、regime、rolling filter、分层仓位全部补齐
- **latest regime 层**：当旧主线失效时，针对新数据结构直接做分钟感知策略

它们不是互相替代，而是层层加细。

---

## 8. 怎么读结果

建议优先同时看这几个指标：

1. `ending_bankroll`
2. `total_return`
3. `max_drawdown`
4. `trades`
5. `avg_trade_return_on_cost`

因为：

- 只看收益率，容易被少量交易误导
- 只看回撤，又可能错过真正有 edge 的主线
- 最终还是要看“收益、风险、交易次数”是否平衡

---

## 9. 当前一句话总结

旧数据里最稳定的一条主线曾经是：

- **前 2 分钟明显下跌后，继续做 Down**

但最新数据里，这条主线已经明显衰减，因此新增的 `latest regime` 层是在回答：

- 现在是不是更应该：
  - 早期大跌追随
  - 第 2 分钟 sharp drop 做反弹
  - 第 4 分钟中等上涨再追突破
  - 并在这些局部 edge 之间做状态切换

# 经典策略说明文档

这份文档解释 `classic_strategies_report.md` 里各个策略到底在**什么时候交易、买哪边、怎么定仓位**。

对应脚本：

- `analysis/classic_run_24869603988_strategies.py`

---

## 1. 回测统一规则

所有经典策略都共享同一套执行假设：

1. 每个 5 分钟事件只在 **第 2 分钟** 看一次信号。
2. 如果信号触发：
   - 买 Up 用 `buy_up_price_2m`
   - 买 Down 用 `buy_down_price_2m`
3. 单笔最大可买份数受 top-of-book size 限制：
   - 买 Up 用 `buy_up_size_2m`
   - 买 Down 用 `buy_down_size_2m`
4. 买入后一直持有到该 5 分钟事件结束，再按最终 `Up/Down` 结算。
5. 每份合约都额外扣一个固定成本 `fee`（当前默认 0.01）。

### 单笔收益

- 买 Up：
  - 若最终 outcome 是 Up，则每份收益 = `1 - buy_up_price_2m - fee`
  - 否则每份收益 = `- buy_up_price_2m - fee`
- 买 Down：
  - 若最终 outcome 是 Down，则每份收益 = `1 - buy_down_price_2m - fee`
  - 否则每份收益 = `- buy_down_price_2m - fee`

---

## 2. 基础字段解释

### 价格类

- `btc_move_2m`：从该 5 分钟窗口开始，到第 2 分钟时 BTC 价格总共变动了多少美元。
- `buy_up_price_2m`：第 2 分钟时买 Up 的价格（美元/份，已经除以 100）。
- `buy_down_price_2m`：第 2 分钟时买 Down 的价格。

### 量/盘口类

- `buy_up_size_2m`：第 2 分钟时，以最优买入价买 Up 最多可成交的份数。
- `buy_down_size_2m`：第 2 分钟时，以最优买入价买 Down 最多可成交的份数。
- `sell_up_size_2m` / `sell_down_size_2m`：对侧挂单快照。
- `size_imbalance_updown_2m`：Up / Down 两边盘口 size 的不平衡程度。
  - 越负：越偏向 Down
  - 越正：越偏向 Up
- `depth_imbalance_updown_2m`：深度不平衡程度。

### 离散化标签

脚本还构造了几个离散标签，供动态策略使用：

- `move_bucket`：把 `btc_move_2m` 分成区间
- `up_price_bucket` / `down_price_bucket`：把买入价格分桶
- `size_sign`：
  - `neg`：`size_imbalance_updown_2m <= -0.1`
  - `pos`：`size_imbalance_updown_2m >= 0.1`
  - `neu`：其余情况
- `liq_sign_down` / `liq_sign_up`：根据 `buy_*_size_2m` 把流动性分成 `low / mid / high`

---

## 3. 静态经典策略（static rules）

这些策略只看当前这根 5 分钟事件第 2 分钟时的信号，不看过去策略是否赚钱。

### 3.1 classic_trend_down_basic

**规则：**

- 如果 `btc_move_2m <= -10`，则买 Down
- 否则跳过

**直觉：**

- 这是最简单的“跌了继续跌”的短周期动量策略
- 它和主报告里的 `rule_drop10_down` 本质上非常接近

---

### 3.2 classic_trend_down_score3

先定义一个 Down 方向确认分数：

- `btc_move_2m <= -10`
- `buy_down_price_2m <= 0.80`
- `size_imbalance_updown_2m <= 0`
- `buy_down_size_2m >= 250`
- `depth_imbalance_updown_2m <= 0`

满足一条记 1 分。

**规则：**

- 如果 `btc_move_2m <= -10` 且上述总分 `>= 3`，则买 Down
- 否则跳过

**直觉：**

- 不仅要“价格下跌”，还要“价格别太贵、盘口也偏向 Down、流动性够深”

---

### 3.3 classic_trend_down_score4

和 `score3` 一样，但要求总分 `>= 4`。

**直觉：**

- 更严格，交易更少，但希望质量更高。

---

### 3.4 classic_trend_down_midrange

**规则：**

- 如果 `-50 < btc_move_2m <= -10`，则买 Down
- 否则跳过

**直觉：**

- 只做“中等幅度下跌”后的动量
- 避免过度极端下跌可能带来的反抽或高价格问题

---

### 3.5 classic_trend_down_liq

**规则：**

- 如果 `btc_move_2m <= -10` 且 `buy_down_size_2m >= 250`，则买 Down
- 否则跳过

**直觉：**

- 只有在 Down 侧流动性够深时才做，避免薄盘口噪声

---

### 3.6 classic_trend_down_price

**规则：**

- 如果 `btc_move_2m <= -10` 且 `buy_down_price_2m <= 0.80`，则买 Down
- 否则跳过

**直觉：**

- 如果 Down 已经太贵了，胜率再高也可能不划算

---

### 3.7 classic_trend_down_combo

**规则：**

- 如果同时满足：
  - `btc_move_2m <= -10`
  - `buy_down_price_2m <= 0.80`
  - `size_imbalance_updown_2m <= 0`
  - `buy_down_size_2m >= 250`
- 则买 Down
- 否则跳过

**直觉：**

- 这是一个“趋势 + 价格 + 盘口偏向 + 流动性”四重确认版

---

### 3.8 classic_trend_up_basic

**规则：**

- 如果 `30 < btc_move_2m <= 50`，则买 Up
- 否则跳过

**直觉：**

- 这是针对上涨侧的中等动量区间
- 因为你前面的研究已经发现：不是所有上涨都值得追，只有某个中等区间比较可能有效

---

### 3.9 classic_trend_up_score3

先定义一个 Up 方向确认分数：

- `30 < btc_move_2m <= 50`
- `buy_up_price_2m <= 0.80`
- `size_imbalance_updown_2m >= 0`
- `buy_up_size_2m >= 250`
- `depth_imbalance_updown_2m >= 0`

**规则：**

- 如果上述分数 `>= 3`，则买 Up
- 否则跳过

---

### 3.10 classic_neutral_value_up

**规则：**

- 如果同时满足：
  - `-10 < btc_move_2m <= 10`
  - `buy_up_price_2m <= 0.55`
  - `size_imbalance_updown_2m >= 0`
- 则买 Up
- 否则跳过

**直觉：**

- 当价格几乎没怎么走，但 Up 价格仍不高，且盘口偏向 Up，尝试做一个偏 value 的买 Up 策略

---

## 4. 动态经典策略（dynamic rules）

这些策略不是只看当前快照，而是会在 walk-forward 框架里利用过去样本估计条件概率 `q_hat`。

也就是说：

- 每走到一个新事件时
- 只允许看之前已经发生过的事件
- 用历史数据估计当前状态下，最终 Up/Down 的经验概率

### 4.1 value_down_margin2

**规则：**

- 先估计当前状态下 `buy_down` 的经验胜率 `q_hat`
- 如果：
  - `btc_move_2m <= -10`
  - `q_hat > buy_down_price_2m + 0.02`
- 则买 Down
- 否则跳过

**直觉：**

- 这是最典型的“fair value > market price”价值策略

---

### 4.2 value_down_margin5

和上面一样，但要求更严格：

- `q_hat > buy_down_price_2m + 0.05`

**直觉：**

- 只做明显便宜的机会

---

### 4.3 value_down_margin2_book

**规则：**

- 要同时满足：
  - `btc_move_2m <= -10`
  - `q_hat > buy_down_price_2m + 0.02`
  - `size_imbalance_updown_2m <= 0`
  - `buy_down_size_2m >= 250`
- 才买 Down

**直觉：**

- 这是 value 策略再加盘口过滤器的版本

---

### 4.4 value_up_margin2

**规则：**

- 如果：
  - `30 < btc_move_2m <= 50`
  - `q_hat_up > buy_up_price_2m + 0.02`
- 则买 Up
- 否则跳过

**直觉：**

- 上涨侧的 value 版本

---

### 4.5 rolling_rule_drop10_down

**规则：**

- 基础信号仍然是：`btc_move_2m <= -10`
- 但还要求这条规则最近 20 笔（至少有 8 笔历史）平均每份 PnL 仍然 > 0.05
- 满足才继续买 Down

**直觉：**

- 这是一个最简单的“近期失效就停手”的 regime filter

---

## 5. 仓位怎么用

当前经典策略会测试这些仓位：

### fixed_10pct / fixed_15pct / fixed_20pct / fixed_25pct

- 每次交易用当前本金的固定比例下注
- 例如 `fixed_25pct`：每次最多用当前本金的 25%
- 但仍然受盘口可成交 size 限制

### full_kelly_capped20 / half_kelly_capped20 / quarter_kelly_capped20

- 先用经验概率 `q_hat` 和入场价 `price` 算凯利比例
- 再乘以 1 / 0.5 / 0.25
- 最后把单笔上限 cap 在 20% 本金

这样做的原因是：

- 纯 Kelly 在小样本、短周期里波动太大
- 加 cap 之后更接近实际可执行的风险控制

---

## 6. 为什么当前最佳是 classic_trend_down_basic + fixed_25pct

你当前看到这版报告里，最佳经典策略是：

- `classic_trend_down_basic`
- `fixed_25pct`

它本质上说明：

- “前 2 分钟跌超 10 美元就做 Down” 这个简单规则非常强
- 到目前为止，很多更复杂的价格/盘口过滤，并没有稳定地提高收益
- 这通常意味着：
  - 原始信号已经很强
  - 样本量还不够支撑过度细分
  - 复杂过滤容易让交易次数减少太多

---

## 7. 下一批建议测试的经典策略

下面这些是我认为下一步最值得测试的方向：

### 7.1 波动率过滤（volatility filter）

思路：

- 不只看 2 分钟净涨跌幅
- 还看前 2 分钟内部波动是否过大

例子：

- `drop10_down` 只在“单边下跌而不是剧烈来回波动”时做

这可能帮助区分：

- 真动量
- 假突破 / 拉锯噪声

---

### 7.2 价格冲击 / 盘口吃单压力（impact / pressure）

思路：

- 用 `buy_*_size` 和价格一起构造“压力”指标
- 比如：
  - `down_pressure = buy_down_price_2m * buy_down_size_2m`
  - `up_pressure = buy_up_price_2m * buy_up_size_2m`

如果 Down 方向价格不贵、同时深度又够，可能代表更健康的趋势继续。

---

### 7.3 多状态 regime filter

思路：

- 先判断当前市场更像哪种 regime：
  - 强趋势
  - 中性震荡
  - 极端单边
- 再决定该用哪条规则

这比单一规则更接近传统 CTA / stat-arb 的做法。

---

### 7.4 持续有效性过滤（rolling Sharpe / rolling win rate）

当前只做了一个很简单的 rolling PnL filter。

可以继续升级成：

- 最近 20 笔胜率是否高于阈值
- 最近 20 笔平均单位收益是否高于阈值
- 最近 20 笔 Sharpe 是否为正

这样比只看平均 PnL 更稳一些。

---

### 7.5 分层仓位（position scaling）

当前固定仓位已经很强，但还可以更细：

- `drop10_down` 用 15%
- `drop20_down` 用 20%
- `drop10_down + 盘口确认` 用 25%

也就是：

- 越高确信度，给越高仓位
- 不再是“一刀切固定 25%”

---

### 7.6 极端价格避免（avoid too-expensive entries）

目前很多规则里只试了 `price <= 0.80` 这样的阈值。

还可以更系统化：

- Down 价过于接近 1 时，即使胜率高，也可能收益率不够
- Up 价过于接近 1 时也是一样

值得做一个：

- 最优价格区间搜索

---

## 8. 建议怎么读这份报告

如果你要快速判断一条策略值不值得继续保留，优先看：

1. `ending_bankroll`
2. `total_return`
3. `max_drawdown`
4. `trades`

其中最关键的是：

- 不要只看收益率
- 要同时看交易次数和回撤

因为短样本下：

- 交易次数太少的策略，容易看起来“很神”但不稳
- 回撤太大的策略，实盘拿不住

---

## 9. 当前一句话总结

到目前为止，最强的主线依然是：

- **前 2 分钟明显下跌后，继续做 Down**

而经典策略层主要是在回答：

- 要不要加盘口过滤？
- 要不要加 value 条件？
- 要不要做近期有效性过滤？
- 用 10/15/20/25% 哪个固定仓位更合适？

这份文档的目的，就是让你以后看到策略名时，能立刻知道它到底在做什么。 

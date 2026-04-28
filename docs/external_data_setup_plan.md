# 外部数据补充方案（优先级、获取方式、成本与接入计划）

这份文档的目标是：

1. 明确哪些外部数据最值得补
2. 哪些我可以先在 repo 里把框架搭好
3. 哪些需要你去申请一个 API key 再发给我
4. 哪些不建议用 LLM 代替

> 说明：这份文档基于当前策略研究框架整理，但**我现在无法在线核对最新官网价格和套餐变动**。因此这里的“免费 / 低成本”判断是工程优先级建议，不应视为实时价格承诺。

---

## 1. 最值得补的数据，按优先级排序

### P0：最高优先级

1. **Chainlink 对齐标签 / 历史 report**
2. **Polymarket 自己的盘口 / 成交 / 市场价格历史**
3. **Binance perpetual futures 微观结构**

### P1：强烈建议补

4. **Deribit 衍生品状态（IV / basis / OI）**
5. **宏观事件日历**

### P2：增强层

6. **DeepSeek（或其他 LLM）事件语义打分**

---

## 2. 每个外部源的作用和推荐获取方式

## 2.1 Chainlink 对齐标签 / 历史 report

### 为什么关键

Polymarket 这个 BTC 5 分钟市场的结算参考不是“任意现货价格”，而是特定参考源。若标签源和真实结算参考错配，策略很容易失真。

### 最想拿到的内容

- 5 分钟事件起点 / 终点对应的 Chainlink 参考值
- 对应时间点的历史 report 或可对齐的价格序列
- 如果可能，再要：bid / ask、波动率、流动性字段

### 现实判断

这是**最关键但也最难补齐**的一块。公开历史数据未必像交易所 K 线那样容易直接拿全。

### 你可以怎么帮我

- 如果你能找到：
  - 官方历史 API / 历史 dump
  - 你自己已有的存档
  - 或者任何可以稳定复现历史 report 的方式
- 直接把 access 信息或文件路径发给我

### 成本判断

- **未知 / 需你确认**
- 这块不能默认认为免费可大规模回溯

### 在 repo 里的接入优先级

- **最高**

---

## 2.2 Polymarket 自己的盘口 / 成交 / 价格历史

### 为什么关键

你交易的是 Polymarket 的概率市场，而不是 BTC 本身。即使 BTC 有信号，市场也可能早就 price in 了。

### 最想拿到的内容

- best bid / ask
- spread
- top-of-book size
- orderbook depth
- 历史成交 / trade prints
- 历史 price history
- 尽量细的市场更新流

### 优先级理由

这是最直接决定“有没有 edge”的地方。

### 成本判断

- 通常应优先尝试**公开接口**
- 很多市场数据类接口往往不一定要 key，但具体限制需你确认

### 你可以怎么帮我

- 如果官网需要登录或 token，给我对应 API key / token / cookie 方案
- 如果不用 key，只要告诉我你确认可用的接口文档链接或导出文件方式即可

### 在 repo 里的接入优先级

- **极高**

---

## 2.3 Binance perpetual futures 微观结构

### 为什么关键

5 分钟级别里，perp 的 OI、主动买卖差、清算流和深度变化，通常比简单的前 2 分钟涨跌更有信息量。

### 最想拿到的内容

- Open interest
- OI change
- taker buy / sell volume
- agg trades
- order book depth
- liquidation imbalance
- basis / mark / index relationship

### 最值得先做的特征

- `dOI_30s`, `dOI_60s`
- `taker_buy_sell_ratio`
- `liquidation_long_short_imbalance`
- `depth_imbalance`
- `basis_perp_vs_spot`

### 成本判断

- 常见市场数据一般优先尝试**公开接口或免费 API key**
- 如果需要账号，一般也是最低成本就能开始采集

### 在 repo 里的接入优先级

- **极高**

---

## 2.4 Deribit 衍生品状态

### 为什么关键

Deribit 更适合作为**风险状态 / regime filter**，而不是单独当 5 分钟方向预测器。

### 最想拿到的内容

- near-ATM IV
- futures / perp basis
- OI
- best bid / ask
- index / mark / last

### 成本判断

- 通常应先尝试**公开市场数据接口**

### 在 repo 里的接入优先级

- **高**

---

## 2.5 宏观事件日历

### 为什么关键

你现在最容易踩的坑之一，就是把“平静时段”和“CPI / FOMC / NFP 等事件时段”混着学。5 分钟策略对这个很敏感。

### 最想拿到的内容

- 事件时间
- 事件等级（高 / 中 / 低）
- 事件类别（CPI / NFP / FOMC / PCE / 初请 / PMI 等）
- 前后 15 分钟布尔变量

### 最简单可用特征

- `has_major_macro_within_15m`
- `had_major_macro_last_15m`
- `macro_event_type`

### 成本判断

- 这块最可能需要一个 API key
- 如果能找到低价或免费层，是很值得投入的

### 在 repo 里的接入优先级

- **高**

---

## 2.6 DeepSeek（或其他 LLM）事件语义打分

### 为什么它不是第一优先级

LLM 擅长的是：

- 新闻摘要
- 宏观事件文本偏利多 / 利空评分
- 风险偏好 regime 的文字标签

LLM **不适合代替**：

- Order flow
- OI
- liquidation
- bid / ask
- 真实参考价格标签

### 适合的用法

- 事件语义分数
- 重大新闻重要性评级
- 风险偏好标签
- regime 文本描述

### 成本判断

- 如果你已经愿意提供 API，这块适合作为**增强层**，而不是底层标签/微观结构替代品

### 在 repo 里的接入优先级

- **中等**

---

## 3. 你最值得优先去申请 / 提供的东西

按我建议的优先级：

### 第一梯队

1. **Chainlink 历史对齐数据 access**
2. **宏观事件日历 API key**

### 第二梯队

3. **如果 Polymarket 需要专门 token / key，就提供它**
4. **如果 Binance 某些衍生品接口需要 key，就提供它**
5. **Deribit 如需 key，也可以补**

### 第三梯队

6. **DeepSeek API key**

---

## 4. 如果你不想自己判断，我建议你这么做

### 方案 A：最小可推进版本

你只给我：

- 一个**宏观事件日历 API key**
- 一个**DeepSeek API key**（可选）

我就能先把：

- 宏观事件 regime
- 事件文本打分层

补进现有框架。

### 方案 B：更正确的版本

你优先给我：

- **Chainlink 历史对齐 access**
- **宏观事件日历 key**

然后我再把：

- fair probability
- regime gate
- 参考源对齐标签

往前推一大步。

### 方案 C：最完整版本

你补齐：

- Chainlink
- Polymarket 微观结构
- Binance perp 微观结构
- Deribit 状态
- 宏观事件日历
- DeepSeek

这时候整个框架就比较接近完整了。

---

## 5. repo 里我会怎么接

我会按这套结构往前推：

- `config/external_data.env.example`：需要哪些 key / token
- `analysis/external_provider_registry.py`：每个 provider 的字段、优先级、用途
- `analysis/merge_optional_external_features.py`：把外部源对齐到事件级特征
- `docs/external_data_setup_plan.md`：你现在看到的这份说明

后续如果你给了 key，我会继续补：

- provider adapter
- feature merge
- 新版 selected strategy / fair probability / robustness 报告

---

## 6. 一句话结论

如果你不希望关键环节缺失，最不能缺的是：

1. **真实参考标签（最好是 Chainlink）**
2. **Polymarket 自己的盘口 / 成交**
3. **Binance perp 的微观结构**

而 DeepSeek 最适合补的是：

- **事件语义层**

它重要，但不是底层替代品。 

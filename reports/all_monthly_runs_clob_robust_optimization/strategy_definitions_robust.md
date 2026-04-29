# all_monthly_runs_clob_robust_optimization 策略定义

这份文档专门解释 `all_monthly_clob_robust_optimization.py` 这一层：它不是最终上线版本，而是**为 final v1 提供中间筛选和结构验证**的“策略实验场”。

---

## 1. 这一层解决的核心问题

在更早的系统性回测里，我们已经发现：

- `static_m2_milddrop_down_book` 收益高，但回撤偏大
- `static_m4_breakout_up_tight` 很稳，但交易覆盖不够
- 单条策略要么太猛、要么太稀

所以这层的目标不是“找一个单点最优信号”，而是测试 4 类更贴近实盘的问题：

1. **按时段切分后，信号是否更稳定**
2. **如果以 12 小时窗口体验来评价，哪些策略更靠谱**
3. **低杠杆 / adaptive sizing 是否能改善体验**
4. **spread / liquidity / overround 三重过滤是否能减少坏交易**

---

## 2. Session 定义（America/New_York）

- `asia`：19:00–02:00
- `london`：02:00–09:30
- `us_open`：09:30–12:00
- `us_afternoon`：12:00–16:00
- 其余：`other`

### 为什么这样切

这不是随便按时钟切，而是想让不同的微观结构环境分开：

- Asia：更容易出现低噪声、慢扩散 breakout
- London：更容易出现顺着盘口结构延续的 milddrop
- US open：噪声大、波动强、需要更谨慎
- US afternoon：有些局部信号更干净，但交易数通常少

这层是为了验证：

> **alpha 不是全天均匀分布，而是 session-dependent。**

---

## 3. 这一层复用了哪些底层原型

### 3.1 breakout 原型
来源：`static_m4_breakout_up_tight`

- 核心：第 4 分钟涨幅在一个“可追但不极端”的区间
- 附加条件：
  - Up 价格不能太贵
  - Up 侧流动性要够

### 3.2 milddrop 原型
来源：`static_m2_milddrop_down_book`

- 核心：第 2 分钟温和下跌
- 附加条件：
  - size imbalance 不支持反转
  - Down 侧盘口压力没明显变差

### 为什么选这两个

因为在前一层的全历史回测里：

- milddrop 是高收益骨架
- breakout 是低回撤骨架

这一层本质上是在测试：

- 它们分别在什么 session 下更好
- 能不能通过组合与过滤，让收益/风险变得更平衡

---

## 4. 三重过滤（triple gate）到底是什么

## 4.1 对 Down 信号

`triple_gate_down` 同时要求：

- `spread_down_median_first2m <= 0.05`
- `overround_median_first2m <= 0.04`
- `buy_down_size_2m >= 150`

## 4.2 对 Up 信号

`triple_gate_up` 同时要求：

- `spread_up_median_first2m <= 0.05`
- `overround_median_first2m <= 0.04`
- `buy_up_size_4m >= 120`

## 4.3 为什么要这样做

这不是为了提升“方向预测能力”，而是为了提高：

- 可成交性
- 实际赔率质量
- edge 能否穿透交易摩擦

### 行为与微观结构直觉

很多看起来对的短周期信号，最后实盘亏钱，不是因为方向错，而是因为：

- spread 太宽
- overround 太高
- 流动性太薄

也就是**你买到的价格已经把信号吃掉了**。
triple gate 的本质是：

> **只在“信号没坏、价格也没坏”的地方交易。**

---

## 5. quality score 是什么

这层把一些“交易质量”压成了两个分数：

### 5.1 `quality_breakout`
由这些部分组成：

- breakout 流动性
- breakout spread
- breakout overround
- breakout 的价格位置是否接近理想突破区间

### 5.2 `quality_milddrop`
由这些部分组成：

- Down 流动性
- Down spread
- overround
- milddrop 的盘口一致性（size imbalance + book pressure）

### 它为什么重要

因为现实里 setup 不是二元的：

- 有的 breakout 很“干净”
- 有的 milddrop 只是勉强满足条件

quality score 的作用是把“信号强弱”显式化，为 adaptive sizing 和更精细筛选服务。

---

## 6. 这一层的策略家族

## A. `session_breakout_<session>_fixed_<N>pct`

### 做法

- 只在指定 session 测 breakout
- 固定仓位 `N%`

### 它要回答什么问题

- breakout 到底在哪个时段最有效？
- 同样的 breakout，放到不同 session 会不会完全不同？

### 结果意义

它主要是一个“拆时段验证器”。

---

## B. `session_milddrop_<session>_fixed_<N>pct`

### 做法

- 只在指定 session 测 milddrop
- 固定仓位 `N%`

### 它要回答什么问题

- milddrop continuation 是否也明显依赖 session？
- 哪个时段适合把它当主体，哪个时段根本不该做？

### 结果意义

这是后续构造组合策略时最关键的输入之一。

---

## C. `gate_breakout_fixed_<N>pct`

### 做法

- breakout 原型
- 再加 triple gate
- 固定仓位

### 它要回答什么问题

- 如果只保留“价格不错、流动性不错、overround 不差”的 breakout，体验会不会明显改善？

### 行为直觉

这是在验证：

> **breakout 不是不能追，而是只能追“盘口没把你吃掉”的 breakout。**

---

## D. `gate_milddrop_fixed_<N>pct`

### 做法

- milddrop 原型
- 再加 triple gate
- 固定仓位

### 它要回答什么问题

- milddrop 最大的问题到底是“方向本身不稳”，还是“便宜/流动性差时做进去会被磨死”？

### 结果意义

这是把 milddrop 从“粗规则”推向“可交易规则”的必要一步。

---

## E. `combo_guarded_fixed_<N>pct`

### 做法

- US 时段优先做 breakout
- Asia/London 时段优先做 milddrop
- 固定仓位
- 叠加：
  - trade quota
  - 连亏后 cooldown

### 它为什么存在

这是第一次把“局部有效片段”拼在一起。

### 风险控制逻辑

- trade quota：防止 12 小时窗口里过度出手
- cooldown：把连续亏损当成 regime 切换警报

### 行为直觉

这是承认：

- 不同时段的有效行为偏差不一样
- 而短周期策略最怕在坏 regime 里连续重复犯错

---

## F. `combo_guarded_adaptive`

### 做法

- 结构与 `combo_guarded_fixed` 一样
- 但仓位随 quality score 在大约 `4%–12%` 之间变化

### 它要回答什么问题

- 好 setup 多做一点，差 setup 少做一点，能否改善 12 小时体验？

### 行为直觉

这相当于把“主观盘感”量化：

- 好盘面上大一点
- 一般盘面上小一点

---

## G. `combo_session_mix_fixed_<N>pct`

### 做法

- 更强地按 session 分工
- `us_open` 只做 breakout
- `asia/london` 只做高质量 milddrop
- 固定仓位

### 它为什么存在

这是为了测试一个更极端的假设：

> **也许全天只需要让每个 session 做它最擅长的那一件事。**

---

## H. `combo_session_mix_adaptive`

### 做法

- 与 `combo_session_mix_fixed` 相同
- 仓位按 quality score 自适应，约 `4%–12%`

### 它要回答什么问题

- 如果 session 已经切得很细，再加 adaptive sizing 会不会进一步改善风险/收益平衡？

---

## 7. 这层为什么要用 12 小时窗口指标

这层额外计算：

- `worst_48_window_return`
- `median_48_window_return`
- `pct_positive_48_windows`
- `pct_nonnegative_48_windows`

### 为什么重要

终值好看不等于体验好。

你真正关心的是：

- 随便从某个时点开始交易
- 接下来半天会不会遇到很差的体验

所以这层不只是研究“赚不赚钱”，而是研究：

> **这个策略在局部时间窗口里是否也能拿得住。**

---

## 8. 这一层和 final v1 的关系

你可以把这层理解成：

- **不是最终版**
- 而是 final v1 之前的“结构搜索层”

它做的工作是：

1. 找出哪些 session 子策略值得保留
2. 找出哪些 triple gate 有帮助
3. 找出 fixed vs adaptive 的差异
4. 看 trade quota / cooldown 是否有价值
5. 为下一层真正的 v1 组合提供骨架

也就是说，后面的 `v1_active_fill_mix`、`v1_balanced_mix` 这些，并不是凭空出现的，而是从这一层筛出来的。

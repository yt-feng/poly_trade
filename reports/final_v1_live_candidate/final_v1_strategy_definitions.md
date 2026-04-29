# final_v1_live_candidate_search 策略定义

## 目标

这一层专门针对更接近实盘的目标函数：

- 最大回撤 < 30%
- 从任意起点开始，未来 36 小时（144 个潜在窗口）收益尽量 > 10%
- 同时允许不同 session 的子策略混合

## 候选组合

### 1. `v1_conservative_mix`
- Asia：只做 breakout，必须 triple gate
- London：只做 milddrop，必须 triple gate，且 quality 更高
- US afternoon：只做高质量 milddrop
- 仓位偏低，trade quota 更紧

### 2. `v1_balanced_mix`
- 保留 Asia breakout、London milddrop、US afternoon milddrop
- 允许少量 breakout filler
- 比 conservative 覆盖更高

### 3. `v1_adaptive_mix`
- 与 conservative 结构类似
- 但仓位按 quality 自适应变化

### 4. `v1_active_fill_mix`
- 允许更多 session filler 交易
- 目标是提高 36 小时窗口覆盖度

### 5. `v1_logit_overlay_mix`
- 在 session mix 基础上再叠一层 rolling logistic edge 过滤
- 只有模型概率与盘口价格偏差足够大时才交易

# all_monthly_runs_clob_robust_optimization 策略定义

## 目标

这一层专门测试四个方向：

1. 时间分段版策略（按 ET session 切分）
2. 12小时滚动窗口最差收益约束
3. 仓位自适应 / 降杠杆组合策略
4. 只在 spread / liquidity / overround 同时达标时交易

## Session 定义（America/New_York）

- `asia`：19:00–02:00
- `london`：02:00–09:30
- `us_open`：09:30–12:00
- `us_afternoon`：12:00–16:00
- 其余：`other`

## 策略家族

### A. `session_breakout_<session>_fixed_<N>pct`

- 底层信号：`static_m4_breakout_up_tight`
- 只在指定 session 交易
- 固定仓位

### B. `session_milddrop_<session>_fixed_<N>pct`

- 底层信号：`static_m2_milddrop_down_book`
- 只在指定 session 交易
- 固定仓位

### C. `gate_breakout_fixed_<N>pct`

- breakout 信号 + triple gate
- triple gate = tight spread + low overround + adequate liquidity

### D. `gate_milddrop_fixed_<N>pct`

- milddrop 信号 + triple gate
- triple gate = tight spread + low overround + adequate liquidity

### E. `combo_guarded_fixed_<N>pct`

- US 时段优先做 breakout
- Asia/London 时段优先做 milddrop
- 使用交易配额和连亏后 cooldown 保护

### F. `combo_guarded_adaptive`

- 与 `combo_guarded_fixed` 相同
- 但仓位按 quality score 自适应，范围约 4%–12%

### G. `combo_session_mix_fixed_<N>pct`

- 只在 `us_open` 做 breakout
- 只在 `asia/london` 做高质量 milddrop
- 比一般组合更强调 session 切分

### H. `combo_session_mix_adaptive`

- 与 `combo_session_mix_fixed` 相同
- 但仓位按 quality score 自适应，范围约 4%–12%

## 12小时窗口指标

每条策略都会额外评估：

- `worst_48_window_return`
- `median_48_window_return`
- `pct_positive_48_windows`
- `pct_nonnegative_48_windows`

因为你要求的不只是终值高，而是：

- 最大回撤低于 30%
- 从任意起点开始，未来 12 小时表现尽量不要太差

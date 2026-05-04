# monthly_runs full refresh v2 strategy definitions

这份刷新专门把 5 分钟 Polymarket 事件当成微观结构问题，而不是宏观 regime 问题。

## 三类策略直觉

### 经典量化策略
- `classic_milddrop_core`：温和下跌 + Down 侧盘口质量达标，做短线延续。
- `classic_breakout_core`：第4分钟确认突破，且 Up 侧价格/流动性仍可交易，做突破延续。
- `classic_early_drop_down`：第1分钟急跌，测试早期冲击是否继续扩散。
- `classic_sharpdrop_reversal`：较大跌幅后，如果 Up 仍便宜且有流动性，测试过度反应后的反弹。

### Polymarket 博弈/盘口策略
- `microstructure_book_consensus_down`：价格下跌、Down 侧盘口压力和交易活跃度一致，代表订单簿共识延续。
- `game_pm_overshoot_fade_up`：Polymarket Up 概率被快速压低，但 BTC 实际跌幅并不极端，测试 crowding/情绪挤压后的概率回摆。

### 时间序列策略
- `timeseries_momentum_up`：前2分钟路径效率高、Polymarket 概率同向上移，做短线动量延续。
- `timeseries_reversion_down`：前2分钟涨幅和波动都偏大，且 Down 赔率便宜，做短线均值回归。
- `v2_time_series_mix`：把动量、回归和高质量 milddrop 拼成组合。

## 重新精选目标

评分同时考虑：全历史收益、最大回撤、36小时最差窗口、72小时最差窗口、最近72小时收益、profit factor。

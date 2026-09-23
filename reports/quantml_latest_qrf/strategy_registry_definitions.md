# monthly_runs quant research framework strategy registry

每个策略都有 family、交易直觉和主要失效方式。

## v1_active_fill_mix
- family: `previous_v1`
- intuition: 旧版最优高覆盖组合；检验是否在新数据中仍然有效。
- likely failure mode: 覆盖率高，在坏盘口状态下会继续交易。

## v1_conservative_mix
- family: `previous_v1`
- intuition: 旧版保守组合；减少 filler 控制回撤。
- likely failure mode: 可能交易太少。

## v1_balanced_mix
- family: `previous_v1`
- intuition: 旧版平衡组合；收益与覆盖折中。
- likely failure mode: 可能受旧阈值影响。

## v1_adaptive_mix
- family: `previous_v1`
- intuition: 旧版按 quality 调仓。
- likely failure mode: quality 分数若失真，仓位仍可能不准。

## v1_logit_overlay_mix
- family: `previous_v1`
- intuition: 旧版概率模型过滤。
- likely failure mode: edge 阈值过严可能无交易。

## classic_milddrop_down
- family: `classic_quant`
- intuition: 温和下跌且盘口支持 Down，做短线延续。
- likely failure mode: 市场快速反弹时失效。

## classic_breakout_up
- family: `classic_quant`
- intuition: 第4分钟确认突破且 Up 未过贵，追随慢扩散。
- likely failure mode: 突破可能是最后一棒。

## classic_early_drop_down
- family: `classic_quant`
- intuition: 第1分钟急跌代表早期冲击，测试继续扩散。
- likely failure mode: 急跌后立刻均值回归。

## classic_sharpdrop_reversal_up
- family: `classic_quant`
- intuition: 较大跌幅后，Up 便宜且有流动性时做过度反应回摆。
- likely failure mode: 趋势下跌时持续亏。

## classic_extremeup_fade_down
- family: `classic_quant`
- intuition: 极端上涨后买便宜 Down，测试追涨拥挤回吐。
- likely failure mode: 强趋势日被持续挤压。

## micro_book_consensus_down
- family: `microstructure`
- intuition: 价格、订单簿压力和成交活跃度都指向 Down，做订单簿共识延续。
- likely failure mode: 盘口共识可能已被价格充分反映。

## micro_book_consensus_up
- family: `microstructure`
- intuition: 订单簿压力和价格路径都指向 Up，做盘口共识延续。
- likely failure mode: Up 价格太贵时 edge 被吃掉。

## game_pm_overshoot_fade_up
- family: `game_theory`
- intuition: Polymarket Up 概率被快速压低但 BTC 未同步崩，赌 crowding 后回摆。
- likely failure mode: PM 可能比 BTC 更早发现信息。

## game_pm_squeeze_fade_down
- family: `game_theory`
- intuition: Polymarket Up 概率快速上挤但 BTC 未跟随，赌拥挤追单回吐。
- likely failure mode: BTC 随后补涨。

## timeseries_momentum_up
- family: `time_series`
- intuition: 路径效率高且 PM 概率同向上移，做短线动量。
- likely failure mode: 最后几分钟反转。

## timeseries_momentum_down
- family: `time_series`
- intuition: 路径效率高且 PM 概率同向下移，做短线下行动量。
- likely failure mode: 流动性突然修复。

## timeseries_reversion_down
- family: `time_series`
- intuition: 大涨且 Down 便宜时做短线均值回归。
- likely failure mode: 趋势强时被挤压。

## ml_logit_edge_06
- family: `ml_value`
- intuition: 滚动逻辑回归估 fair probability，edge > 6% 才交易。
- likely failure mode: 模型边际不稳定。

## ml_logit_edge_10
- family: `ml_value`
- intuition: 更保守的滚动逻辑回归 edge 策略。
- likely failure mode: 交易太少。

## portfolio_micro_ts_mix
- family: `portfolio`
- intuition: 订单簿共识、时间序列动量和 PM 过度反应组合。
- likely failure mode: 多个弱信号相关性上升时一起失效。

## portfolio_conservative_v2
- family: `portfolio`
- intuition: 严格 gate 的 session-aware 组合，目标替代 v1_active_fill_mix。
- likely failure mode: 过度保守导致收益不足。

## anti-overfitting protocol
- 按时间顺序切分：train 50%，validation 25%，test 25%。
- 策略选择只看 validation score。
- test 是盲测，不参与策略选择。
- latest72h 只作为最新健康检查，不用于调参。
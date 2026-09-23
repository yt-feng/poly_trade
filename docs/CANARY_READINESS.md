# BTC 五分钟：研究到 canary 的准入流程

本模块是无交易权限的影子研究链路，不导入 live_execution、不读取钱包密钥、不发送订单。旧模型和实盘配置保持原样，不视为已修复或重新训练。新模块弃用完整前两分钟的前视特征，由当时已接收的前缀重算信号。旧 final_price > target_price 代理标签不得导入。

## 运行

```bash
python -m unittest discover -s tests -p 'test_canary*.py' -v
python analysis/canary_acquire.py
python analysis/canary_readiness.py --input canary_inputs/captured --output canary_results
python analysis/canary_shadow.py --input snapshots.jsonl --output shadow_decisions.jsonl
```

影子流可用 --input - 从标准输入读取，只有诊断输出，不会连接下单服务。GitHub Actions 的 canary-readiness-research 提供有下载预算的获取、测试与回放流程。没有定时任务或下单步骤。CI绿色表示代码、校验和与回放成功，不表示策略通过实盘门槛；readiness.json 是准入结果。

## 固定实验

五个假设、两类执行假设、15/30秒持有期、进出各1/3秒延迟，共40种配置。每个市场首次机会至多尝试一次，不在失败后重选。全量保留无信号、拒单、无成交、未知退出。

- roll：同源Chainlink BTC/USD现价减去60秒前现价，除以60，绝对值至少0.10美元/秒；只是等权均价机制近似，不声称重建官方TWAP。
- roll_veto：剔除Binance过去5秒对信号方向不利超过1bp的状态，等待期间恶化则延迟取消，不忽略取消生效前的代理成交。
- roll_confirm：进一步要求Binance、Coinbase同方向各至少0.5bp，Binance主动流失衡同向至少0.20。
- roll_conflict：现货不利超过1bp而TWAP滚出方向相反，作为探索对照。
- twap_trend：官方TWAP过去5秒变化至少0.5美元，作为简单趋势基线。

config/canary_research.json 是审计注册表；阈值固定在源码。修改需同步源码、测试、注册表，并重新锁定验证数据。

## 执行口径

固定5股结果合约，不是5美元；满足动态最小订单。虚拟风险台账每笔本金不超过5美元，一次一个头寸；已实现虚拟亏损达到2美元后停止新意图，未知退出盈亏立即停止。这不保证最大亏损只有2美元，单笔最坏损失可达全部本金。

主动入场按到达后收到的新ask，最多等2.5秒；入场最多比决策ask追一档，超限拒绝，不重选。退出同样按延迟后的新bid及5股容量检查。

被动入场是trade-through报价代理：延迟到达会穿价的post-only意图记拒绝，只有后来新ask至少低于原挂价一档且足量，才按原挂价记假设成交。未知真实队列、成交和撤单情况，不把其盈亏当作实盘证据；无代理成交也不等于真实市场一定无法成交。

使用观察到的逐时点费率，未知费用不默认为0；maker=0、taker按rate*p*(1-p)现金等值情景，返佣=0。真实净份额、扣费货币和舍入须核对私有成交记录。一档压力情景额外恶化退出bid。

缺失退出保留全额入场成本损失下界。条件平均仅针对可估值尝试，同时列尝试数、代理成交数和未知退出。独立窗口少于30不输出置信区间。

## 标签与验证

只接受归档market_resolved消息：condition_id、两个token、winning_asset_id、winning_outcome和收盘后接收时间匹配，冲突剔除。标签永不进入实时特征。官方起始价缺失时，按起始价估值和持有到期策略继续阻断。

config/canary_shortlist_lock.json 明示第一批四窗口探索后选择的30秒roll_veto主动候选；这是事后筛选，不是原先验主策略。后续仅统计起点达到锁定阈值、且之前未看过的窗口。

建议研究门槛：至少300个独立窗口、跨7个UTC日期、至少100个有执行证据的尝试；独立锁定留出；扣除成本后区间下界为正，3秒延迟和额外一档压力仍为正；99%以上退出可核对；私有订单、成交、撤单、费用完整对账。数字是研究规则，不是盈利保证。只有公共报价数据时不自动放行；本包始终没有真实下单入口。

## 官方协议依据

- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/market-data/chainlink-twap
- https://docs.polymarket.com/trading/place-orders

不依赖付费数据、地域绕行或私人账户访问；私有交易日志不应进入公开仓库。

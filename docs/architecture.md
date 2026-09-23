# Polymarket BTC 5 分钟研究与执行架构

这份文档描述本仓库当前已经落地的研究、验证、模拟执行和显式实盘适配路径。它记录的是代码现状，不把研究结果等同于已经验证的实盘收益。

## 总体链路

```text
历史 quote / monthly_runs
        │
        ▼
数据清洗与特征构建
        │
        ▼
策略研究与因子搜索
        │
        ▼
train → validation → test / walk-forward
        │
        ▼
q_model → edge → execution gates → sizing
        │                         │
        │                         ├─ paper：延迟成交与结算模拟
        │                         ├─ plan：只生成订单计划
        │                         └─ live：显式调用 CLOB V2 SDK
        ▼
reports/：报告、指标、图表、状态与 dashboard
```

## 组件分层

| 层 | 主要入口 | 职责 |
|---|---|---|
| 数据 | `analysis/fetch_and_merge_source_run.py`、`analysis/build_run_24869603988_dataset.py` | 获取/合并来源 run，清洗 quote path，并构建 5 分钟市场级特征。 |
| 基础研究 | `analysis/*research*.py`、`analysis/*strategy*.py` | 评估 classic、time-series、game-theory、microstructure、ML 和组合策略。 |
| 系统研究 | `analysis/robust_trading_system_tool.py` | 统一 `q → edge → execution gate → sizing → validation/test`，并输出压力测试、稳定性和纸面回放。 |
| 选择与验证 | `analysis/monthly_runs_quant_research_framework.py`、`analysis/monthly_runs_walk_forward_validation.py`、`analysis/quantml_candidate_execution_walk_forward.py` | 仅用训练/验证选择模型和执行配置，保留独立 test 与滚动 walk-forward 结果。 |
| 运行时 | `analysis/live_paper_trader.py` | 发现当前 BTC 5 分钟市场，读取盘口和 BTC 参考价，生成信号，写入事件、快照、状态和 dashboard。 |
| 订单适配 | `analysis/live_execution.py` | 封装官方 Polymarket CLOB V2 SDK；只有 `--execution-mode live --allow-live-trading` 才会提交订单。 |
| 研究交付 | `reports/`、`docs/` | 保存 Markdown/JSON/CSV 汇总、图表、可编辑研究表、审计说明和运行设置。 |

## 决策与执行边界

1. `q_model` 是校准后的胜率估计；交易信号必须同时通过 edge、盘口深度、spread、overround、事件额度和重复市场等门槛。
2. 仓位使用 fractional Kelly，并受每个 market 的 event cap 限制；多次入场还受次数和最小间隔限制。
3. `paper` 只模拟延迟成交、滑点容忍和结算；`plan` 只记录订单参数；两者都不提交真实订单。
4. `live` 只负责显式订单提交。完整账户、订单、持仓、成交和结算对账仍是后续工作，不由本仓库的 paper 状态文件自动证明。

## 主要产物

- `reports/robust_trading_system/`：稳健系统报告、决策漏斗、因子贡献、压力测试、纸面回放和 walk-forward 汇总。
- `reports/quantml_latest_qrf/`：量化研究框架、策略注册表、数据覆盖和 split 指标。
- `reports/quantml_latest_factor_sweep/`：因子诊断、验证/测试排名和选中策略表现。
- `reports/quantml_candidate_execution_walk_forward/`：候选执行配置的滚动评估。
- `reports/live_paper_trading/`：本地运行状态和 dashboard；逐笔事件日志属于本机运行数据，不纳入 Git 备份。

## 当前已知缺口

- 行情仍以 REST polling 为主，CLOB WebSocket 尚未接入。
- 账户/订单 WebSocket、实时持仓同步、成交和结算对账尚未完整实现。
- 本地 dashboard 有状态和 CSV 输出，但尚未接入告警与常驻 launcher。
- 研究报告中的历史回测、纸面回放和 walk-forward 结果，不能单独证明实盘成交或未来收益。

## 本地文件边界

真实凭据放在 `.env.local` 或 shell 环境中；`api_key/`、虚拟环境、`poly-bot-btc-5/`、论文原件归档和逐笔运行日志均通过 `.gitignore` 排除。GitHub 备份以可复现代码、可审阅汇总和架构/操作文档为主。

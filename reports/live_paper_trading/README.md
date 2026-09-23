# Live Paper Trading

这个目录用于 Polymarket BTC 5min Up/Down 的 live paper trading。

含义：

- 实时读取当前 5min market 的 Gamma/CLOB 盘口和 BTC 参考价格。
- 用本地 30 天数据训练出来的 robust mixed 模型计算 `q` 和 `edge`。
- 只做 paper 模拟：看到信号后等待 4 秒，再检查盘口是否仍可按容忍滑点成交。
- 到 5min window 结束后，用 BTC 当前价格相对目标价结算 paper PnL。
- 不调用真钱下单接口，不需要钱包、API key 或私钥。

默认启动：

```bash
python3 analysis/live_paper_trader.py
```

第一次运行会训练并缓存 live 模型，之后会直接加载：

```text
reports/live_paper_trading/live_model_cache.pkl
```

只训练模型缓存：

```bash
python3 analysis/live_paper_trader.py --prepare-model-cache
```

只跑一步，用于测试接口和输出：

```bash
python3 analysis/live_paper_trader.py --once
```

输出文件：

- `snapshots.csv`：实时盘口快照。
- `paper_events.csv`：signal、fill_check、settle 事件。
- `state.json`：当前 paper bankroll、pending orders、open positions。
- `index.html`：中文 dashboard，浏览器打开后刷新即可看最新状态。

重要约束：

- 这是 paper trading，不是真实下单。
- 它用 4 秒延迟和滑点容忍模拟成交，但还没有真实队列位置。
- 若之后要接实盘订单，应该单独做 order adapter，并加显式 `--live-ordering` 安全开关。

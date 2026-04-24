# Polymarket BTC Up/Down 5m 数据分析

这个目录放一版先行的数据分析脚手架，用来快速检查你采集的 5 分钟二元事件数据是否具备可交易性。

## 预期输入

脚本支持 `csv` 或 `parquet`，优先从 `data/` 目录自动寻找文件，也可以手动指定：

```bash
python analysis/eda_polymarket_5m.py --input data/your_file.csv
```

如果数据字段名和下面任意一种接近，脚本会自动识别：

- 时间列：`timestamp`, `ts`, `time`, `datetime`, `start_time`
- 结果列：`outcome`, `label`, `winner`, `result`, `direction`, `is_up`
- 标的开收盘：`btc_open`, `btc_close`, `open_price`, `close_price`
- 概率/价格列：`p_up`, `prob_up`, `up_yes_mid`, `price_up_yes`, `p_down`, `prob_down`

如果没有显式结果列，但有开盘价和收盘价，脚本会用 `close > open` 推导 Up/Down。

## 输出内容

脚本会生成：

- `reports/eda/summary.json`
- `reports/eda/report.md`
- `reports/eda/missingness.csv`
- `reports/eda/transition_matrix.csv`
- `reports/eda/hourly_up_rate.csv`（若有时间列）
- `reports/eda/calibration_up.csv` / `calibration_down.csv`（若有概率列）
- `reports/eda/threshold_backtest.csv`（若有概率列）
- `reports/eda/figures/*.png`（若安装了 matplotlib）

## 默认会看什么

1. **标签分布**：Up/Down 是否显著失衡
2. **序列依赖**：上一根是 Up/Down 时，下一根的条件概率
3. **连涨连跌**：streak 长度和自相关
4. **时间分层**：UTC 小时段 / weekday 是否有结构性偏差
5. **概率校准**：如果你采了盘口价格/隐含概率，检查 calibration、Brier score、log loss
6. **阈值回测**：对 `p >= threshold` 的简化入场规则做净值粗测

## 运行示例

```bash
python analysis/eda_polymarket_5m.py --input data/btc_updown_5m.csv --fee 0.01
```

这里的 `--fee` 是一个保守的“单笔固定成本”，按概率点计价；例如 `0.01` 表示每次交易额外扣掉 1 cent on a $1 payoff 的成本。它不是交易所官方费率映射，只是方便你先做敏感性分析。

## 下一步建议

等你把实际数据放进 repo 后，优先看三件事：

- 高置信度区间是否真的更赚钱，而不是只看整体胜率
- 不同时间段/波动阶段是否出现稳定偏差
- 扣掉成本后，优势是否还存在

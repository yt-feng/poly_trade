# 本地支撑交接（公开最小材料）

本轮编号：`20260929T113749Z`。原资料包状态截至 `2026-09-29T11:48:53Z`：`partial`、`support_only`、`trading_enabled=false`。此页仅公开执行状态、缺失输入和既有公开运行记录，不含策略参数或未公开研究结果。

已建立本地隔离环境，具体版本记录保存在加密资料包中。辅助工具的 9 项刷新检查和 7 项收集检查通过，均为本地合成工具测试，不能算作固定实验通过。本地环境可用不代表 ChatGPT 平台已修复。

已取得三份旧现场元数据：[run 36547009200](https://github.com/yt-feng/poly/actions/runs/36547009200)、[live-smoke job 109336100547](https://github.com/yt-feng/poly/actions/runs/36547009200/job/109336100547)、[artifact 11022698629](https://api.github.com/repos/yt-feng/poly/actions/artifacts/11022698629)。旧运行提交是 `ce443fae62d1e4e2aa7041b4a6b35e114978ac01`，不能当作当前 main。元数据显示 job 第 5 步 `Public feeds smoke test, no trading credentials` 失败。

旧 artifact 声明大小为 16,494,247 字节，预期 SHA256 为 `5bca079377bb213a51801ac3006690aede7d6070d69d559d18f9620a9b8b97ab`。下载 60 秒超时，退出码 124，仅收到 490,464 字节；未形成完整 ZIP，未核验完整摘要，部分字节未留存。该次收集随即停止后续 GitHub 请求。因此两个仓库的当前 main、开放 PR / PR #10 和最新相关 Actions 状态均未取得，水位为 `comparison_incomplete`。

固定实验输入包未在指定位置找到；具体文件名保存在加密资料包与原对话中。下面三条命令均未执行，测试数量和重建一致性均未知：

```text
python run_audit.py verify
python run_audit.py test
python run_audit.py study --rebuild
```

旧原始输入和冻结解析器未取得，因此页面身份、RSC 解析、queryKey、状态、更新时间、lookback、openPrice 七层全部为 `not_evaluable`，不能解释为七层失败。`resolution_reference缺失` 仅是用户提供的旧线索，本轮没有从原日志重新验证。重放收据退出码 2 表示缺输入，没有执行解析器。

ChatGPT 下一轮可以据此明确需要哪些输入：

1. 用户提供上述冻结 ZIP 的实际位置或文件；取得后校验外层 SHA256，审阅 README、manifest、依赖与入口，再执行原三条命令。
2. 两个仓库的当前 main / PR / 指定 Actions 元数据，补齐此前未取得的当前状态。
3. 完整旧 artifact；其中必须提取原 micro_event_page HTML、原 slug/URL、原 received 时间和记录身份。
4. 同 slug 的原市场元数据、condition ID、来源和接收时间。
5. 上述历史提交的解析器及直接依赖/配置，以及当时 health 和最短相关失败日志。

不能用当前页面、事后价格或修改后的匹配条件替代旧输入。现有结果没有构成新的样本外证据，也没有给出策略晋级、canary 或投资结论。下一步研究判断仍由原 ChatGPT 对话负责；本地端只接受用户明确运行的有限支撑任务。

完整私人资料包存放于[加密记录](research_vault/records/28d51032c1cd.vault)。本页与 `support_handoff.json` 无需解密即可读取缺失输入和执行边界。原明文包为 67,356 字节，SHA256：`28d51032c1cd39c8da0f12f65bee07e8f90dba2c4726159cf2cd05e70cdc006b`；请按机器索引中的密文摘要核验下载，解密后再核验此明文摘要。

本分支仅用于用户明确授权的资料交接，不包含工作流定义，不修改 main 或已有 PR。加密沿用仓库既有 X25519 公钥与 envelope 格式，未初始化新个人身份、读取私钥或使用 PIN。没有现成授权解密能力时，可先读取本页与机器索引；不要声称已查看密文中的正文。

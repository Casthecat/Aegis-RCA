# 0.2 验证记录

记录日期：2026-09-09（UTC）。本机 Windows / PowerShell，Python 3.12.6，项目 0.2.0。此记录区分代码测试、人工演示、真实 HTTP 和尚未完成的外部验收；不报告 RCA benchmark 准确率。

## 自动化测试与依赖

最终完整测试执行：

```text
python -m pytest -q
204 passed in 1.90s

python -m pip check
No broken requirements found.
```

主要覆盖：

- 契约、半开时间窗、不可变证据、幂等合并与冲突拒绝。
- 图路由、额度先分配、无隐式重试、异常隔离、超时、部分成功保留。
- Prometheus/Jaeger/OpenSearch 响应解析、范围限制、截断和坏响应。
- 零 MAD、counter reset、非有限值、缺失样本、观测拓扑和时间线。
- LLM schema/引用检查、原子预算预留、取消/超时、错误脱敏和失败回退。
- 评分分母、分组/派生样本切分、泄漏候选和独立人工语义标注。
- API 排队上限、任务状态、终态文件和重启后 interrupted。
- 故障录制恢复、独占锁、输出目录防覆盖、固定镜像和共享 flag 配置。

HTTP adapter / LLM provider 测试采用模拟 transport，故障录制测试也使用模拟后端与临时文件。它们不证明真实部署或模型兼容性。

## CLI 与人工快照

以下流程均实际运行通过，结果位于 `artifacts/verification-final/`；默认命令另外验证了 UUID 输出目录。

| 流程 | 结果 | 轮数 / 工具调用 |
| --- | --- | --- |
| 默认 connectivity fixture | SUPPORTED，故障模式为连接失败 | 2 / 3 |
| synthetic 原始规范化快照回放 | SUPPORTED，保留原始来源引用 | 2 / 3 |
| missing-logs | INCONCLUSIVE | 2 / 3 |
| empty | INCONCLUSIVE | 2 / 3 |
| `--max-tool-calls 2` | INCONCLUSIVE，预算停止 | 1 / 2 |

这些报告全部 `mode=synthetic_fixture`、`reasoning_mode=deterministic`、`underlying_cause=null`。工具调用数指调查 backend 尝试，不包含 LLM 调用。

快照分析观察到人工指标的中位数从 0.01 变为 0.45，绝对变化 0.44 超过明确阈值 0.05。baseline MAD 为 0，因此 `robust_score=null` 并输出告警，没有伪造统计显著性。拓扑仅包含观测到的 checkout → payment，`complete=false`。本例泄漏扫描无候选，不代表扫描能证明任意数据已彻底脱敏。

可复制的评分输入已保存到 `examples/evaluation/`。CLI 评分器成功读取 manifest 与报告：人工 dev 示例结构化标签匹配 `1/1`；未提供人工语义评审，所以相应指标为 `N/A`；validation/test 没有数据。**此结果不是实际事故准确率。**

| 本地证据文件 | 内容 |
| --- | --- |
| [checks.json](../artifacts/verification-final/checks.json) | 对报告状态、调用数、模式和评分分母的检查结果 |
| [replay/report.md](../artifacts/verification-final/replay/report.md) | 当前人工回放报告 |
| [analysis/metrics.json](../artifacts/verification-final/analysis/metrics.json) | 指标计算结果与零 MAD 告警 |
| [scores.synthetic.json](../artifacts/verification-final/scores.synthetic.json) | 人工评分示例输出 |

`artifacts/` 被忽略，不属于可移植的源码交付；重建方法见 README 和运行手册。`examples/evaluation/` 则包含自足的人工评分输入。

## 本地真实 HTTP

曾实际启动 uvicorn 于 `127.0.0.1:18001`，通过独立 httpx 进程执行健康检查、POST 提交、轮询任务和读取报告：`202 → completed → 200`。产物 [api-smoke.json](../artifacts/api-smoke.json)。此处 HTTP 是真实的，事故数据依然是人工 fixture。

测试服务器已停止；当前没有常驻网页或外网服务。API 测试也包含在上述完整测试中。

## 固定版本配置与实际外部阻塞

OTel Demo 3.0.0 源码已下载。Compose 配置已通过本机 `docker compose config` 生成与检查；checkout/payment/opensearch 应用镜像均为 `3.0.0-*`，而非 latest。所有发布端口绑定 localhost，flagd/flagd-ui 共用项目内副本。这只证明配置成功生成，不证明镜像已经拉取或服务已经运行。

Docker Desktop 启动曾超时，日志显示 `initializing Inference manager` 阶段无法访问 `dockerInference` 文件。实际本地 backend doctor 的 metric_names、metric_labels、services、log_mapping 四项查询均失败，记录于 [backend-catalog.json](../artifacts/environment/backend-catalog.json)。

仍未执行：

- 真实 OTel 容器启动、业务负载、三类遥测 readiness 与 mapping 验证。
- 真实健康/故障/恢复录制与独立 episode 的可回答性标注。
- 真实模型接口请求、实际 token/账单核对和模型质量评测。
- B0/B1/B2 实验对照、网页、图 checkpoint 恢复与生产部署。

继续所需外部条件与逐步命令见 [运行手册](runbook.md)。后续真实验证的结果应单独追加，不能用人工用例替代。

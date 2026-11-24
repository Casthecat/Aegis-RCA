# AegisRCA

证据驱动的微服务事故调查项目：收集日志、指标和 trace，维护候选解释，针对缺失证据补查，输出可核验报告。

**当前版本 0.2：已实现后端 adapter、快照采集/回放、确定性分析、有预算的可选 LLM、离线评分器和本地任务 API。** 默认无需 API Key。真实 Docker 后端和付费模型尚未验收，测试和演示数据明确标为 synthetic；没有真实 RCA 准确率数字。

## 当前能力与边界

| 能力 | 已实现 | 尚待验证或扩展 |
| --- | --- | --- |
| 调查闭环 | 六阶段 LangGraph、Send 并行、定向补查、预算停止、引用、全局超时保留已完成证据 | 规则确认范围仍主要是 checkout → payment 连接失败 |
| 遥测 | Prometheus catalog/range、Jaeger 公共 v3、OpenSearch lexical/context | 本机 Docker 启动故障，未取得真实录制数据 |
| 回放与分析 | 不可覆盖的规范化快照、median/MAD、counter reset、观测拓扑、时间线 | 指标需用户配置的查询和明确阈值；尚未自动驱动通用 RCA |
| LLM | 结构化计划/候选、引用校验、调用/token/费用预留、deadline、失败回退 | 只用模拟 HTTP 验证；实际 endpoint/model/key/价格待配置 |
| 评测 | episode 切分保护、结构化评分、人工语义标注、泄漏候选检查 | 尚未采集正式 episode 或运行 B0/B1/B2 基线 |
| API | 本地异步任务、并发/排队上限、终态持久化、报告查询 | API 只运行 synthetic fixture；没有网页、生产认证或图恢复执行 |

模型提议始终是候选。当前报告中已确认的连接失败仍由窄范围证据规则检查；不能把任意 LLM prose 视为已通过语义审计的结论。

新增 [benchmark 候选清洗与人工审核](docs/sanitization.md)：结构化移除控制元数据、原件不变、独立 sidecar、扫描上限保护与人工准入。入口为 `python -m aegis_rca.evaluation` 的 `sanitize`、`scan`、`review` 子命令，具体参数和真实录制验收记录见该文档。

## 立即运行

工作区 `.venv` 已安装。新机器需要 Python 3.11+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe -m aegis_rca.cli
.\.venv\Scripts\python.exe -m pytest -q
```

默认示例经过 2 轮、3 次工具调用，输出 `SUPPORTED` 的连接失败观察；`underlying_cause=null`。默认结果写入新的 `artifacts/runs/<UUID>/`。指定 `--output` 时目录必须尚不存在，避免覆盖之前的报告、快照或费用记录。

```powershell
# 证据不足时保留不确定性
.\.venv\Scripts\python.exe -m aegis_rca.cli --scenario missing-logs
.\.venv\Scripts\python.exe -m aegis_rca.cli --scenario empty
.\.venv\Scripts\python.exe -m aegis_rca.cli --max-tool-calls 2

# 使用随仓库提供的人工遥测快照，走真实回放代码
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode replay --snapshot examples/snapshot.synthetic.json

# 明确指定 baseline、incident 分界与原指标单位下的变化门槛
.\.venv\Scripts\python.exe -m aegis_rca.collect analyze --snapshot examples/snapshot.synthetic.json --baseline-end 2026-09-08T14:35:00Z --min-absolute-shift 0.05 --output artifacts/analysis-demo

# 本地 API（单进程、只运行 synthetic fixture）
.\.venv\Scripts\python.exe -m uvicorn aegis_rca.api:app --host 127.0.0.1 --port 18001
```

API 提供 `POST /incidents`、`GET /incidents/{id}`、`GET /incidents/{id}/report` 和 `/health`。启动后可在另一个终端执行 `scripts/smoke_api.py`，验证真实 localhost HTTP 提交与报告读取。

## 接真实数据或模型

具体步骤见 [运行手册与待配合事项](docs/runbook.md)。基本入口：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_demo.py
.\.venv\Scripts\python.exe scripts/demo.py prepare
.\.venv\Scripts\python.exe scripts/demo.py up
.\.venv\Scripts\python.exe -m aegis_rca.collect doctor --backends config/backends.example.json --output artifacts/catalog.json
.\.venv\Scripts\python.exe -m aegis_rca.collect capture --backends config/backends.example.json --minutes 10 --output data/recordings/manual/snapshot.json
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode replay --snapshot data/recordings/manual/snapshot.json
```

已锁定 OTel Demo **3.0.0**。该版本的真实 flag 名是 `paymentUnreachable` 和 `paymentFailure`；不能沿用旧聊天中的名称。项目独立 Compose 使用 localhost 端口，并复制 flagd 配置，避免修改上游源码。启动前仍需 Docker Engine 正常。

配置好 [LLM 环境变量](docs/llm-setup.md) 后，用 `--llm` 显式启用模型。模型当前选择受限的 signal/services/goal，不能生成任意 URL 或 PromQL。费用文件记录保守预留，不代表实际账单。

## 代码导览

```text
src/aegis_rca/
  models/           # incident、evidence、hypothesis、raw telemetry
  adapters/         # 有界只读 HTTP、Prometheus、Jaeger v3、OpenSearch
  analysis/         # 指标比较、counter rate、观测拓扑、时间线
  tools/            # live collection、snapshot、replay、证据派生
  reasoning/        # 可选 structured LLM 与预算
  evaluation/       # 独立评分器，不启动调查进程
  graph.py          # 调查状态机与并行子图
  runner.py         # 总 deadline 与部分结果保留
  report.py         # 窄范围 claim 检查
  cli.py            # fixture/replay/live 调查入口
  collect.py        # doctor/capture/analyze
  api.py            # 本地 synthetic 任务 API
scripts/            # Demo 准备、控制、独立故障录制与 HTTP smoke
examples/           # 明确标注的 synthetic snapshot
tests/              # 模型、adapter、分支、预算、恢复、API 与评测测试
```

[架构与限制](docs/architecture.md) · [实施进度](docs/roadmap.md) · [评测协议](docs/evaluation.md) · [后端契约](docs/backend-contracts.md) · [模型配置](docs/llm-setup.md)

`requirements.lock.txt` 是本次 Windows/Python 3.12 环境的精确依赖快照，未做跨平台 hash 锁定。本轮验证结果记录在 [验证记录](docs/validation.md)。

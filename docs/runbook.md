# AegisRCA 运行手册与待配合事项

所有命令在项目根目录的 PowerShell 中运行。无需激活虚拟环境。完整自动化前，先区分三种数据：`synthetic_fixture` 是人工示例，`snapshot` 是真实录制回放，`live` 是只读实时查询。模型是否参与由 `reasoning_mode` 单独标记。

## 1. 无外部依赖即可体验

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.cli
.\.venv\Scripts\python.exe -m aegis_rca.cli --scenario missing-logs
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode replay --snapshot examples/snapshot.synthetic.json
.\.venv\Scripts\python.exe -m pytest -q
```

每次调查默认新建 `artifacts/runs/<UUID>/`；显式 `--output` 也必须指定新目录。目录中有 report、evidence、tool_calls、trajectory，回放/实时数据另有 topology/timeline。**同一个 incident 的生成文件不与旧运行混合。** `collect analyze` 是可重复的派生分析，会覆盖它的分析输出文件；原始 snapshot 则不可覆盖。

## 2. 目前需要你配合的事项

### A. Docker Desktop 正常启动

本机约 32 GB RAM，Docker Client 29.2.0、Compose v5.0.2 已安装，WSL2 已配置。曾尝试启动 Docker Desktop，但它返回启动超时。随后从本机 Docker 日志定位到实际错误：

```text
starting services: initializing Inference manager
... Docker/run/dockerInference ... The file cannot be accessed by the system
```

请先修复或重启 Docker Desktop，直到 `docker info` 能返回 **Server** 信息。当前没有启动 AegisRCA 的 OTel 容器，没有进行真实故障注入。诊断结果在 `artifacts/environment/backend-catalog.json`；四项 catalog 查询当时均不可达。

这一步是本机 Docker 自身的启动问题。项目没有执行恢复出厂设置、清空镜像/卷或改动 Docker 的全局配置。若改用另一台可用的 Docker 主机，可把那台主机上的三项后端地址配置到本项目。

### B. 选择并配置模型接口

可选 LLM 需要支持 Chat Completions `json_schema` 的兼容接口：endpoint、model、API key，以及你选定模型的输入/输出价格。配置方法见 [LLM setup](llm-setup.md)。Key 放在本地 `AEGIS_LLM_API_KEY` 环境变量；不用写入仓库或聊天。

没配置时所有离线演示、工具和 API synthetic 模式仍可运行；只有显式 `--llm` 才调用模型。当前未发送真实付费模型请求。价格用于保守预留，不能把预留数当成实际账单。

## 3. Docker 恢复后的启动步骤

```powershell
.\.venv\Scripts\python.exe scripts/prepare_demo.py
.\.venv\Scripts\python.exe scripts/demo.py prepare
.\.venv\Scripts\python.exe scripts/demo.py up
.\.venv\Scripts\python.exe scripts/demo.py status
```

准备脚本使用 OTel Demo 3.0.0；`demo.py prepare` 显式设置 `DEMO_VERSION=3.0.0`，因为上游该 tag 的 `.env` 仍默认 `latest`。`up` 会拒绝未固定版本的旧生成配置。

Compose 项目名/网络为 `aegis-rca-demo`；不使用上游固定 container_name。主要端口：

| 服务 | 本地地址 |
| --- | --- |
| 应用 Frontend proxy | `http://127.0.0.1:18080` |
| Prometheus | `http://127.0.0.1:19090` |
| Jaeger | `http://127.0.0.1:16686` |
| OpenSearch | `http://127.0.0.1:19200` |

全部发布端口绑定 localhost。若已有应用占用这些端口，应先调整生成脚本与 backend 配置再启动。源代码在忽略的 `.cache/opentelemetry-demo/`。flagd 与 flagd-ui 使用同一份 `artifacts/demo-flags/` 副本；故障录制不会修改上游文件。

停止这个项目：

```powershell
.\.venv\Scripts\python.exe scripts/demo.py stop
```

## 4. Catalog、采集、回放

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.collect doctor --backends config/backends.example.json --output artifacts/catalog.json
```

确认服务名、metric names/labels 和日志 mapping。`config/backends.example.json` 的 `metric_query` 默认 null，因为不应猜 PromQL。复制为忽略的 `config/local.json`，填入通过实际 catalog 验证的只读查询。OpenSearch 默认按 Demo 3.0.0 的 SS4O 字段配置；如果实际索引模板使用不同的 exact 字段，可覆盖相应设置。

健康负载持续运行并能查询 checkout → payment 后：

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.collect capture --backends config/local.json --minutes 10 --output data/recordings/manual-001/snapshot.json
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode replay --snapshot data/recordings/manual-001/snapshot.json
```

也可用 `--incident path.json` 明确指定窗口。IncidentInput 格式见 `examples/snapshot.synthetic.json` 的 `incident` 部分。直接实时调查：

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode live --backends config/local.json --incident incident.json
```

回放命令以录制时的 incident 为范围，不能扩大窗口或读取其他 seed service。报告保留查询失败、数据截断及未知覆盖率。当前默认解析器只确认少数明确错误；未知 endpoint 不会凭空映射到 Payment，未配置指标阈值也不会由模型猜异常。

## 5. 独立故障录制

此脚本属于测试控制器，不是 Agent 工具。只支持固定版本中核验的两个 flag：

```powershell
.\.venv\Scripts\python.exe scripts/record_episode.py --backends config/local.json --flag paymentUnreachable --seconds 60
# 另一类故障的变体是 100%，并非 on
.\.venv\Scripts\python.exe scripts/record_episode.py --backends config/local.json --flag paymentFailure --seconds 60
```

它先检查 backend catalog，取得独占文件锁，保存原 flags 备份，再注入、采集并在 `finally` 恢复原文件。取消/失败恢复已有测试。进程被强杀或系统掉电仍可能留下锁和开启的 flag；此时先检查 private 审计与 `original-flags.json`，确认没有录制进程后恢复，再处理锁。

记录分为 `data/recordings/private/<UUID>/` 的注入清单与 `quarantine/<UUID>/snapshot.json` 的遥测。原始遥测可能含 feature flag 答案，因此不会自动进入 public 测试集。catalog 可达也不等于真实业务路径、采样、落库和恢复都合格；正式采集仍需按 [评测协议](evaluation.md) 验证。

已有录制进入 benchmark 前，使用 [确定性清洗与人工审核流程](sanitization.md)：`quarantine → sanitize → scan → 技术回放 → review → admit`。新命令只导出不透明 UUID 的候选，原件不变；来源映射和审核在独立 review 区，完整零命中仍须人工批准。`cli --mode replay --snapshot` 仍是工程调试/清洗步骤中的技术回放，不检查批准状态；正式准入只接受 `evaluation admit --candidate <opaque-id>`，二者不是同一门槛。

## 6. 分析与评分

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.collect analyze --snapshot examples/snapshot.synthetic.json --baseline-end 2026-09-08T14:35:00Z --min-absolute-shift 0.05 --injected-flag paymentUnreachable --output artifacts/analysis-demo
.\.venv\Scripts\python.exe -m aegis_rca.evaluation score examples/evaluation/manifest.synthetic.json --output artifacts/scores.synthetic.json
```

第二条命令使用已有人工演示报告，示范评分器调用，不是实际 benchmark。manifest 中 report/evidence/label 路径相对 manifest；评分进程可以读取 private labels，调查进程不能。citation validity 只检查结构与范围，语义正确性单独采用人工标注；没有人工评审时相应指标为 N/A。

## 7. 本地 API

```powershell
.\.venv\Scripts\python.exe -m uvicorn aegis_rca.api:app --host 127.0.0.1 --port 18001
# 第二个终端
.\.venv\Scripts\python.exe scripts/smoke_api.py
```

API 当前只允许 synthetic fixture，没有 live URL、任意文件路径或密钥参数。使用单进程；完成报告保存到 `artifacts/api/`，重启前仍在运行的任务标为 interrupted。关闭测试服务器用 Ctrl+C。当前没有常驻网站，也没有部署外网服务。

# 可选 LLM 提案组件

`src/aegis_rca/reasoning/` 提供一个异步 OpenAI-compatible Chat Completions 客户端，用于生成结构化调查计划和根因候选。当前验证全部使用 `httpx.MockTransport`，没有读取真实密钥、发送付费请求或确认某个线上模型可用。

本版本的计划会影响**选择哪些限定的信号查询**。它不生成任意 PromQL、OpenSearch DSL、SQL、URL 或代码，也不能调用故障注入、部署、重启等操作。实际查询范围和参数仍由后端工具适配器约束。

## 输出的职责边界

- `propose_plan(...) -> PlanProposal`：最多 6 个任务，每个只有 `signal`、`services` 和 `goal`。时间窗由运行器从原始 `IncidentInput` 继承。
- `propose_hypotheses(...) -> HypothesisProposalSet`：最多 3 个候选，包含支持证据、反证和仍需检查的事项。
- 计划、候选集合和每个候选的 `status` 都只能是 `provisional`。不能要求模型直接返回 `supported`、最终报告或置信度分数。
- 返回的 `incident_key` 必须匹配当前 incident；服务、信号、证据 ID 都会进行本地引用检查。输入证据也必须属于允许服务和 `[start, end)` 时间窗。
- 合法引用不等于证据支持论断。当前 graph 只把模型候选作为未确定假设；确定性的观测结论仍由独立的验证/报告规则决定。
- 遥测、用户问题、反馈和 catalog 都放在标为不可信的数据消息里；system prompt 不拼入这些内容。这有助于隔离指令和数据，但不是 prompt injection 已被完全解决的证明。

客户端使用 `response_format.type=json_schema` 与 `strict=true`。结构化输出保证的是格式约束，业务范围和证据引用还需要本地验证；拒绝和不完整输出也单独处理。[OpenAI 结构化输出文档](https://developers.openai.com/api/docs/guides/structured-outputs)。

## 运行前需要提供的配置

这些值由运行器注入，模块不会自动搜寻环境变量、用户目录或配置文件里的密钥：

| 字段 | 要求 |
| --- | --- |
| `endpoint` | 完整 URL，例如 `https://api.openai.com/v1/chat/completions`；不能含账号密码、query 或 fragment |
| `model` | 用户明确选择且在该 endpoint 上可访问的模型 ID；没有自动默认模型或迁移 |
| `api_key` | 运行时传入；以 `SecretStr` 保存，配置序列化及 `repr` 不包含它 |
| `input_usd_per_million` | 用户核对的输入 token 单价；必填 |
| `output_usd_per_million` | 用户核对的输出 token 单价；必填 |
| `allow_http_loopback` | 默认 `False`；本地服务需要 HTTP 时显式开启，只允许 localhost 或 loopback IP |

“OpenAI-compatible” 不是所有服务都完整支持同一协议。所选 endpoint/model 必须支持严格 JSON schema 和 `max_completion_tokens`。客户端不自动降低到普通 JSON 模式，也不在失败后尝试其他模型或 endpoint。

`max_completion_tokens` 限制可见输出和 reasoning tokens 的合计，不能只把最终 JSON 的长度当作模型输出费用。[Chat Completions API 参数](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

模块本身不选择模型、不内置价格表。若为本地免费模型，将单价显式设为 `0`；这只表示你选择的本地计费假设，不代表远程 API 免费。

CLI 使用这些环境变量：`AEGIS_LLM_ENDPOINT`、`AEGIS_LLM_MODEL`、`AEGIS_LLM_API_KEY`、`AEGIS_LLM_INPUT_USD_PER_MILLION`、`AEGIS_LLM_OUTPUT_USD_PER_MILLION`。仅当显式加上 `--llm` 时启用模型；本地 HTTP 端点还需设置 `AEGIS_LLM_ALLOW_HTTP_LOOPBACK=true`。密钥由运行器从当前进程环境取得，不写入配置文件或输出产物。

配置完这些值后，在所选 backend 的运行命令中加上：

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.cli --llm --max-llm-cost 0.25 --deadline 120
```

`--max-llm-cost` 限制模型的估计费用，`--deadline` 是整个 graph 的总时限。是否使用真实 telemetry 由 backend 模式另行决定；在人工 fixture 上启用 LLM 仍不能得到真实事故 benchmark。

## Python 集成

下面是集成函数；调用者提供配置对象和费率。它展示一个真实网络入口，只有配置完毕并明确选择模型模式后才运行。仓库测试用模拟 transport 替代网络。

```python
from decimal import Decimal

from aegis_rca.reasoning import (
    BudgetManager,
    OpenAICompatibleReasoner,
    ReasoningConfig,
    ReasoningLimits,
)


async def propose_next_checks(incident, evidence, config, input_rate, output_rate):
    limits = ReasoningLimits(
        input_usd_per_million=Decimal(input_rate),
        output_usd_per_million=Decimal(output_rate),
        max_calls=8,
        max_input_tokens=24000,
        max_output_tokens=5000,
        max_output_tokens_per_call=1200,
        max_estimated_cost_usd=Decimal("0.25"),
        total_timeout_seconds=120,
    )
    budget = BudgetManager(limits)
    async with OpenAICompatibleReasoner(config, budget) as reasoner:
        proposal = await reasoner.propose_plan(
            incident,
            allowed_services=incident.seed_services,
            allowed_signals=("metric", "trace", "log"),
            evidence=evidence,
            feedback="",
        )
        return proposal, budget.snapshot
```

`ReasoningConfig` 由运行器构造，示意参数如下。`runtime_key` 是调用者已取得的密钥变量，不应打印或写进仓库：

```python
config = ReasoningConfig(
    endpoint=runtime_endpoint,
    model=runtime_model,
    api_key=runtime_key,
    request_timeout_seconds=30,
    max_response_bytes=65536,
)
```

同一个事故的 planner/hypothesis 调用必须共享同一个 `BudgetManager`。不要每个节点重新创建，否则会重置次数、费用和截止时间。`Dependencies` 持有 reasoner 和 clients，`IncidentState` 只保存调查数据和可序列化结果。

## 预算、超时和失败

调用开始前由 `asyncio.Lock` 保护的预算管理器原子预留：1 次模型调用、保守输入 token 估计、该次最大输出 token，以及对应估计美元费用。并发节点不能同时花掉最后一个调用名额。

| 默认限制 | 数值 |
| --- | --- |
| 模型调用总次数 | 8 |
| 输入 token 预留合计 | 24000 |
| 输出 token 预留合计 | 5000 |
| 每次最大输出 token | 1200 |
| 估计费用上限 | 0.25 美元 |
| 事故模型调用总截止时间 | 创建预算管理器后 120 秒 |
| 单次请求超时 | 30 秒，且不超过总截止时间 |
| 单次响应字节上限 | 65536 bytes |

输入预留采用**整个 UTF-8 请求体字节数 + 1024**，其中包含 JSON schema，通常明显高估普通文本 token 数。它是本地保守估计，不是服务端计费 token 测量，也不保证适用于任意兼容服务的 tokenizer。费用按用户提供的两项单价计算；模型、实际单价或服务端行为与配置不一致时，本地估计不能保证外部账单上限。

`budget.snapshot` 中的 `reserved_input_tokens`、`reserved_output_tokens` 和 `estimated_cost_usd` 都是**预留值**，不能在 benchmark 中伪装成实测 token 或实际账单。当前采用不退款策略：成功调用也保留整笔预留，格式错误、HTTP 错误、超时和取消同样计费。取消客户端请求不保证服务端已经停止生成，所以不能据此返还预算。

客户端没有自动重试，也不会跟随重定向。HTTP 响应按字节上限读取，拒绝压缩响应，限制解码体积；本次 HTTP 的连接、响应读取受到同一个外层异步超时约束。错误只抛出固定分类，例如：

```text
call_budget_exhausted
input_token_budget_exhausted
output_token_budget_exhausted
estimated_cost_budget_exhausted
deadline_exhausted
provider_http_error
provider_refusal
incomplete_response
request_timeout
invalid_structured_response
invalid_plan_reference
invalid_hypothesis_reference
```

错误文本不包含上游响应、原始异常、URL、prompt 或密钥。`asyncio.CancelledError` 会向调用者传播；已经预留的调用保持已消耗。`BudgetExceeded` 是 `ReasoningError` 的子类，运行器可据此结束调查或保留现有可靠发现，不应无限重试。

## 本地验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_reasoning.py -q
```

测试涵盖 schema 与引用、范围越界、provisional 状态、并发预算、四类限额、截止时间、取消、响应字节上限、HTTP 错误脱敏、不跟随重定向、无自动重试，以及不可信遥测在 prompt 中的分离。

线上 endpoint 兼容性、模型质量、实际 token 使用、付费账单和真实事故准确率仍需要另行验证。相关评测遵守 `docs/evaluation.md`，不能把这些模拟 HTTP 测试当作真实 RCA benchmark。

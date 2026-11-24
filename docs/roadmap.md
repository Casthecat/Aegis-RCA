# AegisRCA 实施进度

P2.2 已实现语料协议、独立 GDR/GoldLabel V2、私有生产记录、split/QA 和 BSP-v2 工具。仓库已具备 pilot 流程；本轮未进行真实录制、故障注入或批量 Phase-A 生产，也未开始 P2.3。详见 [P2.2 实施说明](corpus-protocol.md) 与 [3–4 episode 操作手册](pilot-runbook.md)。

P2.2 最终本地验证：**1225 passed, 1 skipped**；定向语料/admission/scoring/Stage C 回归：**317 passed, 1 skipped**。112 个受保护文件及 P2.1 BV/BB/BEP/BSP-v1 身份保持不变。

P2.1 的 approved-only Stage C runner 与冻结 B0/B1/B2 架构消融保持不变，见 [Stage C 实施、命令与评测边界](stage-c-benchmark.md)。合成测试不代表真实 benchmark 效果。

Stage C 的 `ExperimentSummary` 已增加冻结、版本化和有界契约：最多 256 个候选、3 个变体、32 个 BSP。汇总明确区分 requested/admitted/attempted/completed；所有主比较使用同一组完整配对候选，提供调用/轮数均值与中位数、逐候选有符号差值、限制类别计数以及独立的 UNSCORED 分母。评分中的候选宏平均、合并 claim 比率和过度声明发生率各自保留定义；同一个归档或其副本不能证明重新执行的一致性。`score-run --run` 可接收归档目录或 `run.json`。该 P2.1 汇总契约保留；P2.2 另行增加 Gold V2 的独立评分协议。

上一阶段 P1.7c 已完成核心 P1 语义架构：有界多假设组合、共享采集、逐 HS 持久化验证与多 failure-mode 报告。契约、测试和迁移见 [P1.7c 实现说明](hypothesis-portfolio.md)。该阶段止于 P1.7c；当前 P2.2 状态见上文。

下面保留早期 0.2 路线图的操作验收记录；它不等同于当前 P1.1–P1.7c 语义阶段编号。真实部署、模型和效果验收不能以人工 fixture 或本地 pytest 代替。历史 P2 scaffold 勾选也不表示本轮新增了 P2 工作。

## P0：调查闭环

- [x] Pydantic contracts、结构化 state、不可变证据 ID 与幂等合并。
- [x] 六阶段 LangGraph、动态并行、定向补查、重新规划。
- [x] 工具调用/轮数上限，全局 deadline，部分成功结果保留。
- [x] 证据不足返回 INCONCLUSIVE，连接失败与底层原因分开。
- [x] JSON/Markdown、证据、工具调用及执行轨迹。

## P1：真实遥测接入

- [x] 检查本机：约 32 GB RAM，Docker/WSL 已安装；定位到 Docker 启动错误。
- [x] 下载并锁定 OTel Demo 3.0.0；Compose 独立项目、固定应用镜像标签与 localhost 端口。
- [x] Prometheus catalog/query_range、Jaeger 公共 v3、OpenSearch lexical/context/mapping adapter。
- [x] 查询响应字节/结果数/时间窗口上限，显式错误、空结果、超时和截断。
- [x] median/MAD、零 MAD、缺失/非有限样本、counter reset；分析阈值由配置明确给出。
- [x] 观测拓扑和时间线，不把拓扑缺失或先后顺序当成因果证明。
- [x] 规范化快照采集/回放，以及从模拟 wire response 到完整图的集成测试。
- [x] 独立故障录制脚本、私有真值、隔离录制区、文件锁及取消后恢复测试。
- [ ] Docker Engine 成功启动，真实 checkout workload 与三类 telemetry readiness。
- [ ] 真实 mapping/catalog 验证，选择 metric_query 并核实基线/流量。
- [ ] 实际注入 `paymentUnreachable` / `paymentFailure`，记录健康、事故、恢复证据。
- [ ] 用真实数据确认规范化规则；未知 endpoint 无法归属时继续保留不确定性。

## P2：模型参与调查

- [x] 可选 OpenAI-compatible strict structured-output provider，key 从本地环境变量读取。
- [x] 计划中的 signal/services/goal 与 evidence IDs 验证，候选保持 provisional/uncertain。
- [x] 预算预留、模型调用上限、input/output token 上限、费用预留、deadline；失败不隐式重试。
- [x] 与 planner/hypothesize 接线，模型先查日志时能继续补查 trace；错误回退并记录。
- [ ] 配置真实 endpoint/model/key/价格，验证所选模型的 schema 兼容性和实际成本。
- [ ] 基于真实 catalog 扩展 query templates；当前模型选择 signal，不自由生成查询。
- [ ] 通用候选反证更新、跨请求关联、任意自由文本 claim 的语义审计与有界修复。
- [ ] 持久化 LangGraph checkpoint 与进程重启恢复。API 保存终态不等于图恢复执行。

## P3：可信评测

- [x] 完整评测协议、public/private 隔离设计与可回答性分层。
- [x] 离线 scorer、完整 episode/group 切分检查、结构化匹配和人工语义评分分开。
- [x] Feature flag/注入关键词泄漏候选扫描；零分母显示 N/A。
- [ ] 先采 8 个独立 pilot episode，验证负载、采集与人工可定位性。
- [x] P2.1 已冻结当前 B0 survey_stop、B1 single_hypothesis_check_driven、B2 full_p1 的公平对照；早期拟议的日志单次报告基线未实现，也不作为当前矩阵的一部分。
- [ ] 采集独立测试 episode，报告逐样本结果、coverage、错误 claim、成本及失败案例。

目前没有 benchmark 结果；人工快照上的通过率不作为 RCA 效果。

## P4：展示与服务

- [x] 本地 FastAPI 异步任务、状态/报告查询、队列上限、错误脱敏。
- [x] UUID 目录保存终态；重启遗留运行标记 interrupted。
- [x] 真实 localhost HTTP smoke：202 提交 → completed → 200 报告。
- [ ] API 接入受配置控制的真实/replay 模式；当前 API 只运行 synthetic。
- [ ] 页面、Agent 自身 OTel tracing、真实事故演示视频。
- [ ] 多用户认证、跨进程任务队列和生产部署。

## P5：由失败案例驱动扩展

- [ ] 检索召回成为瓶颈后，再加入 Knowledge RAG、向量检索、RRF/reranker。
- [ ] 单控制器确有专业策略瓶颈后，再拆自治 specialists 并比较成本收益。
- [ ] 有外部工具消费需求时，再暴露 MCP。

早期真实部署/模型验收仍需 **让 Docker Engine 正常运行**、**配置可调用的模型接口**，操作见 [运行手册](runbook.md)。这两项不是当前离线 Stage C 的运行前提；后续正式效果评测首先需要独立采集且人工批准的候选与明确的私有标签契约，本轮未开始该项工作。

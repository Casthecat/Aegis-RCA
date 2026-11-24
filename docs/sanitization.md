# Benchmark 候选清洗与人工审核

本流程用于 telemetry-only masked 实验：将 quarantine 录制导出为**待审核的候选**。它不证明通用泄漏安全，也不改变 RCA 结论或 Observation/Normalization。规则对健康、故障和对照样本应一致应用。

`TelemetrySnapshot` schema 没有变化；`snapshot.json` 仍可被原有 `read_snapshot`、ReplayBackend 和调查 CLI 读取。benchmark 管理信息全部保存在 sidecar。

## 生命周期与文件边界

```text
data/recordings/
  quarantine/<source UUID>/snapshot.json   # 只读输入，永不覆盖
  private/<source UUID>/...                # 原注入审计，清洗器不读取它
  candidates/<new UUID>/
    snapshot.json                         # 普通 TelemetrySnapshot
    candidate.json                        # 非秘密的候选来源说明
  review/<new UUID>/                       # 审核者专用，不交给调查者
    audit.json                            # 原件 UUID/hash、删除计数/位置清单、私有扫描词表、清洗前扫描
    scan.json                             # 完整公开候选包的清洗后扫描、结构性阻塞
    decision.template.json                # 默认 pending，供人复制后填写
    ready.json                            # 完整导出标记，失败的部分产物没有此文件
    decision.json                         # 显式审核命令写入；一次性、不可覆盖
```

`candidate.json` 只含格式版本、候选 UUID、固定的 quarantine 来源类别、规则集版本与候选快照 SHA-256。它不含原 episode ID、原件 hash、路径、故障名称、标签、扫描词表或审核备注。删除计数保存在私有 audit，避免把清洗数量作为公开的分类提示。

新导出的 `review/<UUID>/audit.json` 还含私有 `removal_manifest`，其 `entries` 每项只记录 `path` 与 `rule`（`control_event` / `control_attribute` / `control_log`），条目数与删除总计一致。整事件或日志删除记一项，不重复展开其内部字段。不会记录被删值、动态键名、flag 名、variant、正文或 trace ID；公开 snapshot/sidecar 均不新增这些路径。

清单采用 `path_format=source-positions-v1`：固定前缀如 `$.traces.spans[0].attributes` 定位原件；`[n]` 是原始数组从 0 开始的下标，`{n}` 是该层字典按键名排序后第 n 个成员的值。例如 `$.traces.spans[0].attributes{0}{0}[0]` 从第一个 span 的 attributes 开始，依次取两层字典中排序第一的成员，再取数组第一项。字典序号按 Python 字符串排序，与 JSON 插入顺序无关；定位时使用 audit 中 source_sha256 对应的原件，不能用删减后的候选。此表示避免把含秘密的属性键或祖先键写入清单。清单按 path/rule 排序，内容确定；既有候选及其不可覆盖审计不会自动补写，需要新清单时从同一原件重新导出。

新候选 ID 自动生成 UUID。快照内容使用稳定 JSON 序列化，无导出时间和随机字段；同一输入经过同一规则得到完全相同的 snapshot bytes。不同导出的候选 ID 和 sidecar 可不同。原始 live/synthetic provenance 保留。

路径必须遵守上述目录结构，源 ID 必须是规范 UUID。拒绝覆盖、路径穿越、symlink 和 Windows junction/reparse point。中途失败保留未完成目录供检查，但没有 ready 标记的产物不可审核。不会修改或删除已有录制。

这是路径和工具职责隔离，**不是操作系统访问控制**。正式调查进程应只挂载已审核候选及预先允许的资源；不得让带文件工具的 Agent 同时访问 recordings 根目录、review/private、注入配置或带答案的文档。scorer 仍只评分已存在的报告，不自动执行这里的候选准入检查。管理者可用下方正式准入 API/CLI 复验当前字节与人工决定；它不替代尚未接入的隔离 `cli --mode benchmark` 运行器。

## 技术回放与正式准入

`python -m aegis_rca.cli --mode replay --snapshot <path>` 仍接受任意快照路径，用于工程调试和清洗流程中的技术回放。它不检查人工批准，不能当作正式 benchmark 准入。

正式纳入评测前，管理者只用不透明候选 UUID：

```powershell
python -m aegis_rca.evaluation admit --candidate <opaque-id>
```

该命令复验当前公开字节、冻结扫描与已存储的人工决定。成功只打印非秘密元数据（`candidate_id`、`review_status`、`benchmark_eligible`、`bundle_sha256`、`scan_sha256`、`ruleset`），不打印 snapshot 内容或 `review/`、`private/` 路径，也不启动调查。pending、rejected、stale、truncated、blocked、quarantine episode UUID，或任意 snapshot 路径均失败。

`scan` 仍是审核工具：它可以计算 eligibility 摘要并返回 `private_scan_report` 路径，但不会发布或准入候选。`open_approved_candidate()` 在校验通过后返回已解析的内存快照，调用方不必再读候选或审核文件。

## 规则集 control-metadata-v1

| 输入 | 行为 |
| --- | --- |
| 明确命名为 `feature_flag.evaluation` 的 span event | 整个事件移除，包括 provider、reason、key、value、variant 和 context |
| attributes 中 `feature_flag` 子树、`feature_flag.*` 或显式扁平 `feature_flag_*` 命名空间 | 移除完整属性/子树；OTLP KeyValue 连同 AnyValue 移除 |
| `events`、OTLP arrayValue/kvlistValue 中的等价结构 | 按相同结构规则处理，包括嵌套 resource/attributes |
| 日志 attributes 的 `event.name` 或完整 JSON body 明确标识评估事件 | 整条控制日志移除 |
| 普通业务日志中的控制属性 | 清理 attributes，原 body、ID、时间、trace/span 关联不变 |
| 业务正文含可疑词或与控制信息混合 | 不替换、不删行；保留给 scanner 和人工审核 |
| 业务 JSON 日志内有明确控制字段，但不是独立评估事件 | 原正文保留，记录结构性阻塞，不能批准 |
| 指标身份/labels 中有明确控制信息 | 不删除 label、不合并 series、不改数值；记录结构性阻塞 |
| 其他业务数据 | 保留；普通 `flag`、`provider`、`reason` 不作为通用删除规则 |

保留 RPC/OTLP status、service、operation、span kind、时间/时长、trace/span/parent ID、普通事件、日志与指标样本。`ERROR/UNAVAILABLE` 与 `ERROR/UNKNOWN` 都保持原义。未知 flag 名与已知名采用相同规则；没有故障名替换表，没有 LLM 清洗或分类。

无法识别的自由文本和其他未覆盖的编码形式可能仍有泄漏。因此输出始终称 candidate，不能仅凭扫描零命中称为 leakage-safe。含明确残留控制结构的候选只供检查，不能交给 blind benchmark。

## 扫描与审核约束

现有 `evaluation.scoring.scan_leakage` 未改动。清洗前对原快照扫描；清洗后扫描整个公开包（snapshot 与 candidate sidecar，包括公开文件名）。私有词表可提供已知 injected flags 和 scenario answers；只用于扫描，不能驱动字符串替换。词表在导出时冻结，重扫和审核复用同一配置。

- 命中数达到 **100** 就保守记录 `scan_truncated=true`、`scan_complete=false`，即使真实命中恰好只有 100 条。
- 树深度/节点预算或扫描处理失败也记录未完成；扫描无法完成时，0 条已返回命中不代表干净。
- 候选输出文件只允许 snapshot.json 与 candidate.json。增加其他未扫描公开文件会使检查失败。
- scan 命令会重新计算，并与不可覆盖的 scan.json 比较。文件、扫描规则或词表不匹配时拒绝沿用旧审核，需从原始 quarantine 重新导出候选。
- 清洗后零命中且扫描完整，仍是 `pending_review`、`benchmark_eligible=false`。

人工审核必须检查完整候选，而不只是命中列表；每个启发式命中逐条标为 `non_leakage`、`true_leakage` 或 `unresolved` 并说明原因。这里的人工分类不会修改 scanner 的原始候选。

批准需要完整且未触顶的扫描、没有结构性阻塞、所有启发式命中均经人工判为 non_leakage，以及明确 reviewer 和 full_snapshot_reviewed。存在 true_leakage/unresolved 时不能批准；结构性阻塞和触顶不能通过手工填 non_leakage 绕过。拒绝可保存为 ineligible。

审核绑定 snapshot 与 candidate.json 的**字节摘要**及完整扫描摘要；即使仅修改 JSON 空白也使旧批准失效。decision.json 一次性创建，不覆盖既有决定。需要修订时重新从原件导出。审核身份是本地操作记录，不是签名认证；审核目录应由实验管理者控制。

## 对一个已有 episode 的准确操作流程

以下在项目根目录 PowerShell 执行。无需启动 Docker 或调用 LLM。此例仅用不透明 ID，不把故障名放进候选路径。

```powershell
$aegisPython = '.\.venv\Scripts\python.exe'
$sourceEpisode = '35c79d11-b916-483c-ace9-4da3b67404ba'

# 私有扫描词表：覆盖已知控制名称，但不指定该 episode 的答案。
# 新 UUID 文件名，已有 private/quarantine 文件保持不动。
New-Item -ItemType Directory -Path data/recordings/review -Force | Out-Null
$termsFile = Join-Path 'data/recordings/review' ('terms-' + [guid]::NewGuid().ToString() + '.json')
$termsJson = @{ injected_flags = @('paymentUnreachable', 'paymentFailure'); scenario_answers = @() } | ConvertTo-Json
New-Item -ItemType File -Path $termsFile -Value $termsJson -ErrorAction Stop | Out-Null

# 1. quarantine → 新 candidate。保留返回的随机候选 ID。
$exportJson = & $aegisPython -m aegis_rca.evaluation sanitize --episode $sourceEpisode --terms-file $termsFile
if ($LASTEXITCODE -ne 0) { throw 'Sanitization failed' }
$export = $exportJson | ConvertFrom-Json
$candidateId = $export.candidate_id
$candidateDir = Join-Path 'data/recordings/candidates' $candidateId
$reviewDir = Join-Path 'data/recordings/review' $candidateId
$export

# 2. 重新扫描并检查 complete/truncated；命中详情仅在 review 区。
& $aegisPython -m aegis_rca.evaluation scan --candidate $candidateId
if ($LASTEXITCODE -ne 0) { throw 'Candidate scan failed' }
Get-Content -LiteralPath (Join-Path $reviewDir 'scan.json')

# 3. 技术回放验收。它不会批准候选，也不会使其自动进入 benchmark。
& $aegisPython -m aegis_rca.cli --mode replay --snapshot (Join-Path $candidateDir 'snapshot.json')
if ($LASTEXITCODE -ne 0) { throw 'Replay failed' }

# 4. 新建人工决定草稿，保留原模板。
$decisionInput = Join-Path $reviewDir 'decision.input.json'
New-Item -ItemType File -Path $decisionInput -Value (Get-Content -LiteralPath (Join-Path $reviewDir 'decision.template.json') -Raw) -ErrorAction Stop | Out-Null
notepad.exe $decisionInput
```

到这里需要**人工检查**候选内容和 scan.json。模板里的 `decision=pending` 不能提交；填写 reviewer，完成完整检查后才设 `full_snapshot_reviewed=true`。逐一处理 dispositions，保留 bundle_sha256/scan_sha256，不要仅为通过审核批量填 non_leakage。根据检查结果将 decision 改为 approve 或 reject。保存并关闭编辑器后执行：

```powershell
# 5. 提交人工决定；CLI 会重新扫描并核对准入条件。
& $aegisPython -m aegis_rca.evaluation review --candidate $candidateId --decision-file $decisionInput
if ($LASTEXITCODE -ne 0) { throw 'Review rejected; inspect completeness, blockers and dispositions' }

# 6. 使用前再次核对当前文件是否仍与批准内容一致（审核工具，不会准入）。
& $aegisPython -m aegis_rca.evaluation scan --candidate $candidateId

# 7. 正式准入：只接受不透明候选 UUID，复验当前字节与已存储的批准决定。
& $aegisPython -m aegis_rca.evaluation admit --candidate $candidateId
```

只有 `admit` 成功且结果为 `review_status=approved` 且 `benchmark_eligible=true` 才可按实验协议纳入评测。pending、ineligible 或命令失败均不能纳入。`scan` 不会发布候选。命令不自动复制 public 数据、不添加评分 manifest、不启动调查模型。原件/派生候选的分组映射仅供评估者使用，所有派生版本必须保持相同 split。

## 本轮验证结果

新增测试全部使用合成数据与 tmp_path。没有测试读取项目实际 data/recordings/private 或 quarantine。覆盖结构清洗、业务字段保留、原件不变、回放、确定性、独立输出、候选命名、扫描上限/失败、人工 gate、摘要失效、目录逃逸、符号链接和 junction 检查。

针对性测试：**84 passed, 1 skipped**（清洗、评分、输出保护三个测试文件）。最终完整 suite：**303 passed, 1 skipped in 5.55s**；`pip check` 正常。新增测试总计 63 项，其中 62 通过。跳过项为本机缺少创建真实 Windows symlink 的权限；reparse-point 拒绝分支另有无需该权限的测试通过。

删除清单补充回归：另新增 6 项，覆盖事件/属性/日志的原始位置、秘密键和值不进入清单、清单确定性、私有落盘、公开产物与修改前字节兼容、空清单。该补充的针对性测试为 **68 passed, 1 skipped**（test_sanitization.py），完整 suite 为 **309 passed, 1 skipped**。跳过项仍为上述 Windows symlink 权限限制。

第一次运行遇到已有 pytest 临时目录权限错误，最终使用全新的项目内 basetemp，不修改系统权限，也不清理他人的测试目录。复现完整测试：

```powershell
$aegisTestBase = Join-Path (Resolve-Path artifacts).Path ('pytest-check-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $aegisTestBase) { throw 'Test directory must be new' }
.\.venv\Scripts\python.exe -m pytest -q --basetemp $aegisTestBase
```

另外进行了**测试套件之外**的本地真实录制验收：

| 候选 UUID | 删除事件 / 额外属性 | 保留 span / log / metric series | 清洗后扫描 | 回放 |
| --- | --- | --- | --- | --- |
| 233f84ca-0d68-46fa-8dc8-00f1edbc0c65 | 18 / 0 | 261 / 4 / 1 | 完整、0 候选、0 结构阻塞 | INCONCLUSIVE，2 轮 / 3 调用 |
| 405cb20f-ab5f-46a5-beaa-3190b26246c7 | 24 / 3 | 383 / 4 / 2 | 完整、0 候选、0 结构阻塞 | INCONCLUSIVE，2 轮 / 3 调用 |

两份原始扫描均触及 100 上限。对 6 个原始 quarantine/private 文件比较 hash，内容未变；日志、指标完整相等，span 的非 attributes 字段与普通顶层属性保持一致。还逐项比较了两份原件与候选的 trace/log/metric ReplayBackend ToolResult，六组结果全部相等。具体原件映射与检查记录仅存于本机 review 区，没有复制真实数据到源码或测试。

两个真实候选目前都保持 **pending_review / benchmark_eligible=false**，尚未替用户作人工批准。回放结论未变不代表准确率验收；本轮只验证清洗行为、数据保留和流程边界。

与批准计划的调整仅为你要求的三点：来源与审核全部采用 sidecar；业务正文/指标不作启发式破坏性改写；触顶或未完成扫描禁止批准。没有 schema 或 Observation/Normalization 改动，也没有通用故障检测器、基线实验或准确率声明。

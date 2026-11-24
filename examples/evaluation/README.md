# 人工评分示例

P1.7a 迁移说明：此目录保留历史 report/label 配对，其中
`payment_connection_failure` 是旧标签。用当前代码重新生成报告时，对应的
私有预期标签应迁移为 `rpc_connection_establishment`，保留调用边及人工评审要求。
评分器继续精确匹配，不在调查策略中加入旧标签别名。详见
[P1.7a 契约与迁移记录](../../docs/dependency-hypotheses.md)。

这里的报告与证据由 `examples/snapshot.synthetic.json` 经当前 replay 调查流程生成。label 是为这份人工示例编写的结构化预期；不是真实故障注入记录，也没有人工独立语义评审。

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.evaluation score examples/evaluation/manifest.synthetic.json --output artifacts/scores.synthetic.json
```

预期 dev 的机制标签精确匹配为 `1/1`，语义人工评审为 `N/A`，validation/test 为 `N/A`。这个数字只说明评分器读懂了这个人工例子，不能作为 RCA 准确率。报告保留 `mode=synthetic_fixture` 和 `underlying_cause=null`。

这里只为演示输入结构而将 label 与报告放在同一目录。正式实验的私有标签需要与调查进程隔离，并遵循 [评测协议](../../docs/evaluation.md)。不要将此目录当作正式测试集或检索知识库。

若需从代码重新生成报告，先运行以下命令，再把新输出目录里的 report.json/evidence.json 与示例比较。CLI 不会自动覆盖此目录。

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode replay --snapshot examples/snapshot.synthetic.json
```

# Agent Eval 与 Golden Cases

本目录保存 Wiki Agent 的第一批行为评估样本。

## 运行边界

评测分为两类：`check.py --summary`、硬约束 harness 和单元测试只依赖仓库内容，
可以在 CI 的干净环境运行；`--verify`、真实 Agent/semantic runner 需要外部 Wiki
目录，semantic runner 还需要 LLM API 配置，因此不应作为无凭据的 CI 步骤。所有
外部路径都必须显式提供，入口会在启动时检查目录/文件并报告完整路径，不会依赖某台
机器上的默认路径。

## 当前批次

`templates/manifest.json` 只定义用户需要填写的字段结构，不包含任何个人笔记数据。
模板内包含一个完整的占位实例：复制后替换 `path/to/source.md`、`REPLACE_WITH_SHA256`、
标题、事实约束和页面路径即可。`templates/qa_manifest.json` 与
`templates/source_manifest.json` 分别对应回答评测和批量 source 评测；字段应保持与
示例相同，样本数量可以按用户数据扩展。

每个样本可填写 `expected_verdict`（`pass`、`fail` 或 `review`）和 `annotation` 人工
标注。汇总时若没有人工标注，只输出实际通过/失败数量；不会把未知真值误算成误报或漏报。
有标注的结果可用统一入口汇总：

```bash
uv run python evals/run.py report qa-result.json stage-result.json
```

报告会输出混淆矩阵、precision、recall 和 F1，供后续扩展更多组件指标。

仓库不复制原始笔记。用户自行构建 manifest 后，通过参数提供笔记根目录：

```bash
export WIKI_NOTEBOOK_ROOT=/path/to/notebook
uv run python evals/run.py --verify
```

如果原文发生变化，hash 校验会失败，避免无意间改变用户自己的评测基准。

## 评估原则

- `must_include` 是摘要必须保留的关键事实，不要求逐字复现完整答案。
- `must_not_invent` 是当前人工确认的高风险外部结论或错误组织方式。
- cluster 的 `expected_pages` 和 `expected_relations` 是组织评估参考，不是唯一允许的页面划分。
- 当前 runner 只做样本完整性和确定性硬约束检查，不自动调用 LLM，也不写入 Wiki。
- 后续真实 LLM runner 应在临时 workspace 中调用现有 Extractor/CompilePipeline，再检查最终文件、scan、Git diff 和事件。

## 阶段级评测

编译运行会在每个源文件的 artifact 目录保存：

- `extract.json`：文档摘要；
- `search.json`：候选页面和原始 LLM 输出；
- `analyze.json`：实体、概念、关系和原始分析；
- `plan.json`：页面目标、`new/update` 决策、理由和原始计划。

使用真实编译的 run 目录评估阶段契约：

```bash
uv run python evals/run.py stages \
  /tmp/wiki-eval/.logs/runs/compile_<run_id> \
  --wiki-dir /tmp/wiki-eval
```

确定性检查包括：候选和目标路径合法、无重复、无幽灵页面、关系引用有效、
关系类型和 disposition 合法、plan 有决策理由。它们不能替代语义判断；页面
相关性、拆分边界和组织合理性仍需第二层 LLM judge 加人工抽查评估。

### LLM Judge

语义评测使用独立的固定 JSON rubric，读取 source、extract、search、analyze、
plan 和页面副本，分别评分 source fidelity、search relevance、analysis quality、
plan quality、page quality，并输出证据、问题、置信度和 `pass/review/fail`。

```bash
uv run python evals/run.py semantic \
  /path/to/wiki/.logs/runs/compile_<run_id> \
  --manifest /path/to/your/source_manifest.json \
  --state /path/to/run/state.json \
  --notebook-root /path/to/notebook \
  --wiki-dir /path/to/wiki \
  --concurrency 1 \
  --output /tmp/wiki-eval-semantic-120.json
```

用户提供的 source manifest 使用 `source-001` 等 id 定位批次 artifact，
并从每条 source 的 `plan.json` 找到最终页面；它不要求为 120 条逐条手写
cluster。用户也可以基于同一模板建立多 cluster 清单：

```bash
WIKI_NOTEBOOK_ROOT=/path/to/notebook \
uv run python evals/run.py semantic \
  /tmp/wiki-eval/.logs/runs/compile_<run_id> \
  --output /tmp/wiki-eval-semantic.json
```

Judge 结果是语义意见，不覆盖确定性失败；`review` 和低置信结果必须人工抽查。
runner 启动时就创建输出文件，并在每条 case 完成后更新；长时间没有完成项时，
可直接查看 `completed/total` 判断是接口等待还是评测仍在运行。

## 增加样本

增加样本时应同时完成：

1. 阅读原文并固定 source 路径。
2. 记录 SHA-256。
3. 写出必须保留的事实。
4. 写出不能从原文推出的高风险结论。
5. 将相关章节放入已有 cluster，或新建 cluster。
6. 人工复核页面边界和关系，但允许多个合理组织方案。

QA 组件回归使用 `templates/qa_manifest.json` 作为格式模板。真实 QA 数据应由用户
在仓外生成并通过 `--manifest` 显式传入。

## 一键离线检查

在项目根目录执行：

```bash
uv run python evals/run.py
```

它先运行评测清单摘要，再运行完整单元测试；只检查评测入口时使用
`uv run python evals/run.py --quick`。这些命令不读取个人笔记，也不调用 LLM。

其他入口使用简短命名：`qa.py` 评估已记录回答，`stages.py` 检查编译阶段契约，
`refine.py` 检查 refine 结果，`semantic.py` 调用 LLM 做语义评估。需要真实 Wiki/API
时，QA 使用 `qa.py live` 子命令，Refine 使用 `refine.py live` 子命令。

# Agent Eval 与 Golden Cases

本目录保存 Wiki Agent 的第一批行为评估样本。

## 当前批次

`golden/manifest.json` 登记了 20 个来自个人笔记的 Markdown 章节，分为：

- 操作系统/进程
- C++ 语言与运行时
- 深度学习/数学
- Agent 架构

仓库不复制原始笔记，只保存相对路径、人工确认的不变量和 SHA-256。运行前设置：

```bash
export WIKI_NOTEBOOK_ROOT=/media/zilan/B44018EE4018B8D6/notebook
uv run python evals/run_eval.py --verify
```

如果原文发生变化，hash 校验会失败，避免无意间改变黄金样本。

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
uv run python evals/eval_stages.py \
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
uv run python evals/run_semantic_eval.py \
  /tmp/wiki-agent-eval-batched-dnqTF7/wiki/.logs/runs/compile_<run_id> \
  --manifest evals/golden/source_manifest_120.json \
  --state /tmp/wiki-agent-eval-batched-dnqTF7/state.json \
  --notebook-root /media/zilan/B44018EE4018B8D6/notebook \
  --wiki-dir /tmp/wiki-agent-eval-batched-dnqTF7/wiki \
  --concurrency 1 \
  --output /tmp/wiki-eval-semantic-120.json
```

`source_manifest_120.json` 使用 `source-001` 等 id 定位批次 artifact，
并从每条 source 的 `plan.json` 找到最终页面；它不要求为 120 条逐条手写
cluster。旧的 20 条人工黄金集仍可通过显式指定 `manifest.json` 运行：

```bash
WIKI_NOTEBOOK_ROOT=/media/zilan/.../notebook \
uv run python evals/run_semantic_eval.py \
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

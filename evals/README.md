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

语义评测只判定最终结果：`groundedness`（页面论断是否受原文支持）和
`coverage`（关键事实是否被覆盖）。确定性检查另外验证页面存在、frontmatter、
引用来源和 Wiki 结构。search/analyze/plan 只用于失败归因，不作为通过标准。
每个主观断言都是 `supported/unsupported/unknown` 或
`covered/missing/unknown` 三值判定，`unknown` 不被偷偷计入分母。

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

## 私有种子集与快照评测

从当前 Wiki 的 provenance 构建 30 条可追溯种子样本：

```bash
uv run python evals/run.py dataset \
  --wiki-dir wiki \
  --provenance-dir workspace/provenance/sources \
  --limit 30 \
  --output workspace/evals/wiki-golden-30.json
```

自动产生的事实保持与 provenance 逐字一致，但只是「来源可验证种子集」；
在人工抽查 20–30 条并校准 judge 之前，不应标记为 golden dataset。快照评测
默认每题运行 3 次，同时输出 `pass_at_1` 和更严格的 `pass_power_k`：

```bash
uv run python evals/run.py snapshot workspace/evals/wiki-golden-30.json \
  --repeats 3 \
  --output workspace/evals/results/wiki-snapshot.json
```

评测的 LLM 请求/响应、span 和运行元数据保存在输出目录下的
`traces/` 中，便于区分「系统失败」和「评分器失败」。

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

在项目根目录执行（不调用 LLM、不读个人笔记）：

```bash
make eval-all            # = eval-dataset + eval-adversarial（生成数据集+对抗自检）
uv run python -m pytest -q   # 评测器自身的单测（含评分器自检、合并判定、对抗牙齿）
```

离线校验单个数据集：`uv run python evals/run.py validate workspace/evals/wiki-golden-30.json`。
需要真实 LLM 的 `snapshot`/`agreement` 见下方流水线（会调用你 env/.env 的 key）。

其他入口使用简短命名：`qa.py` 评估已记录回答，`stages.py` 检查编译阶段契约，
`refine.py` 检查 refine 结果，`semantic.py` 调用 LLM 做语义评估。需要真实 Wiki/API
时，QA 使用 `qa.py live` 子命令，Refine 使用 `refine.py live` 子命令。

## 评测标准与统一流水线（v3 重建 · 七准则）

> v1（`core/harness.py`、`commands/check.py`、`commands/semantic.py`、`templates/manifest.json`、
> `source_manifest.json`、`scripts/materialize_source_manifest.py`）已在统一重建中删除。上文旧的
> `--verify`/`semantic` 命令示例为历史，以本节为准。

**分层判定 = A ∧ B**：确定性结构门禁(A) 与 二元 LLM 语义 judge(B) 都判好才 pass。
实测单一 LLM judge 只抓到 5/12 注入缺陷、对人机金标一致率 81%（对结构缺陷全盲）；
**合并 A∧B 后 12/12 检出、一致率 ~97.6%** —— 分层互补的实证。

- **A 确定性代码门禁**（CI-safe，不 gate 的除外）：
  - compile：`outcomes.integrity_metrics`（页存在+非空 sources+来源同一性腿+`check_page_quality`）、
    `compile_graders.compile_structure_checks`（**过度拆分**：同来源页 title+首段 token-Jaccard>0.6；
    **路由**：非 concepts/entities/topics/sources）、`dataset.score_refusal`（应拒答却产出=坏）。
  - QA：`qa_harness.score_answer` —— must_include/forbidden、required/forbidden_tools、
    **引用精确率+召回率**（cited∉existing=精确率扣分；expected−cited=召回率缺口）。
  - refine：`refine_harness` 含**诚实 no-op**（无变化却声称 update=坏）。
  - stage：`stage_harness`（search/analyze/plan 契约）**非门控诊断**（`gating=False`，原则2 评结果非路径）。
- **B 二元 LLM judge**（temp0、严格 JSON、unknown 弃权不入分母、不评路径）：
  `semantic_judge`（compile grounding/coverage）、`qa_semantic_judge`（answer 各维 pass/fail/unknown +
  逐论断 grounding + **citation_support**）、`refine_semantic_judge`（gap_status）。
- **C 人工金标**（只校准 B）：`human_verdict` 须开发者裁决（`label.py` 工作表/回写）；
  `agreement.py` 跑 judge↔人（含对抗例）→ `results.aggregate_reports` 出混淆矩阵/P/R/F1。**≥85% 门槛**。

**统一 v3 数据集**：`task_type ∈ {compile_outcome, qa_outcome, refine_outcome, stage_contract}`、
`suite ∈ {capability, regression, adversarial}`、`polarity`、`reference_solution`（任务自带可解证明）、
`human_verdict`。种子正例(capability)+应拒答负例(regression)+注入缺陷对抗例(adversarial, gold=fail by 构造)。
`pass^k`：上报 k=3，pass@1 与 pass^k 并列。

**评分器自检**（离线，证明"有牙齿"）：`dataset.assert_all_references_pass`（参考解全过 A 层）+
`commands/adversarial`（注入 drop/strip/refuse/over_split/misroute 必被 A 层抓；fabricate 故意留给 judge）
+ `test_eval_combined`（结构缺陷纵使 judge 被骗也判 fail）。

**流水线**（`run.py`/`Makefile`；仅 snapshot、agreement 需真实 LLM 与费用）：
```
make eval-dataset        # 生成 v3 数据集 + 自检（离线）
make eval-validate       # 校验数据集 schema + 参考解过确定性门禁（离线）
make eval-adversarial    # 注入缺陷 → A 层结构门禁自检（离线）
make eval-label          # 交互逐条引导人工金标（写 ratify.json，可续标）
make eval-apply-labels   # 回写已签核金标进数据集
make eval-agreement      # judge↔人（+对抗）一致率 → evals/reports/<date>/agreement.json  [LLM]
make eval-snapshot       # 分层判定 + pass^k（repeats=3）→ workspace/evals/snapshot-pass3.json  [LLM]
make eval-headline       # 汇总 → evals/reports/<date>/HEADLINE.md + manifest.json（离线）
make eval-all            # eval-dataset + eval-adversarial（离线）
```

**当前证据**（`evals/reports/`，可含事实原文）：数据集 42 任务（30 正 + 12 应拒答），
参考解确定性自检 42/42 全过；agreement% 与 pass^3 待你在本机 key + 签核金标后 `make` 产出。

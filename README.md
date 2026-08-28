# wiki_agent

LLM 驱动的个人知识编译器——把杂乱的学习笔记编译成结构化、可关联、可持续演化的 Wiki 知识库。

不只是"生成 Markdown"：笔记 → **摘要（extract）** → **关系分析（analyze）** → **编辑决策（plan）** → **页面生成（execute）**，每一步都由 LLM 完成、由代码层层把关，产出的 wiki 能随新笔记的到来持续增量演化。

---

## 三种使用模式

| 模式 | 命令 | 输入 → 输出 | 用途 |
|------|------|------------|------|
| **compile** | `scripts/compile_folder.py <源文件夹>` | 源笔记 → wiki | 初次建库 / 全量重建 |
| **watch** | `scripts/watch_folder.py <源文件夹>` | 源目录变更 → 自动增量编译 | 持续积累（日常模式） |
| **refine** | `scripts/refine_wiki.py` | wiki 页面自身 → 自身 | 全文 index 建立后刷新交叉引用（执行前自动备份全部页面） |
| **surgery** | `scripts/surgery_wiki.py [--dry-run\|--yes]` | wiki 全库 → 结构手术 | 合并重复页 / 删除垃圾页（两段 LLM 裁决 + 人工确认 + 自动备份） |

另有基于 wiki 的问答 CLI：`uv run wiki-agent`（ReAct agent + wiki 探索工具）。

---

## 架构总览

```
                     compile 管线（单文件）
┌────────────────────────────────────────────────────────────────┐
│ DataLoader → Converter(MinerU+VLM) → Chunker                    │  ingestion
│    ↓                                                             │
│ Extractor：chunk → 滚动压缩/均匀分配 → 文档级摘要                 │  compiler
│    ↓                                                             │
│ Integrator：search → analyze → plan → execute                   │
│    ├─ search  L1 初筛：摘要 + index → 候选页面                   │
│    ├─ analyze 关系分析：候选页 × 文档 → 两段式输出               │
│    ├─ plan   编辑决策：→ page_targets {new/update, reason}       │
│    └─ execute 落盘：normalize_page（修脏→盖戳→质检）→ 更新 index │
└────────────────────────────────────────────────────────────────┘

三个模式共用同一个单文件管线 CompilePipeline.ingest_one，
差异只在一处声明（mode 参数）：
  compile  mode="compile"  全量 index + 存 source 档案页 + 策展人 prompts
  watch   同 compile（生产消费驱动：轮询 → 队列 → 单 worker）
  refine  mode="refine"    index 排除自身 + 不存档案 + 润色师 prompts（只更新自己）

**refine 数据流**（wiki 自编译——与 compile 的关键差异）:

```
输入: wiki/{concepts,entities,topics}/*.md（sources/系统文件排除）
      ↓ 执行前备份全部页面 → runs/refine_<ts>/backup/（可回滚）
每页串行 ingest_one(mode="refine"):
      index 视图 = index.md 排除当前页条目（search 不会选中自己）
      ↓
      search → analyze → plan（润色师 prompt: "只更新自己，
      补交叉引用/刷新 gaps/修正 summary"）
      ↓
      ALLOWED_DISPOSITIONS={"update"} 契约校验拦 new
      → _filter_refine_targets 兜底: 非 self-update 丢弃转 noop
      ↓
      normalize_page 定稿 → 落盘（只碰自己）
      ↓
      全库 scan_wiki → scan_report.md
```

**结构手术（surgery）数据流**（合并/删除——独立于 refine 的显式命令）:

```
① 粗提    LLM 见全库 index（紧凑 40-70 行）→ 原子操作提议（merge/delete）
② 复判    每条带涉事页面全文 + 引用计数证据 → confirm/reject + 定方向
③ 消解    代码确定性消解（去重/方向互斥/merge 赢 delete）
          → LLM 复裁剩余冲突 → 无法仲裁的记录 pending_decisions.json（不阻塞）
④ 确认    人逐条 yes/no（--yes 跳过，备份兜底）
⑤ 执行    备份受影响文件 → 原子动作（吸收拼接/三类引用处理/index 清理）
          顺序无关，零死链
```

**wiki 产物结构**：

```
wiki/
├── index.md          # 页面索引（search 的输入、references 的依据）
├── purpose.md        # 建库用途（LLM 决策时的背景）
├── schema.md         # 目录规范（页面类型 → 路由）
├── concepts/ entities/ topics/   # LLM 生成的页面
├── sources/          # 源文档摘要档案页（系统代码创建，LLM 禁止生成）
├── .watch/state.json # watch 状态持久层（哈希指纹 + 两段确认现场）
└── .logs/runs/<ts>/  # 每次运行的完整记录（见"可观测性"）
```

---

## 核心设计决策与由来

每个决策都来自一次真实事故或审计发现。按"**问题 → 方案 → 原则**"记录。

### 1. 阶段职责分离：analyze 只分析，plan 只决策

**问题**：早期的 analyze 同时产出"新建页面建议 + 交叉引用建议"——分析和决策混在一个 prompt 里，两个角色互相干扰：分析不深（忙着出建议）、决策无据（拿到的是结论不是材料）。

**方案**：analyze 输出两段式（自由分析文本 + 结构化 JSON 尾巴），只回答"是什么"（实体/概念/与候选页的 from→to 关系）；plan 是独立的"策展人"，基于分析做 new/update 决策，交叉引用由它派生。

**原则**：**通道要干净，每个阶段只承载一种语义**。这个原则后来多次复用（见 §2、§6）。

### 2. page_targets 里没有 skip——"不操作"不进操作列表

**问题**：早期 plan 输出三分类 {new/update/skip}，执行层无差别执行所有 target。被 skip 的占位文档（如内容为"请粘贴原文"的 review.json）生成了垃圾页面，甚至空路径规范化出 `wiki/.md` 幽灵文件。事故根因：一个结构承载了两种语义——"要做的操作"和"考虑过但不做"。

**方案**：`Disposition` 只留 NEW/UPDATE；不操作 = 空数组 + `plan_noop` 事件（审计通道）。占位/空内容源文件在上游断掉（DataLoader 拦空文件 → extract 无产出 → plan 空 targets）。

**原则**：**通道语义单一化**——审计走事件流（emit_event），执行指令走 targets；混用类型系统表达不了的东西，运行时一定反噬。

### 3. IngestError：一个异常类，阶段是字段不是类型

**问题**：计划为每个阶段建异常类（PlanError/ConvertError…），但各阶段失败的处理策略完全相同（记录 → 跳过 → 汇总）。SkipFileError 这类"策略写在类型名里"的异常，换个入口（想要中断而非跳过）就名不副实。

**方案**：`IngestError(stage=IngestStage.PLAN, source=..., cause=..., raw=...)`——stage 是枚举字段。异常对象自己就是汇总记录（含 LLM 原始输出 raw），边界单点 `_record_failure` 收集，汇总按 stage 分组落 `failed.json`（机器可读，重试功能输入）。

**原则**：**抽象形状匹配问题形状**——策略相同就不建类型差异，差异在数据不在类型。

### 4. LLM 输出防护：六层纵深，不押注 prompt 100% 合规

**问题**：LLM 输出四类故障——JSON 格式异常（fence 包裹/未转义换行/截断）、页面格式异常（stray fence）、精确数据偏移（日期/路径/sources 写错）、信息断层（plan 的 reason 是复读机，生成阶段不知道做什么）。

**方案**（六层）：

| 层 | 机制 | 例子 |
|----|------|------|
| Prompt | few-shot + 负例 + 字段禁止 | "不要用 ```json 包裹" |
| Retry 校验 | `async_invoke_with_retry` + check 回调，坏输出追加修正消息重试 | `_check_plan_json` 字段级校验 |
| 解析容错 | 剥 fence 后解析（fence 是格式化噪声不是内容错误） | `_check_plan_json`/`_parse_plan` 同款容错 |
| 代码注入 | 精确数据代码写，LLM 写了也覆盖 | created/updated/sources（§5） |
| 归一化 | 页面定稿链 fix → inject → check | `normalize_page`（§6） |
| 兜底过滤 | 校验通过但语义违规的输出，执行前丢弃 | refine 只拿不放（§10） |

**原则**：**永远不在 prompt 层押注 100% 合规，最终鲁棒性来自代码侧逐层容错**。校验错误消息必须可执行（告诉 LLM 错在哪、合法值是什么）。

### 5. 系统权威字段 + related 代码推导：LLM 不写它写不顺的东西

**问题**：两类字段 LLM 反复翻车——精确数据（created 写成训练截止时间、路径前缀叠加、sources 漏写）和 YAML 数组（related 裸名/引号/括号混合错误，全库 56% 缺失或格式坏）。

**方案**：
- `inject_metadata`：created（新页用今天/已有页保留旧值）、updated、sources（去重合并）全部代码注入，无视 LLM 输出
- `extract_related`：related 从正文 wikilink 自动提取（LLM 正文链接写得很顺，YAML 数组写得很烂——用顺的那条路）

**原则**：**让 LLM 做它擅长的事，代码接管它不擅长的事**。顺带修掉过一个真 bug：created 的追加路径曾用 today 重置旧页面的创建日期（冒烟测试抓到）。

### 6. normalize 与 quality 分离：写路径与审计路径是两条线

**问题**：后处理文件膨胀成 440 行的"utils"——修 fence、死链、元数据、质检、全库扫描、报告格式化混在一起，还带一个只有一个调用方的泛型管道类 PostProcessPipeline（为未到来的通用性付税，partial 绑定遮蔽数据流）。

**方案**：拆成两个模块，依赖单向：
- `normalize.py`（写路径）：修 LLM 脏 → 盖系统权威戳 → 质检闸门，`normalize_page` 是唯一入口（顺序是领域知识，调用方不组装）
- `quality.py`（审计路径）：check_page_quality / check_dead_links / scan_wiki / format_scan_report

**原则**：**拆的是"路径"不是"种类"**——utils 是无语义的杂物抽屉；按职责拆，命名给语义（normalize/quality 自己说明职责）。

### 7. gaps 字段：数据自描述它缺什么

**问题**：增量编译时 analyze 只有候选页的 title+summary+headings，与新文档的"摘要 vs 摘要"判断关系容易误判；读全文则上下文暴涨，L1 轻量筛选失去意义。

**方案**：每个页面 frontmatter 声明自己的边界——生成时写 gaps（"本页尚未覆盖的内容"），更新时收敛（覆盖的缺口移除），analyze 时读取（文档命中缺口 = extends 的直接依据）。闭环：页面越被补充 gaps 越收敛，gaps 越准后续匹配越准。

**原则**：**数据自描述它缺什么**——后续消费者不用全量扫描就能对齐。类似数据库索引、API 版本声明。

### 8. reasoning 模型教训：thinking 会静默吃光整个输出预算

**问题**：一次全量编译审计发现 58% 的 analyze 响应完全为空（重试无效、静默降级，产出 -40%、白烧 49 分钟）。API 探测实锤根因：deepseek-v4-flash 是 reasoning 模型，思考段（`reasoning_content`）计入 max_tokens——连"1+1=?"都先思考 41 token；复杂 prompt 下模型思考不休，`finish_reason=length`、`content=''`、预算全被思考吃掉。而代码只读 `message.content`，思考被静默丢弃。

**方案**：编译流水线全部 LLM 调用加 `extra_body={"thinking": {"type": "disabled"}}`；`reasoning_content` 捕获进 LLMResponse 供诊断；空响应不再静默——analyze 重试后仍空 raise IngestError，plan 校验不过 raise IngestError（raw 进异常字段）。

**原则**：**编译要的是"写页面"不是"解难题"**——深度推理在这里是纯成本。推理能力留给 agent 侧（cli.py 的问答），编译侧显式关闭。另外：**静默降级是最大敌人**——LLM 输出质量失败必须显式报错，空 targets 只保留"合法输出空数组"一种语义。

### 9. 可观测性：给人看的日志与给机器看的事件分离，按 run 归拢

**问题**：两次事故暴露可观测性断链——① 编译失败的 LLM 错误只走 stderr（未配置 logger），终端一关证据消失；② 300+ 个阶段产物（extract/search/plan…）平铺混在 .logs/，跨 run 对比无从做起；③ 事件通道（emit_event）从未 setup，静默 no-op。

**方案**：每次运行一个目录：

```
wiki/.logs/runs/<ts>/
├── run.log          # 给人看：时间线（log() + wiki_agent logger 全量）
├── events.jsonl     # 给机器看：compile_failure/plan_noop/file_skipped...
├── failed.json      # 失败清单（stage/source/message/cause/raw）——重试功能输入
├── scan_report.md   # 质量扫描报告
└── artifacts/<源文件>/   # 原始材料：extract/search/analysis/plan/pages
```

watch 与 refine 同容器（`watch_<ts>` / `refine_<ts>` 前缀区分）。

**原则**：**分散按类、统一按 run**——不同消费者要不同格式（人读时间线、机器过滤事件、审计要原始材料），但同一次运行的证据必须能在一个目录找到。`diff runs/a runs/b` 即编译差异报告。

### 10. watch 模式：生产消费，队列里装"去抖后的文件"不是原始事件

**问题**：源文件变化要自动重新 ingest，但"大的改动"才值得重跑 LLM（几分钟/文件）。编辑器保存是原子操作（临时文件 + rename），一个保存动作产生 2-4 个文件事件；touch 和微调不该触发。

**方案**：轮询生产消费，三道闸全在生产端：
- **两段确认去抖**：内容与已知状态不同 → 记 pending，同一内容连续 2 个轮询周期才入队（半写文件永远等不到第二轮）
- **变更门**：hash 相同忽略；difflib 相似度 ≥0.7 视为微调跳过；<0.7 走确认入队
- **单 worker 串行消费**：每个文件的 ingest 都读写 index.md，并发会互相覆盖（幽灵 index 的教训）

**持久层**：`wiki/.watch/state.json`（队列不持久，状态文件是持久层）——进程重启后扫描哈希差异 reconcile，断点续传免费获得。

**原则**：**生产者过滤噪音，队列只管缓冲，消费者保持愚蠢**。事件源（轮询/inotify）突发且廉价，消费者（LLM）慢且贵。轮询（5s）对当前规模绰绰有余，源目录大到上千文件再换事件驱动（TODO 已记）。

### 11. prompts 模块组：模块即命名空间，差异面就是文件长度

**问题**：8 个 prompt 散落在 integrate.py/extract.py 的代码中间，找 prompt 要 grep 代码。refine 模式需要不同 prompt 时面临选择：按任务建组会复制 7 个相同函数（复制即漂移）；在函数里加条件判断违背"任务自知"。

**方案**：

```
compiler/prompts/
├── compile.py   # 全量 8 个 prompt + 契约常量 ALLOWED_DISPOSITIONS
└── refine.py    # ~90 行：继承 7 个（import 即继承），plan 完全独立实现
```

- refine 的 plan 是**润色师**（只更新自己），compile 的 plan 是**策展人**（new/update 开放决策）——角色不同，共享文本只会互相污染，所以分离实现而非继承
- **模式契约**（`ALLOWED_DISPOSITIONS`）声明在 prompt 模块里：prompt 文本与允许的操作在同一个文件保持一致，`_check_plan_json(allowed_dispositions=)` 按契约拦截，Integrator 不感知模式

**原则**：**收拢的是"有共享需求的 prompt"，不是所有 prompt**——压缩/agent 类 prompt 与代码逻辑共同演化（依赖 digest 状态、窗口统计），抽出去只会把契约断成两处。等出现第二个消费者再迁。

### 12. refine 只拿不放：页面只更新自己

**问题**：refine 以 wiki 页面为输入时，self 从 index 排除后 LLM 不知道输入是已有页面，必然判定"新文档" → new 自己 → 用摘要重写自己（信息损耗）。最初设计 refine 可更新其他页面（合并），但这带来顺序敏感性（A 页更新影响 B 页输入）和自动合并的危险。

**方案**：**只拿不放**——每页 refine 只碰自己（补交叉引用、刷新 gaps/summary、修正矛盾），不把自己的内容写给其他页。三层防护：prompt 角色引导（润色师）→ 校验按契约拦（refine 只允许 update）→ pipeline 代码兜底（非 self-update 的 target 丢弃）。合并/拆分/删除是显式命令的职责（TODO）。

**收益**：全库 refine 任意顺序、任意断点续跑、天然幂等。

**原则**：**收窄操作面换取顺序无关性**——读别人永远只读，写永远只写自己。

### 13. 结构手术：结构操作是显式命令，不是 refine 的一部分

**问题**：合并/拆分/删除天然违反"只拿不放"（A 合并进 B 就是写 B），塞进 refine 的逐页循环会毁掉顺序无关性；而"逐页问自己该不该存在"产生局部决策，A 决定合并进 B、B 决定合并进 A——全局矛盾无处消解。

**方案**：独立的手术流程，**两步 LLM 精选复判 + 原子执行**：
- **粗提**（LLM 见全库 index，温度 0）：召回有把握的原子操作——容量解法（全库视野用紧凑 index，不把 70 页全文塞进 prompt）
- **复判**（每条带涉事页面全文 + 引用计数证据）：确认/否决/定 merge 方向——实测 10 条粗提被复判滤到 2-3 条，"相关但不同"的页面（概念-实例层级、总览-专题互补）被正确保留
- **依赖消解**（代码 + LLM 复裁）：提议原子化（每条不可再分）→ 代码确定性消解（去重/质量分定方向/merge 赢 delete）→ 剩余冲突 LLM 复裁 → 无法仲裁的记录 pending_decisions.json（不阻塞，归统一决策队列）
- **执行**（代码，顺序无关）：备份 → 吸收拼接（合并 = 确定性拼接章节，不是 LLM 融合）→ 三类引用处理（源页内自引→目标 / 目标页引用→纯文本 / 其他页→目标）→ index 清理。合并后零死链

**原则**：**检测的权力给代码，裁决的权力给 LLM，破坏性操作先备份**——代码不做相似度阈值（只收集证据），LLM 不做文件操作（只出原子提议），人确认是唯一闸门。

---

## 模块地图

| 层 | 模块 | 职责 |
|----|------|------|
| **入口** | `scripts/compile_folder.py` | 全量编译（run 容器 + 失败收集 + 汇总） |
| | `scripts/watch_folder.py` | watch 模式（轮询生产消费） |
| | `scripts/refine_wiki.py` | refine 模式（wiki 自编译，执行前备份全部页面） |
| | `scripts/surgery_wiki.py` | 结构手术（两段 LLM 裁决 + 冲突消解 + 备份 + 原子执行） |
| | `src/wiki_agent/cli.py` | 可安装的 wiki 问答 CLI（`wiki-agent`） |
| **ingestion** | `data_loader.py` | 文件发现/模态分类/多编码/空文件拦截/SHA256 |
| | `converter/` | MinerU 转 Markdown + VLM 图片 caption |
| | `chunker/` | 标题感知语义切分 + 结构化分块 |
| **compiler** | `pipeline.py` | **CompilePipeline**：单文件管线，三模式共用入口（mode 参数收束） |
| | `extract.py` | 摘要（均匀分配/滚动压缩）+ source 档案页 |
| | `integrate.py` | search/analyze/plan/execute + 全部校验函数 |
| | `normalize.py` | 页面定稿：修脏 → 系统字段 → 质检闸门 |
| | `quality.py` | 质检：页面检测 + 全库扫描 + 报告 |
| | `refine.py` | refine 编排（输入范围 + index 排除闭包） |
| | `surgery.py` | 结构手术（粗提/复判/冲突消解/复裁/备份/原子执行） |
| | `prompts/` | prompt 模块组（compile 全量 + refine 覆写） |
| | `models.py` | 数据模型（ExtractResult/PageTarget/Disposition...） |
| **watch** | `watcher.py` | 生产端：轮询 + 两段确认 + 变更门 |
| | `consumer.py` | 消费端：单 worker 串行 ingest + 状态回写 |
| | `state.py` | state.json 持久层 |
| **llm** | `llm.py` | 传输层（OpenAI 兼容，extra_body 透传，reasoning_content 捕获） |
| | `retry.py` | 中间件：输出校验 + 自动重试 |
| **errors** | `errors.py` | 三分类异常 + IngestError（编译链路统一信号） |
| **log** | `logger.py` / `events.py` / `tracer.py` | 人看日志 / 机器看事件 / span 追踪 |
| **config** | `config/` | pydantic-settings 单入口（frozen + fail-fast） |
| **agent** | `agent/` `tools/` `session/` `context/` `hook/` | 问答 CLI：ReAct 循环、wiki 工具、会话、压缩、渲染 |

---

## 关键机制速查

- **索引生命周期**：search 读 index 选候选 → plan 用 index 校验 references → execute 后新页面进 index（生成失败的不进——幽灵页面防线）→ scan_wiki 查 index 幽灵条目
- **系统字段**：created/updated/sources 代码注入，related 从正文 wikilink 提取——页面上的这四行永远不来自 LLM
- **质量闸门**：check_page_quality（空页/无 frontmatter/无正文/过短/代码块未闭合）error 级拒绝落盘；scan_wiki 每轮编译后全库体检
- **失败语义**：IngestError 显式报告（不静默）→ 边界收集 → failed.json；空 targets 只可能是"LLM 合法输出空数组"
- **编译流水线 LLM 调用**：全部 `thinking=disabled`（推理能力留给 agent 侧）
- **设计笔记**：深度设计讨论记录在 `docs/system-design-notes.md`（12 节），待办与优先级在 `TODO.md`

---

## 快速上手

```bash
# 环境（uv 管理）
uv sync

# 初次编译（全量）
VIRTUAL_ENV= .venv/bin/python scripts/compile_folder.py <笔记文件夹>

# 日常持续积累（watch 模式）
VIRTUAL_ENV= .venv/bin/python scripts/watch_folder.py <笔记文件夹>

# 全文 index 建立后刷新交叉引用
VIRTUAL_ENV= .venv/bin/python scripts/refine_wiki.py

# 结构手术（合并重复页/删除垃圾页；--dry-run 只看不动手，--yes 跳过确认）
VIRTUAL_ENV= .venv/bin/python scripts/surgery_wiki.py --dry-run
VIRTUAL_ENV= .venv/bin/python scripts/surgery_wiki.py

# 问答
uv run wiki-agent
```

配置：`env/.env`（API key/模型）+ `env/mcp.json`（MCP server 清单）。LLM 模型默认 `deepseek-v4-flash`（reasoning 模型——编译侧已关闭 thinking，见 §8）。

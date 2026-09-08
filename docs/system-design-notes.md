# 系统建构笔记（System Design Notes）

> 沉淀对话中讨论到的成熟系统建构知识，供回看。随代码同目录演进。

## 当前包边界（2026-09-09）

Python 包按可识别的业务能力直接命名，不设置笼统的 `adapters`、
`infrastructure` 或 `common` 容器：

```text
application/   用例编排与运行时组装
agent/         ReAct 循环与交互命令
compiler/      可解释的知识编译流水线
documents/     原始资料的加载、转换与分块
wiki/          生成页面的规则、路径、导航与质量
conversation/  消息协议、历史规则与会话生命周期
context/       上下文构建、预算治理与压缩
memory/        长期记忆存储与 Dreamer
jobs/          持久化后台任务模型与存储
issues/        面向用户的问题、证据与决策状态
events/        Agent 生命周期钩子与实时事件发布
persistence/   SQLite 连接、事务与后续 schema migration 基础
llm/           模型客户端、重试与全局限流
versioning/    Wiki Git 事务与版本模型
watch/         文件变化发现与 Job handler
web/           HTTP/SSE 入口
```

运行数据使用三个互不包含的根：`materials/` 仅放用户原始资料，
`wiki/` 仅放生成知识，`workspace/` 仅放数据库、会话、日志、任务和溯源记录。

共享 SQLite 连接位于 `persistence/database.py`，但具体 Store 留在所属领域，
例如 `jobs/store.py` 与 `issues/store.py`。这避免了将所有存储实现堆入一个新的基础设施巨石包。

## 模块分层：用「依赖方向」而非「文件大小」驱动拆分（2026-08-28，compiler 重构）

把巨石文件按子包拆开时，真正的约束不是"每个文件多大"，而是**依赖只能单向向下**。
compiler 包确立的分层：

```
models  →  wiki (无 LLM 底层)  →  integration / surgery  →  workflows
```

下层绝不 import 上层。几个可复用的判断：

- **先定"底层"再搬家**：`models` 与 `wiki` 被钉为"无 LLM 的底层"，于是"页面级判定
  （fence 状态机 / wikilink / frontmatter / 页面闸门）"全部下沉到 `wiki/rules`，
  `integration` 的 execute 闸门与 `wiki` 的落盘 scan **复用同一份 `_check_page_output`**。
  这就是"禁止跨层复制校验规则"的落地方式——不是喊口号，而是给判定找一个所有人都能
  向下依赖的家。

- **"单一来源"要先找真正的共享者**：`_strip_fence`（LLM 原始输出清洗）被 integration 和
  surgery 同时需要。它语义上既不是 wiki 也不是 data model。选择让 surgery 直接
  `from integration.parse import _strip_fence`（peer→peer 的一条边），代价是跨层引用；
  好处是绝不复制。**当且仅当依赖图仍无环时才允许 peer 边**——这里 integration 从不反向
  import surgery，所以安全。若哪天需要，应下沉成独立底层工具，而不是复制第二份。

- **共享常量要消灭重复计数**：`_NO_THINKING` 一度有 4 份拷贝（checks / stages /
  surgery / extraction）。收进 `models.py` 单一来源。重复常量比重复函数更阴险——它不会
  报错，只会某天在"改了一份、漏了三份"时引发不一致。

- **迁移期用 facade 保持绿**：先把真实代码搬进新子包、把旧路径改成 re-export facade，
  每搬一个巨石就跑全量测试；等所有内部/外部/测试消费者都改指新真实路径、facade 零消费者
  之后，再单独一次删除 facade。**搬家和删除分成两个 PR**——中间任何一步都能独立回滚，
  diff 里只有"位移 + import 改写"，绝不混入行为改动。

- **拆分前先画调用图**：surgery 的 `execute → {common, resolve, rewrite, transaction}`，
  且 `resolve ↛ execute`、`review ↛ resolve`、LLM 只出现在 proposal/review、备份只在
  transaction——这些"谁调谁"的边必须在动手前列清楚，否则极易拆出循环 import 或把 LLM
  逻辑漏进本该确定性的模块。

## 拆分时的隐藏引用陷阱（2026-08-28）

顶层 grep import 语句不足以发现全部跨模块引用，实跑发现三类"只在调用时才炸"的暗雷：

1. **函数体内的延迟 import**：`surgery/_load_pages` 里有 `from compiler.parse import
   split_frontmatter`，import 期不报错、调用期才 ImportError。搬模块时函数体 import 也要扫。
2. **包内部反向引用旧 facade**：`wiki/normalize` 曾 import 根 `quality` facade，把根
   facade 降级后若不改成兄弟模块 `wiki.quality`，wiki 会自毁（下层依赖了将死的兼容层）。
3. **公开 API 漏导出**：`surgery/__init__` 一直没导出 `_index_overview`，导致
   `scripts/surgery_wiki.py` 早已 `ImportError`——拆分正好是给这类"facade 面"补齐的时机。

教训：**以"能否被独立 import + 冒烟 CLI 跑通"为验收**，而不是"import 语句 grep 干净"。

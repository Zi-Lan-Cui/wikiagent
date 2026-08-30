# wiki-agent

一个基于 LLM 的本地知识库编译与问答工具。它将源材料整理为结构化 Wiki，并提供增量更新、质量检查和基于 Wiki 的问答能力。

## 为什么使用 wiki-agent

wiki-agent 面向的是“资料越来越多，但不想花时间维护知识库”的用户。它关注的不是生成多少文档，而是让知识库长期好用：

- **少整理**：把零散笔记、文档和资料交给它，自动归纳主题、建立关联并整理成可浏览的 Wiki。
- **持续更新**：新资料加入后只处理受影响的内容，不必反复重做整个知识库。
- **更容易找得到**：页面有统一分类、索引和交叉链接，减少“记得看过但找不到”的情况。
- **回答有依据**：问答会先查 Wiki，再给出答案和引用位置，方便核对，而不是只给一段无法追溯的生成文本。
- **修改更放心**：整理和修订前会保留运行记录，出现问题可以查看、重试或回退。
- **本地可控**：资料和生成的 Wiki 保存在自己的环境中，模型服务、路径、并发和成本都可以自行控制。
- **适合逐步使用**：可以先整理一个小目录，确认效果后再扩大范围，不要求一次性迁移全部资料。

## 你可以用它做什么

- 把资料整理成结构清晰的 Wiki
- 自动跟踪资料变化并更新相关页面
- 发现重复、缺失关联或需要补充的内容
- 在确认和备份保护下调整页面结构
- 通过 `wiki-agent` CLI 查询自己的知识库
- 查看每次处理的结果、失败原因和修改记录

## 安装

项目使用 `uv` 管理依赖：

```bash
uv sync
```

配置文件位于 `env/`。请在本地填写模型服务和路径配置，不要将包含密钥或个人路径的文件提交到仓库。

首次配置可复制模板：

```bash
cp env/.env.example env/.env
```

所有可调运行参数（模型、reasoning、预算、并发、watch 时间窗和重试策略）都集中在这个文件中；
`RootConfig` 只负责类型定义、优先级合并和启动校验。

问答模型默认保留 reasoning。可通过 `LLM_THINKING=enabled|disabled` 控制；启用 reasoning 时，
请同时保证 `AGENT_MAX_TOKENS` 足够覆盖思考和最终回答。编译阶段会按各阶段契约显式关闭 reasoning。

## 使用

安装后可运行：

```bash
uv run wiki-agent --help
```

编译、watch、refine 等开发和运维入口位于 `scripts/`，评测入口位于 `evals/run.py`。这些命令都接受路径参数或读取统一配置，不要求修改源码中的路径字符串。

### 1. 编译资料

将一个源目录编译成 Wiki：

```bash
uv run python scripts/compile_folder.py /path/to/source-folder
```

默认使用配置中的 Wiki 目录。运行结束后会生成页面、索引、来源记录和本次运行日志；如果某个文件失败，会记录在运行目录中，不会悄悄跳过。

### 2. 持续监测

需要持续接收新资料时：

```bash
uv run python scripts/watch_folder.py /path/to/source-folder
```

watch 会检测新增和修改的文件，并只重新处理有实际变化的内容。使用 `Ctrl-C` 停止即可，下一次启动会继续根据状态文件检查变化。

### 3. 优化已有 Wiki

对已有页面进行摘要、关联和缺口修订：

```bash
uv run python scripts/refine_wiki.py \
  --wiki-dir /path/to/wiki \
  --project-root /path/to/project
```

可以用 `--limit N` 先处理少量页面。Refine 默认只更新当前页面，并在运行前保存备份。

### 4. 调整页面结构

合并重复页面或整理结构前，建议先预览：

```bash
uv run python scripts/surgery_wiki.py --dry-run
```

确认提议后再执行：

```bash
uv run python scripts/surgery_wiki.py
```

`--yes` 可用于自动确认，但只建议在已经检查过预览和备份策略后使用。

### 5. 使用问答助手

在 Wiki 上进行交互式问答：

```bash
uv run wiki-agent
```

常用选项：

```bash
uv run wiki-agent --list              # 查看已有会话
uv run wiki-agent --resume SESSION_ID # 恢复会话
uv run wiki-agent --debug              # 保存调试日志和事件
```

输入 `/q`、`/quit` 或 `/exit` 结束当前会话。

### 6. 查看运行记录

每次处理都会在 Wiki 的 `.logs/runs/` 下保存独立运行目录，通常包括：

- `run.log`：人类可读的运行日志
- `events.jsonl`：机器可读的事件流
- `failed.json`：失败来源及原因
- `scan_report.md`：页面和链接质量报告
- `artifacts/`：阶段产物和原始响应

如果失败是由网络波动、服务限流等偶然因素引起，可以使用 retry 对失败文件重新尝试；持续失败时再查看最近一次运行目录中的错误记录。

## 使用体验

```text
资料 → 自动整理 → Wiki → 持续更新 → 可引用问答
```

你可以把它当作一个会持续维护的个人知识库：平时只需放入新资料，需要时直接提问；重要修改仍然可以在写入前确认，并在出现问题时回退。

## 开发检查

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv build
```

评测框架只提供格式模板和执行器，不包含个人资料或固定数据集。用户可根据 `evals/templates/` 自行建立 manifest，并通过 `evals/run.py` 执行检查和汇总。

## 项目结构

```text
src/wiki_agent/   核心运行时代码
scripts/          编译、watch、refine 等运维入口
evals/            评测入口、模板和结果汇总
test/             自动化测试
docs/             设计记录
```

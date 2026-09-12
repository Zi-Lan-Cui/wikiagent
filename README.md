# wiki-agent

一个基于 LLM 的本地知识库编译与问答工具。它将源材料整理为结构化 Wiki，并提供增量更新、质量检查和基于 Wiki 的问答能力。

## 为什么使用 wiki-agent

wiki-agent 面向"资料越来越多，但不想花时间维护知识库"的用户。它关注的不是生成多少文档，而是让知识库长期好用：

- **少整理**：把零散笔记、文档和资料交给它，自动归纳主题、建立关联并整理成可浏览的 Wiki。
- **持续更新**：新资料加入后只处理受影响的内容，不必反复重做整个知识库。
- **更容易找得到**：页面有统一分类、索引和交叉链接，减少"记得看过但找不到"的情况。
- **回答有依据**：问答会先查 Wiki，再给出答案和引用位置，方便核对。
- **修改更放心**：整理和修订前会保留运行记录，出现问题可以查看、重试或回退。
- **本地可控**：资料和生成的 Wiki 保存在自己的环境中，模型服务、路径、并发和成本都可以自行控制。
- **适合逐步使用**：可以先整理一个小目录，确认效果后再扩大范围。

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

配置文件位于 `env/`，请先在本地填写模型服务和路径配置，不要提交包含密钥的文件。

## 使用

### 1. 编译资料

将一个源目录编译成 Wiki：

```bash
uv run python scripts/compile_sources.py /path/to/source-folder
```

省略资料目录时使用配置的默认目录。知识页写入 `wiki/`，来源记录和运行日志写入 `workspace/`。

### 2. 持续监测

持续接收新资料：

```bash
uv run python scripts/watch_folder.py /path/to/source-folder
```

watch 会检测新增和修改的文件，只重新处理有实际变化的内容。

### 3. 优化已有 Wiki

对已有页面进行摘要、关联和缺口修订：

```bash
uv run python scripts/refine_wiki.py --wiki-dir /path/to/wiki
```

可以用 `--limit N` 先处理少量页面。

### 4. 调整页面结构

合并重复页面或整理结构前先预览：

```bash
uv run python scripts/surgery_wiki.py --dry-run
```

确认后再执行 `uv run python scripts/surgery_wiki.py`。

### 5. 使用问答助手

在 Wiki 上进行交互式问答：

```bash
uv run wiki-agent
```

常用选项：`--list` 查看已有会话，`--resume SESSION_ID` 恢复会话，`--debug` 保存调试日志。输入 `/q`、`/quit` 或 `/exit` 结束当前会话。

### 6. 使用 Web 工作台

启动本地 Web 界面，在浏览器中浏览 Wiki、进行问答并处理问题队列：

```bash
uv run uvicorn wiki_agent.web.app:create_app --factory --port 8000
```

打开 http://localhost:8000 即可使用。界面包含三个区域：

- **对话**：与 Wiki 进行可引用问答，支持多会话管理
- **Wiki 浏览**：查看生成的知识页面、目录索引与交叉链接，支持前后导航
- **工作台**：查看后台任务执行进度，处理编译失败与质量提醒（支持重试、处置等操作）

## 数据目录

```text
materials/     用户提供的原始资料
wiki/          仅保存可发布的生成知识页
workspace/     运行状态、日志、溯源记录和会话数据
```

三个目录必须互不包含，可分别通过 `WIKI_MATERIALS_DIR`、`WIKI_WIKI_DIR` 和 `WIKI_WORKSPACE_DIR` 配置。

## 开发检查

```bash
uv run ruff check .
uv run pyright
uv run pytest
```

## 项目结构

```text
src/wiki_agent/   核心运行时代码（按业务领域组织）
scripts/          编译、watch、refine 等运维入口
evals/            评测框架与判词题集
test/             自动化测试
docs/             设计记录
```

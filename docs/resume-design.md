# 编译中断恢复设计

本文定义 Wiki 编译任务在进程取消、崩溃或输入文件变化后的恢复契约。

## 目标与边界

恢复能力面向单用户、单进程版本。状态文件位于 Wiki 目录之外，不成为 Wiki 内容；Git 仍负责一次批次的事务提交和回滚。恢复事实以 source 为单位，batch 只是执行分组和审计视图。

## 状态模型

```text
RunState
├── run_id / manifest_hash / root / wiki_dir
├── source_state[source_id]
│   ├── path / sha256 / size
│   ├── status / completed_stage
│   ├── run_dir / error / timestamps
└── batches[]
    ├── id / source_ids
    └── status / commit / timestamps
```

`source_state` 是唯一恢复事实来源。`batches` 可按当前 manifest 重新计算，不得在 batch 中复制另一份 source 状态。

`batch-size` 只控制执行分组；`commit-scope` 独立控制 Git 边界：`source`（每个 source）、`batch`（默认，每个 batch）或 `run`（整个运行）。因此文件列表变化时可重建 batch，而不会改变 source 的恢复身份。

## 输入变化策略

恢复前对当前 manifest 和 source 重新计算指纹，并生成差异：

| 变化 | 默认处理 |
| --- | --- |
| source ID、路径、hash 均未变 | 复用已完成状态；不复制旧产物 |
| 内容 hash 变化 | 标记 `modified`，从头处理 |
| 新 source | 标记 `pending`，加入当前 batch 计划 |
| source 被删除 | 标记 `removed`，不自动删除 Wiki 页面 |
| 仅路径变化 | 默认按删除+新增处理 |

`--resume` 只执行安全恢复；发现输入变化时停止并报告。`--reconcile` 是显式确认入口，确认后按 source 状态重建 batch 计划。删除 source 不自动删除 Wiki 页面。

## 生命周期

```text
pending → running → committed
                   ↘ failed
                   ↘ interrupted
```

取消保存 `interrupted` 状态。`cancel` 不等同于 `abort`：前者保留恢复信息，后者需要用户明确要求并回滚当前 Git 事务。

## 入口与验收

- `compile_manifest.py`：状态创建、指纹校验、source 恢复和 batch 重建。
- `compile_sources.py`：阶段 checkpoint、取消记录和幂等 source 写入；`compile_folder.py` 仅为旧调用保留兼容包装。
- `GitManager`：active/interrupted/committed/aborted 生命周期与 stale lock 防护。
- CLI/Web：状态查询、resume、reconcile、cancel、abort。
- 测试：中断恢复、增删改文件、batch-size 变化、manifest 变化、锁恢复和重复执行。

数据库不属于当前阶段；多进程、多用户或多实例部署时再迁移到 SQLite/共享存储。由于 refine 可能改变 Wiki 结构，reconcile 不复用旧页面产物，而是只依据 source 状态跳过已完成输入并重新执行需要更新的批次。

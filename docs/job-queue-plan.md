# 统一任务调度改造计划

## 目标

统一 watcher、CLI 编译、Web 重试和后续 refine 的执行入口，明确区分：

- **Job**：需要执行的工作，具有排队、进度、重试、取消和恢复状态。
- **Issue**：执行失败后需要用户处理的问题，保存在 `state.db`。
- **Event**：面向前端或其他订阅者的实时通知，不作为可靠状态来源。
- **WatchState**：文件指纹与变更检测游标，不作为任务队列。

## 实施阶段

1. 在 `state.db` 增加持久化 Job 模型、任务表和状态迁移；实现提交、查询、认领、完成、失败、取消与恢复。
2. 增加统一 `JobService`/`WorkCoordinator` 与单 Worker，记录当前阶段、尝试次数、时间和错误信息。
3. 将 watcher 改为只负责发现和确认文件变化，并通过统一 Job 服务提交 `compile`/`delete` 任务；consumer 改为 Job handler。
4. 将 Web/CLI 的 retry、rescan、compile 入口迁移到统一 Job 服务，逐步移除内存 `IssueTaskManager` 队列。
5. 增加服务重启恢复、任务幂等键、重复提交去重和并发认领保护。
6. 将工作台改为读取持久化 Job，将问题中心继续作为 Issue 的用户决策界面；EventPublisher 只负责实时更新。
7. 补充 watcher→Job、重启恢复、重复事件、重试生成新 Job、Issue 与 Job 解耦等回归测试。

## 设计边界

不把 `watcher.py` 与执行逻辑简单合并。watcher 负责检测，Job 服务负责调度，worker/handler 负责执行，Issue 服务负责失败记录。`WatchState` 暂时保留为文件检测游标，后续可迁移为数据库表。


"""watch 模式——生产消费监控源目录变化，大改动自动重新 ingest。

- watcher.py: 生产端（轮询扫描 → 两段确认去抖 → 变更门 → 队列）
- consumer.py: 消费端（单 worker 串行 ingest → 状态回写）
- state.py:    state.json 持久层（队列不持久，状态文件是持久层）
"""

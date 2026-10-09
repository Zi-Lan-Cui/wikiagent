"""sync 域——同步状态持久层与源文件任务执行体。

- state：文件处理状态（hash/text）与快照对比（scan_disk/diff）；
- coordinator：sync 线提交入口，submit_sync 做一次性快照并整批入队；
- source_jobs：compile/delete Job 的 handler，执行 ingest 与溯源清理，
  成功时在结果中携带写入状态的凭证。
"""

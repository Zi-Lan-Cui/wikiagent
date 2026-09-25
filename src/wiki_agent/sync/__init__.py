"""sync 域——快照账本与源文件编译的执行体。

- 状态持久层（state）: 完成账（hash/text）+ 快照两半（scan_disk/diff）
- 执行体（source_jobs）: compile/delete Job 的 handler——ingest 执行、
  溯源清理，成功才携带核账凭证

发现变更不在此域——那是提交入口 JobService.submit_sync 的一次性快照。
"""

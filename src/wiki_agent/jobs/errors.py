"""Job 存储层错误。"""

from __future__ import annotations


class DuplicateActiveJob(RuntimeError):
    """不变式 I1 冲突：同一 resource 已存在 queued/running 的在途 Job。

    resource 统一为绝对路径字符串——compile/delete/issue_retry 同族共享
    这一身份，部分唯一索引 uq_jobs_active_resource 是强制点。
    """

    def __init__(self, resource: str):
        super().__init__(f"resource 已有在途 Job: {resource}")
        self.resource = resource

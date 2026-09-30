"""重试输入的解析——按失败记录定位"要重新编译哪个文件"（纯函数）。

只在提交时刻被调用：submit_issue_retry 把解析结果当 job 的 resource 并
据此捕获快照；IssueActionExecutor 用它筛选可重试问题、标记不可用。
执行与结算不经过这里——重试 job 就是一个普通 compile job。

重试的来源只有原始输入文件：ingestion_failure 的 source_path 是失败当时
定格的源路径。原始来源不能猜测——丢失时必须由用户重新提供。
"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.issues import IssueRecord


class SourceUnavailableError(FileNotFoundError):
    """失败记录所指向的输入已经无法恢复。"""


def resolve_retry_source(issue: IssueRecord) -> Path:
    """从问题记录解析当前可用的重试输入（原始源文件路径）。

    Args:
        issue: 挂重试账的失败记录。

    Returns:
        现存的源文件绝对路径。

    Raises:
        SourceUnavailableError: 原始来源已不存在。
    """
    raw_path = str(issue.context.get("source_path") or "").strip()
    if raw_path:
        # source_path 由提交口写 resolve() 后的绝对路径；相对形态不再兜底
        # （按 cwd 解析会随启动方式漂移，命中即错账）
        candidate = Path(raw_path)
        if candidate.is_absolute() and candidate.is_file():
            return candidate
    label = str(issue.resource.get("path") or "").strip() or raw_path or "未知来源"
    raise SourceUnavailableError(f"原始来源已不存在，请重新提供后再编译: {Path(label).name}")

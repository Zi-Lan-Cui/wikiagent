"""重试输入的解析——按失败记录定位"要重新编译哪个文件"（纯函数）。

只在提交时刻被调用：submit_issue_retry 把解析结果当 job 的 resource 并
据此捕获快照；IssueActionExecutor 用它筛选可重试问题、标记不可用。
执行与结算不经过这里——重试 job 就是一个普通 compile job。
"""

from __future__ import annotations

from pathlib import Path

from wiki_agent.issues import IssueRecord


class SourceUnavailableError(FileNotFoundError):
    """失败记录所指向的输入已经无法恢复。"""


def resolve_retry_source(issue: IssueRecord, wiki_dir: str | Path) -> Path:
    """从问题记录解析当前可用的重试输入。

    refine 的输入是 Wiki 页面。旧评测记录可能保留了已消失的
    ``/tmp`` 绝对路径，此时允许用公开页面名在当前 Wiki 中唯一
    定位。compile 输入不能猜测，原始来源丢失时必须由用户重新提供。
    """
    wiki = Path(wiki_dir).resolve()
    raw_path = str(issue.context.get("source_path") or "").strip()
    source_kind = str(issue.resource.get("type") or "input_file")
    public_name = str(issue.resource.get("path") or "").strip()

    if raw_path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if candidate.is_file():
            if source_kind != "wiki_page":
                return candidate
            try:
                candidate.relative_to(wiki)
            except ValueError:
                pass
            else:
                return candidate

    if source_kind == "wiki_page" and public_name:
        relative = Path(public_name)
        direct = (wiki / relative).resolve()
        try:
            direct.relative_to(wiki)
        except ValueError:
            direct = wiki / "__invalid__"
        if direct.is_file():
            return direct
        matches = [
            path.resolve()
            for folder in ("concepts", "entities", "topics")
            for path in (wiki / folder).rglob(relative.name)
            if path.is_file()
        ]
        unique = list(dict.fromkeys(matches))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            raise SourceUnavailableError(
                f"Wiki 中存在多个同名页面，无法确定重试对象: {relative.name}"
            )
        raise SourceUnavailableError(f"Wiki 页面已不存在，无法重试: {relative.name}")

    label = public_name or raw_path or "未知来源"
    raise SourceUnavailableError(f"原始来源已不存在，请重新提供后再编译: {Path(label).name}")

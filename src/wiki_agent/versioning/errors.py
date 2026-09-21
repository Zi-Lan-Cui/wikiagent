"""Wiki 版本管理相关异常。"""

from __future__ import annotations


class GitManagerError(RuntimeError):
    """Git 版本管理失败。"""


class GitScopeError(GitManagerError):
    """操作试图越过允许的 Wiki 范围。"""


class GitCommitError(GitManagerError):
    """提交失败。"""

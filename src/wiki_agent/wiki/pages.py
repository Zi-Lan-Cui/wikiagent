"""内容页面目录模型——目录与类型映射的唯一来源（无 LLM，底层）。

wiki 层的检查、compile 的路由校验、维护的名册与装配共用这一份定义；
新增内容目录只改 PAGE_TYPE_BY_DIR。
"""

from __future__ import annotations

PAGE_TYPE_BY_DIR = {"concepts": "concept", "entities": "entity", "topics": "topic"}

CONTENT_DIRS = tuple(PAGE_TYPE_BY_DIR)

TYPE_DIR = {t: d for d, t in PAGE_TYPE_BY_DIR.items()}

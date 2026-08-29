"""Wiki 文本、链接与质量规则。"""

from wiki_agent.compiler.wiki.normalize import normalize_page
from wiki_agent.compiler.wiki.quality import Issue, scan_wiki

__all__ = ["Issue", "normalize_page", "scan_wiki"]

"""上下文构建——system prompt 组装 + history 拼接。

system prompt 五块（build 阶段的核心扩展）:
1. 用户画像       memory.md（Dreamer 加工）
2. wiki 环境      purpose.md（知识库使命）+ schema.md（目录规范）
                  + index.md（页面地图，行截断）
3. 工具描述       tool registry
4. 对话摘要       session.last_summery（压缩产物）
5. 待处理纠错     corrections.md（未处理清单——回答时避开已知错误页）

块顺序 = 变化频率排序（prompt cache 前缀纪律）: 位置 N 的变化
只失效 N 之后——最易变的放最后。摘要变化时机（consolidate）
恰好也是 history 重放窗口变化时机，放中间零额外代价；纠错是
用户事件（独立于 history 变化），放最尾——记录一次纠错只 miss
纠错块自身，工具描述与历史前缀照样命中。
"""

from pathlib import Path

from wiki_agent.tools import ToolRegistry
from wiki_agent.memory import MemoryStore
from wiki_agent.message import Message
from wiki_agent.session import Session


# index 地图的字符上限——按行截断（不切断行），超限提示用工具探索
_WIKI_INDEX_CHARS = 4_000
# 纠错清单的字符上限——通常短，超限才截
_CORRECTIONS_CHARS = 2_000


class ContextBuilder:
    """从 system_prompt, session.history, tool_description 构建初始信息。"""

    def __init__(
            self,
            system_prompt: str,
            tool_registery: ToolRegistry,
            memory_store: MemoryStore,
            wiki_dir: str | Path | None = None,
        ):
        self.system_prompt = system_prompt
        self.tool_registery = tool_registery
        self.memory_store = memory_store
        self.wiki_dir = Path(wiki_dir) if wiki_dir else None

    # ── system prompt 各块 ─────────────────────────────────

    def _load_user_description(self) -> str:
        """用户画像——memory.md（Dreamer 定期加工）。"""
        try:
            content = self.memory_store.memory_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        return content.strip() or "（暂无用户画像——对话积累后由记忆机制生成）"

    def _read_wiki_file(self, name: str) -> str:
        """读 wiki 系统文件——不存在返回空串。"""
        if self.wiki_dir is None:
            return ""
        try:
            return (self.wiki_dir / name).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _load_wiki_context(self) -> str:
        """wiki 环境——purpose（使命）+ schema（规范）+ index（地图）。

        与 compile 管线读同一组系统文件——单一权威，两个消费者
        看到同一份知识库定义。
        """
        if self.wiki_dir is None:
            return "（未接入 wiki 目录）"

        parts: list[str] = []
        purpose = self._read_wiki_file("purpose.md")
        if purpose:
            parts.append(f"## 知识库使命\n{purpose}")
        schema = self._read_wiki_file("schema.md")
        if schema:
            parts.append(f"## 目录规范\n{schema}")

        index = self._read_wiki_file("index.md")
        if index:
            lines = index.splitlines()
            kept: list[str] = []
            total = 0
            for line in lines:
                if total + len(line) + 1 > _WIKI_INDEX_CHARS:
                    break
                kept.append(line)
                total += len(line) + 1
            text = "\n".join(kept)
            if len(kept) < len(lines):
                text += "\n...（地图过长已截断，用 ListDir/Grep 探索其余部分）"
            parts.append(f"## 页面地图\n{text}")
        elif not purpose and not schema:
            parts.append("（wiki 为空库——尚未编译）")

        return "\n\n".join(parts)

    def _load_corrections(self) -> str:
        """待处理纠错——corrections.md（未处理清单，回答时注意避开）。"""
        items = self.memory_store.get_corrections()
        if not items:
            return "（暂无待处理纠错）"
        text = "\n".join(items)
        if len(text) > _CORRECTIONS_CHARS:
            text = text[:_CORRECTIONS_CHARS] + "\n...（纠错清单过长已截断）"
        return text

    def _build_system_prompt(
            self,
            tools_description: str,
            last_summery: str,
        ) -> str:
        return self.system_prompt.format(
            user_description=self._load_user_description(),
            wiki_context=self._load_wiki_context(),
            corrections=self._load_corrections(),
            tools_description=tools_description,
            summery=last_summery,
        )

    def build_messages(
            self,
            session: Session,
            current_message: Message,
            last_summery: str,
            history: list[Message],
        ) -> list[Message]:
        """构建请求消息——system + 历史 + 当前输入。

        职责边界: 只做组装（system prompt 五块 + history 拼接 +
        当前消息追加）。消息治理（合并连续同 role / 孤儿修复 /
        token 截断）是 ContextGovernor.prepare_for_llm 的职责——
        本函数的输出是"原始形态"，治理在发出请求前由 governor 完成。
        """
        messages = [
            Message(
                role="system",
                content=self._build_system_prompt(
                    tools_description=self.tool_registery.get_all_description(),
                    last_summery=last_summery,
                )
            )
        ]
        messages.extend(history)
        messages.append(current_message)

        return messages

"""上下文构建——system prompt 组装 + history 拼接。

system prompt 四块:
1. 用户画像（记忆加工产物）
2. wiki 环境（使命 + 目录规范 + 页面地图）
3. 工具描述
4. 对话摘要（压缩产物）

纠错信息不插入 system prompt，而是作为 history 之后、当前问题之前的
动态消息注入，避免改变稳定 system 前缀。
"""

from pathlib import Path

from wiki_agent.conversation import Message, Session
from wiki_agent.issues import IssueKind, IssueService, IssueStatus
from wiki_agent.memory import MemoryStore
from wiki_agent.tools import ToolRegistry


class ContextBuilder:
    """从 system_prompt, session.history, tool_description 构建初始信息。"""

    def __init__(
        self,
        system_prompt: str,
        tool_registry: ToolRegistry,
        memory_store: MemoryStore,
        issue_service: IssueService | None = None,
        wiki_dir: str | Path | None = None,
        agent_config=None,
    ):
        self.system_prompt = system_prompt
        self.tool_registry = tool_registry
        self.memory_store = memory_store
        self.issue_service = issue_service
        self.wiki_dir = Path(wiki_dir) if wiki_dir else None
        # 截断上限从 agent_config 取——None 时用默认
        cfg = agent_config
        self._index_chars = cfg.wiki_index_chars if cfg else 4_000
        self._corrections_chars = cfg.corrections_chars if cfg else 2_000

    # system prompt 各块

    def _load_user_description(self) -> str:
        """读用户画像块——memory.md（Dreamer 定期加工）。

        Returns:
            画像文本；文件缺失时返回占位文案。
        """
        try:
            content = self.memory_store.memory_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        return content.strip() or "（暂无用户画像——对话积累后由记忆机制生成）"

    def _read_wiki_file(self, name: str) -> str:
        """读 wiki 系统文件。

        Args:
            name: 文件名（purpose.md/schema.md/index.md）。

        Returns:
            文件内容（strip 后）；不存在或未接入 wiki 返回空串。
        """
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
                if total + len(line) + 1 > self._index_chars:
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
        """读待处理纠错块——corrections.md（回答时注意避开）。

        Returns:
            纠错清单文本（超长截断）；无纠错时返回占位文案。
        """
        items = []
        if self.issue_service is not None:
            cards = self.issue_service.list(
                statuses={IssueStatus.OPEN, IssueStatus.BLOCKED},
                kinds={IssueKind.CONTENT_CORRECTION},
                limit=100,
            )
            items = [
                f"[{card.resource.get('path')}] {card.summary}"
                if card.resource.get("path")
                else card.summary
                for card in cards
            ]
        if not items:
            return "（暂无待处理纠错）"
        text = "\n".join(items)
        if len(text) > self._corrections_chars:
            text = text[: self._corrections_chars] + "\n...（纠错清单过长已截断）"
        return text

    def _build_system_prompt(
        self,
        tools_description: str,
        last_summary: str,
    ) -> str:
        """组装完整 system prompt（五块填充）。

        Args:
            tools_description: 工具描述文本。
            last_summary: 对话摘要。

        Returns:
            填充后的 system prompt 文本。
        """
        return self.system_prompt.format(
            user_description=self._load_user_description(),
            wiki_context=self._load_wiki_context(),
            # 兼容旧的外部模板；正式 ReAct system prompt 已移除该占位符。
            corrections="",
            tools_description=tools_description,
            summary=last_summary,
        )

    def build_messages(
        self,
        session: Session,
        current_message: Message,
        last_summary: str,
        history: list[Message],
    ) -> list[Message]:
        """构建请求消息——system + 历史 + 当前输入。

        职责边界: 只做组装（system prompt 五块 + history 拼接 +
        当前消息追加）。消息治理（合并连续同 role / 孤儿修复 /
        token 截断）是 ContextGovernor.prepare_for_llm 的职责——
        本函数的输出是"原始形态"，治理在发出请求前由 governor 完成。

        Args:
            session: 会话（供组装使用）。
            current_message: 当前用户消息（追加在末尾）。
            last_summary: 对话摘要（进 system prompt）。
            history: 未压缩历史消息（拼接在 system 之后）。

        Returns:
            组装好的消息列表（首条为 system）。
        """
        messages = [
            Message(
                role="system",
                content=self._build_system_prompt(
                    tools_description=self.tool_registry.get_all_description(),
                    last_summary=last_summary,
                ),
            )
        ]
        messages.extend(history)
        corrections = self._load_corrections()
        if corrections != "（暂无待处理纠错）":
            messages.append(
                Message(
                    role="system",
                    content=(
                        "# 当前 Wiki 纠错提示\n"
                        "以下内容是用户提交但尚未完成修复的纠错。回答时不要把相关页面的争议内容当作确定事实；"
                        "如问题涉及这些页面，明确提示存在待核实问题。\n\n" + corrections
                    ),
                )
            )
        messages.append(current_message)

        return messages

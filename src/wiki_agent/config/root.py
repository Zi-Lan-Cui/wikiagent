"""配置根——单一加载入口。

设计:
- 分层优先级: 构造参数 > 环境变量 > .env 文件 > 默认值（12-Factor）
- frozen: 加载后不可变，配置是契约不是状态
- fail-fast: 坏配置启动即报错，带明确提示

用法::

    cfg = load_config(project_root=Path("."))
    llm = create_llm(cfg.llm)
    agent = ReActAgent(llm=llm, vlm=vlm, tool_registry=..., workspace=...,
                       wiki_dir=..., agent_config=cfg.agent, hooks=...)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """文本 LLM 配置（env 前缀 LLM_）。"""

    model_config = SettingsConfigDict(env_prefix="LLM_", frozen=True, extra="ignore")

    api_key: str = ""
    base_url: str = ""
    model_id: str = ""


class VLMConfig(LLMConfig):
    """多模态 VLM 配置（env 前缀 VLM_），用于图片 caption。

    继承 LLMConfig——字段完全一致（多模态能力由 Message(images=)
    驱动而非客户端配置），只覆盖 env 前缀。LLMClient(cfg) 接受的
    是结构化子类型而非鸭子类型同名字段。
    """

    model_config = SettingsConfigDict(env_prefix="VLM_", frozen=True, extra="ignore")


class AgentConfig(BaseSettings):
    """Agent 生成与治理参数（env 前缀 AGENT_）。

    agent 生成与治理参数——唯一配置来源（react/governor/consolidator 直接读本类）。
    """

    model_config = SettingsConfigDict(env_prefix="AGENT_", frozen=True, extra="ignore")

    context_windows: int = 128_000
    max_tokens: int = 4_096
    max_messages_length: int = 2_000
    max_loop: int = 20
    consolidate_ratio: float = 0.5
    trigger_ratio: float = 0.8
    dream_interval: int = 3_000


class LoggingConfig(BaseSettings):
    """日志策略（env 前缀 LOG_）。"""

    model_config = SettingsConfigDict(env_prefix="LOG_", frozen=True, extra="ignore")

    debug: bool = False
    """--debug: 全量日志 + 结构化事件写文件。"""


class PathsConfig(BaseSettings):
    """项目路径（env 前缀 WIKI_）。

    env 不覆盖时按项目根推导——入口传入 project_root 即可。

    env_file 是本类的一个普通字段（记录 .env 位置供查询），
    与 pydantic-settings 的 `_env_file` 参数（告诉库去哪读 .env）
    无关——名字相近但语义不同，本类不消费 _env_file。
    """

    model_config = SettingsConfigDict(env_prefix="WIKI_", frozen=True, extra="ignore")

    project_root: Path = Path(".")
    env_file: Path = Path("env/.env")
    wiki_dir: Path | None = None
    workspace_dir: Path | None = None

    def resolved_wiki_dir(self) -> Path:
        return self.wiki_dir or (self.project_root / "wiki")

    def resolved_workspace_dir(self) -> Path:
        return self.workspace_dir or (self.project_root / "workspace")


# ════════════════════════════════════════════════════════════
#  MCP 配置——独立 mcp.json，判别联合按 type 校验
# ════════════════════════════════════════════════════════════

class StdioMcpTransport(BaseModel):
    """stdio 传输——本地命令。"""

    type: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class SseMcpTransport(BaseModel):
    """SSE 传输——远程服务（旧规范）。"""

    type: Literal["sse"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class StreamableHttpTransport(BaseModel):
    """Streamable HTTP 传输（MCP 2025-06 规范，逐步取代 SSE）。"""

    type: Literal["streamable"]
    url: str


class McpServerConfig(BaseModel):
    """单个 MCP server 配置。transport 按 type 判别。"""

    name: str = ""
    transport: StdioMcpTransport | SseMcpTransport | StreamableHttpTransport = Field(
        discriminator="type",
    )


class McpConfig(BaseModel):
    """MCP 服务器集合——独立文件 env/mcp.json 加载。

    和 .env 分离的考量:
    - MCP server 清单是"基础设施结构"，不是密钥
    - JSON 语法高亮、可结构化、好 diff
    """

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    def enabled(self) -> dict[str, McpServerConfig]:
        return {k: v for k, v in self.servers.items() if v.transport}


class RootConfig(BaseSettings):
    """应用根配置——顶层字段对应各子系统。"""

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @model_validator(mode="after")
    def _check_required(self) -> "RootConfig":
        """fail-fast 校验——缺关键配置启动即报错。"""
        if not self.llm.api_key:
            raise ValueError(
                "缺少 LLM API key——请在 env/.env 中设置 LLM_API_KEY"
            )
        if not self.llm.model_id:
            raise ValueError(
                "缺少 LLM 模型名——请在 env/.env 中设置 LLM_MODEL_ID"
            )
        return self


def load_config(
    env_file: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
    overrides: dict | None = None,
) -> RootConfig:
    """加载配置的唯一入口。

    Args:
        env_file: .env 文件路径。默认 project_root/env/.env
        project_root: 项目根目录，用于推导 wiki/workspace 路径
        overrides: CLI 参数等最高优先级覆盖，如 {"logging": {"debug": True}}

    Returns:
        校验过的 frozen RootConfig

    Raises:
        ValidationError: 配置缺失/类型错误——启动即报错，带明确提示
    """
    root = (
        Path(project_root) if project_root
        else Path(__file__).resolve().parents[3]
    )
    env = Path(env_file) if env_file else (root / "env" / ".env")

    paths = PathsConfig(project_root=root, env_file=env)
    llm = LLMConfig(_env_file=env)
    vlm = VLMConfig(_env_file=env)
    agent = AgentConfig(_env_file=env)
    logging_cfg = LoggingConfig(_env_file=env)

    # MCP: 独立 env/mcp.json（存在才加载）——结构与密钥分离
    mcp_cfg = McpConfig()
    mcp_file = root / "env" / "mcp.json"
    if mcp_file.exists():
        mcp_cfg = McpConfig.model_validate(json.loads(mcp_file.read_text(encoding="utf-8")))

    cfg = RootConfig(
        llm=llm, vlm=vlm, agent=agent,
        logging=logging_cfg, paths=paths, mcp=mcp_cfg,
    )
    if overrides:
        # dump 成 dict 树 → 合并 overrides → 整体重新 validate。
        # model_validate 天然把嵌套 dict 转回子配置对象，
        # 且 fail-fast 校验（缺 API key）在合并后重新执行。
        cfg = RootConfig.model_validate({**cfg.model_dump(), **overrides})
    return cfg


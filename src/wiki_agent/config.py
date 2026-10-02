"""配置模块：单一入口 load_config。

- 优先级: 构造参数 > 环境变量 > .env 文件 > 默认值
- 配置对象 frozen，加载后不可变
- 配置错误在加载时立即报错，提示明确
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """文本 LLM 配置（env 前缀 LLM_）。"""

    model_config = SettingsConfigDict(env_prefix="LLM_", frozen=True, extra="ignore")

    api_key: str = ""
    base_url: str = ""
    model_id: str = ""
    thinking: Literal["enabled", "disabled"] = "enabled"
    """是否启用模型 reasoning；通过 ``LLM_THINKING`` 控制。"""
    # 单次请求超时（秒）。思考模型两次数据块之间的间隔可能超过 120 秒
    # 导致 ReadTimeout，故默认放宽到 300
    timeout: float = 300.0
    max_concurrency: int = 4
    requests_per_minute: int = 60
    tokens_per_minute: int = 500_000

    @model_validator(mode="after")
    def _check_limits(self) -> LLMConfig:
        prefix = str(self.model_config.get("env_prefix", "LLM_")).rstrip("_")
        if self.timeout <= 0:
            raise ValueError(f"{prefix}_TIMEOUT 必须大于 0 秒")
        if self.max_concurrency < 1:
            raise ValueError(f"{prefix}_MAX_CONCURRENCY 必须至少为 1")
        if self.requests_per_minute < 0 or self.tokens_per_minute < 0:
            raise ValueError(
                f"{prefix}_REQUESTS_PER_MINUTE 和 {prefix}_TOKENS_PER_MINUTE 不能为负数"
            )
        return self


class VLMConfig(LLMConfig):
    """多模态 VLM 配置（env 前缀 VLM_），用于图片 caption。

    字段与 LLMConfig 完全一致，只覆盖 env 前缀；多模态能力由调用时的
    Message(images=) 决定，而非客户端配置。
    """

    model_config = SettingsConfigDict(env_prefix="VLM_", frozen=True, extra="ignore")


class AgentConfig(BaseSettings):
    """Agent 生成与上下文治理参数（env 前缀 AGENT_）。

    react/governor/consolidator 直接读取本类。

    治理参数:
    - 工具结果 TTL: 可随时重新获取的工具结果的保留时限；wiki 内容随编译
      变化，跨轮的旧读取会失真。30 分钟是经验值
    - 转存/紧凑化/snip: 上下文窗口超限时的处理阈值，与 context_windows 配套调整
    """

    model_config = SettingsConfigDict(env_prefix="AGENT_", frozen=True, extra="ignore")

    context_windows: int = 128_000
    max_tokens: int = 4_096
    max_messages_length: int = 2_000
    max_loop: int = 20
    consolidate_ratio: float = 0.5
    trigger_ratio: float = 0.8
    session_idle_minutes: int = 15
    session_tail_messages: int = 6
    dream_poll_interval: int = 60
    # 治理参数
    tool_result_ttl_minutes: int = 30  # 可随时重新获取的工具结果的保留时限
    tool_persist_length: int = 8_000  # 工具结果转存阈值（超限写文件）
    snip_safe_buffer: int = 1024  # token 估计安全余量
    inflight_target_ratio: float = 0.85  # 窗口紧凑化的目标占用比例
    inflight_compact_min_chars: int = 500  # 短于此长度的结果不做紧凑化
    snip_ratio: float = 0.5  # snip 截断后保留的预算比例
    wiki_index_chars: int = 4_000  # system prompt 的 index 地图截断
    corrections_chars: int = 2_000  # system prompt 的纠错清单截断

    @model_validator(mode="after")
    def _check_bounds(self) -> AgentConfig:
        if self.context_windows <= 0 or self.max_tokens <= 0:
            raise ValueError("AGENT_CONTEXT_WINDOWS 和 AGENT_MAX_TOKENS 必须大于 0")
        if self.max_tokens >= self.context_windows:
            raise ValueError("AGENT_MAX_TOKENS 必须小于 AGENT_CONTEXT_WINDOWS")
        for field in ("consolidate_ratio", "trigger_ratio", "inflight_target_ratio", "snip_ratio"):
            value = getattr(self, field)
            if not 0 < value < 1:
                raise ValueError(f"AGENT_{field.upper()} 必须在 (0, 1) 内")
        if self.consolidate_ratio >= self.trigger_ratio:
            raise ValueError("AGENT_CONSOLIDATE_RATIO 必须小于 AGENT_TRIGGER_RATIO")
        if (
            min(
                self.max_messages_length,
                self.max_loop,
                self.session_idle_minutes,
                self.session_tail_messages,
                self.dream_poll_interval,
                self.tool_result_ttl_minutes,
                self.tool_persist_length,
                self.inflight_compact_min_chars,
                self.wiki_index_chars,
                self.corrections_chars,
            )
            <= 0
        ):
            raise ValueError("Agent 的消息数、时间窗和字符预算必须大于 0")
        if self.snip_safe_buffer < 0:
            raise ValueError("AGENT_SNIP_SAFE_BUFFER 不能为负数")
        return self


class RetryConfig(BaseSettings):
    """LLM 调用重试策略（env 前缀 ``RETRY_``）。

    仅覆盖 LLM 层；源文件级失败需手动重试，不经此配置排程，故无 source_* 字段。
    """

    model_config = SettingsConfigDict(env_prefix="RETRY_", frozen=True, extra="ignore")

    llm_max_attempts: int = 3
    llm_base_delay_seconds: float = 2.0

    @model_validator(mode="after")
    def _check_bounds(self) -> RetryConfig:
        if self.llm_max_attempts < 1:
            raise ValueError("RETRY_LLM_MAX_ATTEMPTS 必须至少为 1")
        if self.llm_base_delay_seconds <= 0:
            raise ValueError("RETRY_LLM_BASE_DELAY_SECONDS 必须大于 0 秒")
        return self


class CompileConfig(BaseSettings):
    """编译阶段预算与吞吐参数（env 前缀 COMPILE_）。

    ``context_window`` 是模型能力上限；Extractor 扣除 system、输出和
    安全缓冲后得到各阶段输入预算。
    """

    model_config = SettingsConfigDict(env_prefix="COMPILE_", frozen=True, extra="ignore")

    context_window: int = 128_000
    chunk_size: int = 8_000
    extract_concurrency: int = 3
    extract_system_tokens: int = 4_000
    extract_output_tokens: int = 6_000
    context_safety_buffer: int = 1_024

    @model_validator(mode="after")
    def _check_bounds(self) -> CompileConfig:
        if self.context_window <= 0:
            raise ValueError("COMPILE_CONTEXT_WINDOW 必须大于 0")
        if self.chunk_size < 256:
            raise ValueError("COMPILE_CHUNK_SIZE 必须至少为 256")
        if self.extract_concurrency < 1:
            raise ValueError("COMPILE_EXTRACT_CONCURRENCY 必须至少为 1")
        if self.extract_system_tokens < 0 or self.extract_output_tokens < 0:
            raise ValueError("编译 token 预算不能为负数")
        if self.context_safety_buffer < 0:
            raise ValueError("COMPILE_CONTEXT_SAFETY_BUFFER 不能为负数")
        reserved = (
            self.extract_system_tokens + self.extract_output_tokens + self.context_safety_buffer
        )
        if reserved >= self.context_window:
            raise ValueError("编译阶段 system/output/安全缓冲预算超过 context window")
        return self


class LoggingConfig(BaseSettings):
    """日志策略（env 前缀 LOG_）。"""

    model_config = SettingsConfigDict(env_prefix="LOG_", frozen=True, extra="ignore")

    debug: bool = False
    """--debug: 全量日志 + 结构化事件写文件。"""


def source_records_dir_for(workspace: str | Path) -> Path:
    """workspace 内的来源记录目录；内部布局，只有 workspace 本身可配置。"""
    return Path(workspace) / "provenance" / "sources"


def sync_state_path_for(workspace: str | Path) -> Path:
    """workspace 内的 sync 完成状态文件路径；内部布局，目录名 watch 沿用既有名称。"""
    return Path(workspace) / "watch" / "state.json"


class PathsConfig(BaseSettings):
    """项目路径（env 前缀 WIKI_）。

    env 未配置时按 project_root 推导，入口传入 project_root 即可。
    """

    model_config = SettingsConfigDict(env_prefix="WIKI_", frozen=True, extra="ignore")

    project_root: Path = Path(".")
    env_file: Path = Path("env/.env")
    materials_dir: Path | None = None
    wiki_dir: Path | None = None
    workspace_dir: Path | None = None

    @model_validator(mode="after")
    def validate_storage_boundaries(self) -> PathsConfig:
        """拒绝相互重叠的数据根，避免来源、产物和运行状态混写。"""
        roots = {
            "WIKI_MATERIALS_DIR": self.resolved_materials_dir().resolve(),
            "WIKI_WIKI_DIR": self.resolved_wiki_dir().resolve(),
            "WIKI_WORKSPACE_DIR": self.resolved_workspace_dir().resolve(),
        }
        items = list(roots.items())
        for index, (left_name, left) in enumerate(items):
            for right_name, right in items[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError(
                        f"{left_name} 与 {right_name} 必须是互不包含的独立目录: {left} / {right}"
                    )
        return self

    def resolved_materials_dir(self) -> Path:
        """用户原始资料根目录，默认与 wiki/workspace 平级。"""
        return self._resolve(self.materials_dir, "materials")

    def resolved_wiki_dir(self) -> Path:
        """解析后的 wiki 目录。

        Returns:
            显式配置的 wiki_dir；未配置时为 project_root/wiki。相对
            路径始终相对 project_root 解析，从任意工作目录启动都指向
            同一份数据。
        """
        return self._resolve(self.wiki_dir, "wiki")

    def resolved_workspace_dir(self) -> Path:
        """解析后的工作区目录。

        Returns:
            显式配置的 workspace_dir；未配置时为 project_root/workspace。
            相对路径始终相对 project_root 解析。
        """
        return self._resolve(self.workspace_dir, "workspace")

    def resolved_source_records_dir(self) -> Path:
        """系统生成的来源摘要存档目录。"""
        return source_records_dir_for(self.resolved_workspace_dir())

    def resolved_sync_state_path(self) -> Path:
        """sync 完成状态文件路径。"""
        return sync_state_path_for(self.resolved_workspace_dir())

    def _resolve(self, configured: Path | None, default_name: str) -> Path:
        path = configured or Path(default_name)
        return path if path.is_absolute() else self.project_root / path


# MCP 配置：来自独立 mcp.json，transport 按 type 字段判别


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
    """Streamable HTTP 传输（MCP 2025-06 规范，SSE 的后续替代）。"""

    type: Literal["streamable"]
    url: str


class McpServerConfig(BaseModel):
    """单个 MCP server 配置，transport 按 type 判别。

    need_resources / need_prompts 是 server 级开关，transport 子配置
    不包含这两个字段，只能从这里读。
    """

    name: str = ""
    transport: StdioMcpTransport | SseMcpTransport | StreamableHttpTransport = Field(
        discriminator="type",
    )
    need_resources: bool = False  # 是否暴露 resources（wrapper 未实现，见 adaptor）
    need_prompts: bool = False  # 是否暴露 prompts（wrapper 未实现，见 adaptor）


class McpConfig(BaseModel):
    """MCP 服务器集合，从独立文件 env/mcp.json 加载。

    与 .env 分离：这里是结构化清单而非密钥，JSON 格式便于校验与 diff。
    """

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class RootConfig(BaseSettings):
    """应用根配置，顶层字段对应各子系统。"""

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    compile: CompileConfig = Field(default_factory=CompileConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @model_validator(mode="after")
    def _check_required(self) -> RootConfig:
        """校验关键配置，缺失时立即报错。

        Returns:
            校验通过后的自身。

        Raises:
            ValueError: LLM API key 或模型名缺失。
        """
        if not self.llm.api_key:
            raise ValueError("缺少 LLM API key——请在 env/.env 中设置 LLM_API_KEY")
        if not self.llm.model_id:
            raise ValueError("缺少 LLM 模型名——请在 env/.env 中设置 LLM_MODEL_ID")
        return self


def default_project_root() -> Path:
    """未显式指定项目根时的默认值：当前工作目录。

    各入口统一经此取值，不在各处各自调用 Path.cwd()。
    """
    return Path.cwd()


def load_config(
    env_file: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
    overrides: dict | None = None,
) -> RootConfig:
    """加载配置。

    Args:
        env_file: .env 文件路径，默认 project_root/env/.env
        project_root: 项目根目录，用于推导 wiki/workspace 路径
        overrides: 最高优先级覆盖（如 CLI 参数），如 {"logging": {"debug": True}}

    Returns:
        校验通过的 frozen RootConfig

    Raises:
        ValidationError: 配置缺失或类型错误
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    env = Path(env_file) if env_file else (root / "env" / ".env")

    # pydantic-settings 用保留参数 `_env_file` 指定加载哪个 .env。frozen 模型
    # 合成的 __init__ 签名不含继承来的该参数，类型检查会误报；经 dict 解包传入
    # 绕开误报，运行时行为不变。
    env_source: dict[str, Any] = {"_env_file": env}

    paths = PathsConfig(**env_source, project_root=root, env_file=env)
    llm = LLMConfig(**env_source)
    vlm = VLMConfig(**env_source)
    agent = AgentConfig(**env_source)
    compile_cfg = CompileConfig(**env_source)
    logging_cfg = LoggingConfig(**env_source)
    retry_cfg = RetryConfig(**env_source)

    # MCP 服务器配置来自 env/mcp.json，文件存在才加载
    mcp_cfg = McpConfig()
    mcp_file = root / "env" / "mcp.json"
    if mcp_file.exists():
        mcp_cfg = McpConfig.model_validate(json.loads(mcp_file.read_text(encoding="utf-8")))

    cfg = RootConfig(
        llm=llm,
        vlm=vlm,
        agent=agent,
        compile=compile_cfg,
        logging=logging_cfg,
        paths=paths,
        retry=retry_cfg,
        mcp=mcp_cfg,
    )
    if overrides:
        # 转 dict 深合并 overrides 后整体重新校验；model_validate 会把
        # 嵌套 dict 转回子配置对象，必填项校验在合并后再次执行。
        cfg = RootConfig.model_validate(_deep_merge(cfg.model_dump(), overrides))
    return cfg


def _deep_merge(base: dict, overrides: Mapping) -> dict:
    """递归合并配置覆盖，保留未被覆盖的嵌套字段。"""
    merged = deepcopy(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged

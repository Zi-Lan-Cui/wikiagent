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
    # 单次请求超时（秒）——reasoning 模型思考段长，
    # 120s 对 deepseek 思考链不够（两次 chunk 间隔超限即 ReadTimeout）
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

    继承 LLMConfig——字段完全一致（多模态能力由 Message(images=)
    驱动而非客户端配置），只覆盖 env 前缀。LLMClient(cfg) 接受的
    是结构化子类型而非鸭子类型同名字段。
    """

    model_config = SettingsConfigDict(env_prefix="VLM_", frozen=True, extra="ignore")


class AgentConfig(BaseSettings):
    """Agent 生成与治理参数（env 前缀 AGENT_）。

    agent 生成与治理参数——唯一配置来源（react/governor/consolidator 直接读本类）。

    治理参数（E3 收编，2026-08-17）:
    - 工具结果 TTL: 可重复获得工具的驱逐时限——wiki 经 compile/refine
      变化，跨轮旧读取会失真；30 分钟是经验值（实测依据见报告 11.7）
    - 转存/紧凑化/snip: 窗口维度治理的阈值——与 context_windows 配套调
    """

    model_config = SettingsConfigDict(env_prefix="AGENT_", frozen=True, extra="ignore")

    context_windows: int = 128_000
    max_tokens: int = 4_096
    max_messages_length: int = 2_000
    max_loop: int = 20
    consolidate_ratio: float = 0.5
    trigger_ratio: float = 0.8
    dream_interval: int = 3_000
    session_idle_minutes: int = 15
    session_tail_messages: int = 6
    dream_poll_interval: int = 60
    # ── 治理参数（E3 收编——原散在 governor/builder 模块顶部）──
    tool_result_ttl_minutes: int = 30  # 可重复获得工具（导航三件套）驱逐时限
    tool_persist_length: int = 8_000  # 工具结果转存阈值（超限写文件）
    snip_safe_buffer: int = 1024  # token 估计安全余量
    inflight_target_ratio: float = 0.85  # 窗口紧凑化目标水位
    inflight_compact_min_chars: int = 500  # 紧凑化最小长度（短结果不值得）
    snip_ratio: float = 0.5  # snip 截断力度（保预算的比例）
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
                self.dream_interval,
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


class WatchConfig(BaseSettings):
    """文件监听时间窗与变更门（env 前缀 WATCH_）。"""

    model_config = SettingsConfigDict(env_prefix="WATCH_", frozen=True, extra="ignore")
    settle_window: float = 2.0
    stability_delay: float = 2.0
    fallback_interval: float = 60.0
    similarity_threshold: float = 0.7

    @model_validator(mode="after")
    def _check_bounds(self) -> WatchConfig:
        if self.settle_window <= 0 or self.stability_delay <= 0 or self.fallback_interval <= 0:
            raise ValueError(
                "WATCH_SETTLE_WINDOW、WATCH_STABILITY_DELAY、WATCH_FALLBACK_INTERVAL 必须大于 0"
            )
        if not 0 <= self.similarity_threshold <= 1:
            raise ValueError("WATCH_SIMILARITY_THRESHOLD 必须在 [0, 1] 内")
        return self


class RetryConfig(BaseSettings):
    """远程 LLM 与 source 失败队列的重试策略（env 前缀 ``RETRY_``）。"""

    model_config = SettingsConfigDict(env_prefix="RETRY_", frozen=True, extra="ignore")

    llm_max_attempts: int = 3
    llm_base_delay_seconds: float = 2.0
    source_max_attempts: int = 3
    source_base_delay_seconds: float = 30.0
    source_max_delay_seconds: float = 3_600.0

    @model_validator(mode="after")
    def _check_bounds(self) -> RetryConfig:
        if self.llm_max_attempts < 1 or self.source_max_attempts < 1:
            raise ValueError("RETRY_LLM_MAX_ATTEMPTS 和 RETRY_SOURCE_MAX_ATTEMPTS 必须至少为 1")
        if self.llm_base_delay_seconds <= 0 or self.source_base_delay_seconds <= 0:
            raise ValueError("RETRY 的基础退避时间必须大于 0 秒")
        if self.source_max_delay_seconds < self.source_base_delay_seconds:
            raise ValueError("RETRY_SOURCE_MAX_DELAY_SECONDS 必须不小于基础退避时间")
        return self


class CompileConfig(BaseSettings):
    """编译阶段预算与吞吐参数（env 前缀 COMPILE_）。

    ``context_window`` 是模型能力上限；Extractor 会扣除 system、输出和
    安全缓冲后得到阶段输入预算，不再由各层分别维护 60k/120k 两个语义
    不同的默认值。
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


class PathsConfig(BaseSettings):
    """项目路径（env 前缀 WIKI_）。

    env 不覆盖时按项目根推导——入口传入 project_root 即可。

    ``load_config`` 与其他 Settings 一样传入 `_env_file`，因此
    ``WIKI_WIKI_DIR`` 等字段遵循统一优先级。
    """

    model_config = SettingsConfigDict(env_prefix="WIKI_", frozen=True, extra="ignore")

    project_root: Path = Path(".")
    env_file: Path = Path("env/.env")
    source_dir: Path | None = None
    wiki_dir: Path | None = None
    workspace_dir: Path | None = None

    @model_validator(mode="after")
    def validate_storage_boundaries(self) -> PathsConfig:
        """拒绝相互重叠的数据根，避免来源、产物和运行状态混写。"""
        roots = {
            "WIKI_SOURCE_DIR": self.resolved_source_dir().resolve(),
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

    def resolved_source_dir(self) -> Path:
        """返回用户原始资料根目录，默认与 wiki/workspace 平级。"""
        return self._resolve(self.source_dir, "sources")

    def resolved_wiki_dir(self) -> Path:
        """返回解析后的 wiki 目录。

        Returns:
            显式配置的 wiki_dir；未配置时按 project_root/wiki 推导。

            相对路径始终相对于 ``project_root``，这样从任意工作目录
            启动（例如 ``uvicorn`` 或服务管理器）都使用同一份数据目录。
        """
        return self._resolve(self.wiki_dir, "wiki")

    def resolved_workspace_dir(self) -> Path:
        """返回解析后的工作区目录。

        Returns:
            显式配置的 workspace_dir；未配置时按
            project_root/workspace 推导。相对路径始终相对于
            ``project_root``。
        """
        return self._resolve(self.workspace_dir, "workspace")

    def resolved_source_records_dir(self) -> Path:
        """返回系统生成的来源摘要存档目录。"""
        return self.resolved_workspace_dir() / "provenance" / "sources"

    def resolved_runs_dir(self) -> Path:
        """返回编译、精炼、重试和手术的运行存档根目录。"""
        return self.resolved_workspace_dir() / "runs"

    def resolved_watch_dir(self) -> Path:
        """返回 watch 持久化状态目录。"""
        return self.resolved_workspace_dir() / "watch"

    def _resolve(self, configured: Path | None, default_name: str) -> Path:
        path = configured or Path(default_name)
        return path if path.is_absolute() else self.project_root / path


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
    """单个 MCP server 配置。transport 按 type 判别。

    need_resources / need_prompts 是 server 级开关——transport
    子配置上没有（adaptor 曾从 transport 读这两个字段，
    AttributeError 导致 SSE server 连接必失败）。
    """

    name: str = ""
    transport: StdioMcpTransport | SseMcpTransport | StreamableHttpTransport = Field(
        discriminator="type",
    )
    need_resources: bool = False  # 是否暴露 resources（wrapper 未实现，见 adaptor）
    need_prompts: bool = False  # 是否暴露 prompts（wrapper 未实现，见 adaptor）


class McpConfig(BaseModel):
    """MCP 服务器集合——独立文件 env/mcp.json 加载。

    和 .env 分离的考量:
    - MCP server 清单是"基础设施结构"，不是密钥
    - JSON 语法高亮、可结构化、好 diff
    """

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    def enabled(self) -> dict[str, McpServerConfig]:
        """返回启用的 MCP server（带有效 transport 的）。

        Returns:
            server 名 → 配置映射；transport 为空的被过滤。
        """
        return {k: v for k, v in self.servers.items() if v.transport}


class RootConfig(BaseSettings):
    """应用根配置——顶层字段对应各子系统。"""

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    compile: CompileConfig = Field(default_factory=CompileConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @model_validator(mode="after")
    def _check_required(self) -> RootConfig:
        """fail-fast 校验——缺关键配置启动即报错。

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
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    env = Path(env_file) if env_file else (root / "env" / ".env")

    # pydantic-settings 用带下划线的 `_env_file` 指定加载哪个 .env（刻意加下
    # 划线以免与字段名冲突）。frozen 模型下 pyright 按字段合成 __init__ 签名、
    # 看不到继承来的该参数；经 dict[str, Any] 解包传入即可绕开该假阳性——运行时
    # 命中的仍是 BaseSettings.__init__ 的同名参数，行为完全不变。
    env_source: dict[str, Any] = {"_env_file": env}

    paths = PathsConfig(**env_source, project_root=root, env_file=env)
    llm = LLMConfig(**env_source)
    vlm = VLMConfig(**env_source)
    agent = AgentConfig(**env_source)
    compile_cfg = CompileConfig(**env_source)
    logging_cfg = LoggingConfig(**env_source)
    watch_cfg = WatchConfig(**env_source)
    retry_cfg = RetryConfig(**env_source)

    # MCP: 独立 env/mcp.json（存在才加载）——结构与密钥分离
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
        watch=watch_cfg,
        retry=retry_cfg,
        mcp=mcp_cfg,
    )
    if overrides:
        # dump 成 dict 树 → 深合并 overrides → 整体重新 validate。
        # model_validate 天然把嵌套 dict 转回子配置对象，
        # 且 fail-fast 校验（缺 API key）在合并后重新执行。
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

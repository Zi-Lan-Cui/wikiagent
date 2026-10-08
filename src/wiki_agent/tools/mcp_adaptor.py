import asyncio
import hashlib
import re
from contextlib import AsyncExitStack, suppress
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from wiki_agent.log import get_logger
from wiki_agent.tools.base import BaseTool
from wiki_agent.tools.registry import ToolRegistry

logger = get_logger("MCP")


class MCPConnection:
    """对任务外部提供关闭连接的接口。

    stdio 要求关闭连接栈必须发生在持有连接的 task 内，
    因此外部只通过本对象向该 task 发信号。
    """

    def __init__(
        self,
        owner: asyncio.Task[None],
        close_requsted: asyncio.Event,
        dead: asyncio.Event | None = None,
    ) -> None:
        """包装连接生命周期。

        Args:
            owner: 持有连接栈的后台任务。
            close_requsted: 关闭请求信号。
            dead: 健康检查失败信号（连接假死）。
        """
        self._owner = owner
        self._close_requsted = close_requsted
        self._dead = dead or asyncio.Event()

    async def aclose(self):
        """请求关闭连接并等待 owner 退出。

        shield 保证清理过程不被取消打断；owner 因自身异常退出时
        （finally 已清理）异常不传播。
        """
        self._close_requsted.set()
        try:
            await asyncio.shield(self._owner)
        except asyncio.CancelledError:
            if not self._owner.cancelled():
                raise
        except Exception:
            pass


async def connect_mcp_servers(
    mcp_servers: dict, tool_registry: ToolRegistry
) -> dict[str, MCPConnection]:
    """连接配置中的所有 MCP server 并把工具注册进 registry。

    Args:
        mcp_servers: server 名到 McpServerConfig 的映射，transport 在
            cfg.transport，need_resources 与 need_prompts 是 server 级开关。
        tool_registry: 工具注册表（工具注册进这里）。

    Returns:
        server 名到 MCPConnection 的映射；单个 server 连接失败时
        记录日志后跳过，不影响其他 server。
    """

    async def open_single_server(name, cfg):
        transport = cfg.transport
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            if transport.type == "stdio":
                server_params = StdioServerParameters(
                    command=transport.command, args=transport.args, env=transport.env
                )
                read, write = await server_stack.enter_async_context(stdio_client(server_params))

            elif transport.type == "sse":
                # 工厂签名由 sse_client 约定，参数必须保留；返回普通
                # httpx.AsyncClient。
                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: Any | None = None,
                    auth: Any | None = None,
                ) -> Any:
                    merged_headers = {
                        "Accept": "application/json,text/event-stream",
                        **(headers or {}),
                        **(
                            transport.headers or {}
                        ),  # headers 为 client 调用时注入，transport.headers 是配置项
                    }

                    return httpx.AsyncClient(
                        headers=merged_headers,
                        timeout=timeout,
                        auth=auth,
                        trust_env=False,  # 不读取环境变量中的代理配置
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(url=transport.url, httpx_client_factory=httpx_client_factory)
                )

            elif transport.type == "streamable":
                # 该 client 返回 3 元组，第三个 get_session_id 供会话头管理；
                # ClientSession 只用前两个，显式丢弃
                read, write, _get_session_id = await server_stack.enter_async_context(
                    streamable_http_client(transport.url)
                )

            else:
                # 配置可能被外部 JSON 直接修改，显式抛错
                # 避免 read/write 未定义产生的 NameError
                raise ValueError(f"MCP server '{name}' 未知传输类型: {transport.type!r}")

            session = await server_stack.enter_async_context(ClientSession(read, write))

            await session.initialize()

            tools = await session.list_tools()

            registered: list[str] = []
            for tool in tools.tools:
                wrappered_tool = MCPToolWrapper(session, name, tool)
                tool_registry.register(wrappered_tool)
                registered.append(wrappered_tool.name)

            if cfg.need_resources:
                # Resources 支持未实现，只探测记录、不注册
                resources = await session.list_resources()
                logger.warning(
                    "MCP server '%s' 暴露 %d 个 resources——Resources 支持未实现，跳过注册",
                    name,
                    len(resources.resources),
                )

            if cfg.need_prompts:
                prompts = await session.list_prompts()
                logger.warning(
                    "MCP server '%s' 暴露 %d 个 prompts——Prompts 支持未实现，跳过注册",
                    name,
                    len(prompts.prompts),
                )

            return name, session, server_stack, registered

        except BaseException:
            # 连接阶段失败时当场清理 context：async generator 泄漏给 GC 后，
            # 事件循环关闭时会跨 task athrow，anyio 拒绝跨 task 退出
            try:
                await server_stack.aclose()
            except Exception:
                pass
            raise

    async def connect_single_server(name, cfg):
        # 用 get_running_loop：get_event_loop 无当前 loop 时行为不确定
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        close_requested = asyncio.Event()
        dead = asyncio.Event()

        async def own_connection():
            stack: AsyncExitStack | None = None
            session = None
            registered: list[str] = []
            try:
                _, session, stack, registered = await open_single_server(name, cfg)
                if not ready.done():
                    ready.set_result(stack is not None)
                if stack is None:
                    return

                # 健康监督：定期 ping，失败判定连接假死。
                # 断连异常发生在 SDK 内部后台 reader task，不冒到本 task，
                # 只能靠主动探测发现。
                async def health_watch():
                    while not close_requested.is_set():
                        await asyncio.sleep(10)
                        try:
                            # mcp 1.x 运行时提供 ping，但其类型桩未声明该方法；
                            # 仅在此 SDK 兼容边界窄化为 Any，不放宽全局检查。
                            await asyncio.wait_for(getattr(session, "ping")(), timeout=5)
                        except Exception:
                            dead.set()
                            return

                watcher = asyncio.create_task(health_watch())
                # asyncio.wait 在 3.13 禁止传 coroutine，Event.wait() 须先包成 task
                close_task = asyncio.create_task(close_requested.wait())
                dead_task = asyncio.create_task(dead.wait())
                try:
                    await asyncio.wait(
                        {close_task, dead_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    close_task.cancel()
                    dead_task.cancel()
                    watcher.cancel()

                if dead.is_set() and not close_requested.is_set():
                    logger.warning("MCP server '%s' 连接已断开（ping 失败）", name)

            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise
            finally:
                if stack is not None:
                    try:
                        await stack.aclose()
                    except Exception:
                        # AsyncExitStack 关闭 ClientSession 在先、sse_client 在后，
                        # sse_reader 后台任务可能往已关闭的流写入，触发 BrokenResourceError，属于无害关闭噪音
                        pass
                # 连接结束（假死或正常关闭）即摘除该 server 的工具
                for tool_name in registered:
                    tool_registry.unregister(tool_name)

        owner = asyncio.create_task(own_connection(), name=f"mcp:{name}")
        connection = MCPConnection(owner, close_requested, dead)

        connected = False
        try:
            connected = await ready
        except BaseException:
            # 不 cancel owner：cancel 会打断 finally 里的 stack.aclose()
            # （CancelledError 不被 except Exception 接住），导致生成器泄漏给 GC。
            # dead.set() 让 owner 的 wait 自然返回，走完整清理
            close_requested.set()
            dead.set()
            with suppress(BaseException):
                await asyncio.shield(owner)
            raise

        if not connected:
            await connection.aclose()
            return name, None
        return name, connection

    server_stacks: dict[str, MCPConnection] = {}

    for name, cfg in mcp_servers.items():
        try:
            name, connection = await connect_single_server(name, cfg)
        except Exception:
            logger.exception(f"MCP server '{name}' connection failed")
            continue
        if connection is not None and name:
            server_stacks[name] = connection

    return server_stacks


def _extract_nullable_branch(options: list[dict]):
    if not isinstance(options, list):
        return None

    non_null = []
    has_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            has_null = True
            continue
        non_null.append(option)

    # 只处理恰好一个 null 加一个非 null 的情况
    if has_null and len(non_null) == 1:
        return non_null[0], True
    return None


def normlize_schema_for_openai(raw_schema):
    """把 MCP schema 规范化为 OpenAI 兼容格式。

    去除 OpenAI 不支持的 null / anyOf / oneOf 以及多参数类型形式。

    Args:
        raw_schema: MCP 工具 inputSchema。

    Returns:
        规范化后的 schema dict；输入非 dict 时返回空 object 结构。
    """
    if not isinstance(raw_schema, dict):
        return {"type": "object", "properties": {}}

    dict_schema = dict(raw_schema)

    raw_type = dict_schema.get("type")
    if isinstance(raw_type, list):
        # 多个非 null 类型 OpenAI 不支持，不做处理，留待调用方报错
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            dict_schema["type"] = non_null[0]
            dict_schema["nullable"] = True

    for key in ("oneOf", "anyOf"):
        branches = dict_schema.get(key)
        if not branches:
            continue
        nullable_branch = _extract_nullable_branch(branches)
        if nullable_branch:
            branch, _ = nullable_branch
            # 去掉该 key 后合入 branch（构造新 dict，不改原对象）
            merged = {k: v for k, v in dict_schema.items() if k != key}
            merged.update(branch)
            dict_schema = merged

            dict_schema["nullable"] = True
            break

    if "properties" in dict_schema and isinstance(dict_schema["properties"], dict):
        dict_schema["properties"] = {
            name: normlize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in dict_schema["properties"].items()
        }

    if "items" in dict_schema and isinstance(dict_schema["items"], dict):
        dict_schema["items"] = normlize_schema_for_openai(dict_schema["items"])

    if dict_schema.get("type") != "object":
        return dict_schema

    # object 类型补齐 properties、required 默认值（顶层与嵌套同样处理）
    dict_schema.setdefault("properties", {})
    dict_schema.setdefault("required", [])
    return dict_schema


# OpenAI function name 规范: 只允许 [a-zA-Z0-9_-]，最长 64 字符
_MAX_TOOL_NAME_LEN = 64


def _sanitize_tool_name(name: str) -> str:
    """把 MCP 工具名规范化为 OpenAI 兼容的函数名。

    规则:
    1. 非法字符替换为下划线，连续下划线折叠，首尾 _ - 去除
    2. 结果为空时用 mcp_tool
    3. 超长截断到 64 字符
    4. 原名含非 ASCII 字符时附 8 位稳定 hash，
       避免不同非 ASCII 名清理后互相覆盖

    原始名保存在 MCPToolWrapper.original_name，调用时用原名。
    """
    result = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    result = re.sub(r"_+", "_", result)
    result = result.strip("_-")
    if not result:
        result = "mcp_tool"
    if re.fullmatch(r"[a-zA-Z0-9_-]+", name) is None:
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        result = f"{result}_{digest}"
    return result[:_MAX_TOOL_NAME_LEN].rstrip("_-")


class MCPToolWrapper(BaseTool):
    """将 MCP 工具适配为统一的 ToolRegistry 执行协议。"""

    # 外部 MCP 工具的副作用未知，不能自动重试可能已成功的远端写操作
    side_effect = "irreversible"

    def __init__(self, session, server_name, tool_def, tool_timeout: int = 30):
        self.server_name = server_name
        self.session = session
        self.tool_def = tool_def
        self.raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self.timeout_seconds = tool_timeout
        self.original_name = tool_def.name

        # 工具名可能不符合 OpenAI 规范，先 sanitize 再加 server 名前缀，
        # 区分不同 server 的同名工具
        self.name = f"{server_name}_{_sanitize_tool_name(tool_def.name)}"
        self.description = tool_def.description
        self.parameters = normlize_schema_for_openai(self.raw_schema)

    async def execute_once(self, **kwargs):
        """调用 MCP 工具一次；请求 session 时使用 original_name。

        timeout、取消和异常分类由 ``ToolRegistry`` 统一处理。
        """
        result = await self.session.call_tool(self.original_name, arguments=kwargs)
        return self._render_call_result(result.content, kwargs)

    def _render_call_result(self, content, arguments):
        """渲染 MCP 调用结果；content 可能包含图片，当前只提取文本。

        Args:
            content: MCP 返回的 content 块列表。
            arguments: 调用参数（保留备用）。

        Returns:
            拼接的纯文本结果。
        """
        from mcp import types

        text_part: list[str] = []
        for block in content:
            if isinstance(block, types.TextContent):
                text_part.append(block.text)
                continue
        return "\n".join(text_part)


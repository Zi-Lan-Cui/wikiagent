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
    """
    给任务外部提供关闭连接的接口。因为stdio要求关闭任务的和连接的task要在一个task之内，所以需要使用这种方式包装owner
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

    @property
    def is_alive(self) -> bool:
        """连接是否存活（未被请求关闭且健康检查未失败）。

        Returns:
            True 表示连接可用。
        """
        return (
            not self._close_requsted.is_set() and not self._dead.is_set() and not self._owner.done()
        )

    async def aclose(self):
        """请求关闭连接并等待 owner 退出。

        shield 保证清理链接时 _owner 本身不被取消，
        避免清理到一半中止。owner 因自身异常已死亡时
        （finally 已尽力清理）异常不传播——调用方（CLI 收尾）
        不需要替 MCP 连接的旧伤买单。
        """
        self._close_requsted.set()
        try:
            # shield保证在清理链接的时候_owner本身不被取消，造成清理一半中止
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
        mcp_servers: server 名 → McpServerConfig（transport 在
            cfg.transport，need_resources/need_prompts 是 server 级开关）。
        tool_registry: 工具注册表（工具注册进这里）。

    Returns:
        server 名 → MCPConnection 映射；单个 server 连接失败
        记录日志后跳过（不影响其他 server）。
    """

    async def open_single_server(name, cfg):
        # cfg 是 McpServerConfig——传输细节在 cfg.transport，
        # need_resources/need_prompts 是 server 级开关
        # （曾直接从 transport 读，AttributeError 导致 SSE 必失败）
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
                # mcp 1.x 的 sse_client 使用 httpx + httpx-sse 的
                # aconnect_sse；项目已锁定 mcp<2，因此统一返回普通
                # httpx.AsyncClient。升级 MCP 主版本时需单独验证关闭语义。
                # 根据 client 的类型标识，工厂必须保留这些参数。
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
                        ),  # headers是client自己调用时注入，这个是自己配置输入
                    }

                    return httpx.AsyncClient(
                        headers=merged_headers,
                        timeout=timeout,
                        auth=auth,
                        trust_env=False,  # 不使用环境
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(url=transport.url, httpx_client_factory=httpx_client_factory)
                )

            elif transport.type == "streamable":
                # Streamable HTTP（MCP 2025-06 规范新传输，逐步取代 SSE）
                # 该 client 返回 3 元组（read, write, get_session_id）——
                # get_session_id 供会话头管理，ClientSession 只用前两个，显式丢弃
                read, write, _get_session_id = await server_stack.enter_async_context(
                    streamable_http_client(transport.url)
                )

            else:
                # 判别联合保证 type 合法，但配置可能被外部 JSON 直改——
                # else 显式抛错，避免 read/write 未定义的 NameError 误导排查
                raise ValueError(f"MCP server '{name}' 未知传输类型: {transport.type!r}")

            session = await server_stack.enter_async_context(ClientSession(read, write))

            await session.initialize()

            tools = await session.list_tools()

            for tool in tools.tools:
                wrappered_tool = MCPToolWrapper(session, name, tool)
                tool_registry.register(wrappered_tool)

            if cfg.need_resources:
                # TODO（MCP Resources 支持）: wrapper 是空壳未实现——
                # 注册空壳会在 get_all_schema_openai 读 name/description
                # 属性时 AttributeError 必崩。实现前不注册，只留探测日志。
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

            return name, session, server_stack

        except BaseException:
            # 连接阶段失败——当场清理全部 context。泄漏给 GC 的
            # async generator 会在事件循环关闭时被跨 task athrow，
            # anyio cancel scope 拒绝跨 task 退出，打印 RuntimeError
            # 噪音（"generator didn't stop after athrow" 那类报错）
            try:
                await server_stack.aclose()
            except Exception:
                pass
            raise

    async def connect_single_server(name, cfg):
        # get_running_loop——本函数在 async 上下文内，
        # get_event_loop 无当前 loop 时行为有坑（可能新建/报错）
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        close_requested = asyncio.Event()
        dead = asyncio.Event()  # 健康检查失败 = 连接假死

        async def own_connection():
            stack: AsyncExitStack | None = None
            session = None
            try:
                _, session, stack = await open_single_server(name, cfg)
                if not ready.done():
                    ready.set_result(stack is not None)
                if stack is None:
                    return

                # 健康监督: 定期 ping，失败判定连接假死
                # 断开的异常发生在 SDK 内部后台 reader task，
                # 不会冒到本 task——只能靠主动探测发现。
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
                # asyncio.wait 在 3.13 禁止传 coroutine
                # （"Passing coroutines is forbidden"）——Event.wait()
                # 必须先包成 task。否则连接成功走到这里立即 TypeError，
                # owner 死亡且 coroutine 泄漏（"was never awaited" 警告）
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

        owner = asyncio.create_task(own_connection(), name=f"mcp:{name}")
        connection = MCPConnection(owner, close_requested, dead)

        connected = False
        try:
            connected = await ready
        except BaseException:
            # 不 cancel owner——cancel 会打断 finally 里的
            # stack.aclose()（CancelledError 是 BaseException，
            # except Exception 接不住），sse_client 生成器泄漏给
            # GC，事件循环关闭时跨 task athrow 打印 RuntimeError
            # 噪音。dead.set() 让 owner 的 wait 自然返回走完整清理
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

    # 只处理恰好一个null，一个非null的情况
    if has_null and len(non_null) == 1:
        return non_null[0], True
    return None


def normlize_schema_for_openai(raw_schema):
    """把 MCP schema 规范化为 OpenAI 兼容格式。

    去除 OpenAI 不支持的 null / anyOf / oneOf 以及多参数类型形式。

    Args:
        raw_schema: MCP 工具 inputSchema。

    Returns:
        规范化后的 schema dict（非 dict 输入返回空 object 兜底）。
    """
    if not isinstance(raw_schema, dict):
        return {"type": "object", "properties": {}}

    # 复制一份schema
    dict_schema = dict(raw_schema)

    raw_type = dict_schema.get("type")
    if isinstance(raw_type, list):
        # 只处理一个null，一个非null的情况，对于多个类型加null的情况，不处理
        # 等待报错后，交给用户自己处理，openai不支持这种格式
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            dict_schema["type"] = non_null[0]
            dict_schema["nullable"] = True

    for key in ("oneOf", "anyOf"):
        branches = dict_schema.get(key)
        if not branches:
            continue
        nullable_branch = _extract_nullable_branch(branches)
        # 返回值可能是None，或者两个返回值，所以不能直接解包
        if nullable_branch:
            branch, _ = nullable_branch
            # 删除key，并且将branch添加进去,这里使用了创建新对像，然后拷贝的做法
            merged = {k: v for k, v in dict_schema.items() if k != key}
            merged.update(branch)
            dict_schema = merged

            dict_schema["nullable"] = True
            break

    if "properties" in dict_schema and isinstance(dict_schema["properties"], dict):
        # 对properties中嵌套的字典进行处理
        dict_schema["properties"] = {
            name: normlize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in dict_schema["properties"].items()
        }

    if "items" in dict_schema and isinstance(dict_schema["items"], dict):
        # 对items同样处理
        dict_schema["items"] = normlize_schema_for_openai(dict_schema["items"])

    if dict_schema.get("type") != "object":
        return dict_schema

    # 对含有object属性的字典，初始化dict_schema默认值，包括初始最上层和可能的嵌套情况
    dict_schema.setdefault("properties", {})
    dict_schema.setdefault("required", [])
    return dict_schema


# OpenAI function name 规范: 只允许 [a-zA-Z0-9_-]，最长 64 字符
_MAX_TOOL_NAME_LEN = 64


def _sanitize_tool_name(name: str) -> str:
    """把 MCP 工具名规范化为 OpenAI 兼容的函数名。

    规则:
    1. 非法字符 → 下划线（中文等整体剔除）
    2. 连续下划线折叠
    3. 首尾 _ - 去除
    4. 全空 → 兜底名 mcp_tool
    5. 超 64 截断
    6. 纯非 ASCII 名（如"天气查询"）→ 附 8 位稳定 hash，
       保证不同中文工具名不互相覆盖（weather_8f3a2b1c）

    原始名保存在 MCPToolWrapper.original_name，调用时用原名。
    """
    result = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    result = re.sub(r"_+", "_", result)
    result = result.strip("_-")
    if not result:
        result = "mcp_tool"
    if re.fullmatch(r"[a-zA-Z0-9_-]+", name) is None:
        # 原名含非 ASCII（如中文）——hash 保证区分度
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        result = f"{result}_{digest}"
    return result[:_MAX_TOOL_NAME_LEN].rstrip("_-")


class MCPToolWrapper(BaseTool):
    """将 MCP 工具适配为统一的 ToolRegistry 执行协议。"""

    # 外部 MCP 工具的副作用未知；除非将来由 MCP 元数据明确声明幂等性，
    # 不能自动重试可能已成功的远端写操作。
    side_effect = "irreversible"

    def __init__(self, session, server_name, tool_def, tool_timeout: int = 30):
        self.server_name = server_name
        self.session = session
        self.tool_def = tool_def
        self.raw_schema = tool_def.inputSchema or {"type": "object", "properities": {}}
        self.timeout_seconds = tool_timeout
        self.original_name = tool_def.name

        # 有些 MCP 提供者的函数名是中文/以数字开头等，不符合 OpenAI 规范，需要重命名。
        # server 名做前缀（自己配置的、可控），不同 server 的同名工具天然不冲突，
        # LLM 也能从名字看出工具归属（如 weather_search / filesystem_search）。
        self.name = f"{server_name}_{_sanitize_tool_name(tool_def.name)}"
        self.description = tool_def.description
        self.parameters = normlize_schema_for_openai(self.raw_schema)

    async def execute_once(self, **kwargs):
        """调用 MCP 工具一次（必须传原名，不是 sanitize 后的名字）。

        timeout、取消和异常分类由 ``ToolRegistry`` 统一处理。
        """
        result = await self.session.call_tool(self.original_name, arguments=kwargs)
        return self._render_call_result(result.content, kwargs)

    def _render_call_result(self, content, arguments):
        """渲染 MCP 调用结果——可能包含图片，暂时只提取文本。

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


class MCPResourceWrapper:
    """TODO: MCP Resources 支持（URI 寻址 + 动态发现）——未实现，不注册。"""

    pass


class MCPPromptWrapper:
    """TODO: MCP Prompts 支持——未实现，不注册。"""

    pass

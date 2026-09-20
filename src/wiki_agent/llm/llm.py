import inspect
import json
from collections.abc import Awaitable, Callable
from typing import cast

import openai
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from openai.types.chat.completion_create_params import ResponseFormat

from wiki_agent.config import LLMConfig, RetryConfig
from wiki_agent.conversation import LLMResponse, Message, ToolCall
from wiki_agent.errors import translate_openai_error
from wiki_agent.llm.rate_limit import RequestLimiter
from wiki_agent.log import get_logger
from wiki_agent.utils.helpers import estimate_text_tokens

logger = get_logger("LLMCLIENT")


def _openai_messages(messages: list[Message]) -> list[ChatCompletionMessageParam]:
    """Serialize internal messages at the boundary to the OpenAI SDK."""
    return cast(list[ChatCompletionMessageParam], [message.openai_schema for message in messages])


def _parse_tool_calls(openai_calls) -> list[ToolCall]:
    """将 OpenAI 工具调用转换为内部 ToolCall 列表。

    arguments 解析失败不丢弃调用本身——空 args + 警告（调用存在
    是事实，参数坏是 LLM 的错；丢弃会让上层误以为没调用）。

    Args:
        openai_calls: OpenAI 响应中的 tool_calls 列表。

    Returns:
        内部 ToolCall 列表（坏参数调用保留空 arguments）。
    """
    result: list[ToolCall] = []
    for tc in openai_calls:
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "工具调用 %s 参数解析失败——保留空参数: %.80s",
                tc.function.name,
                tc.function.arguments,
            )
            args = {}
        result.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
    return result


class LLMClient:
    def __init__(
        self,
        config: LLMConfig,
        retry_config: RetryConfig | None = None,
        request_limiter: RequestLimiter | None = None,
    ):
        """初始化 LLM 客户端。

        构造即完整——配置注入后立刻可用，无中间态。

        Args:
            config: LLM 配置（api_key/base_url/model_id）。
        """
        self.api_key: str = config.api_key
        self.base_url: str = config.base_url
        self.model_id: str = config.model_id
        # ``enabled`` is the provider default and sends no vendor-specific
        # field; ``disabled`` uses the compatible-mode switch supported by the
        # configured OpenAI-compatible endpoint.
        self.default_extra_body = (
            {"thinking": {"type": "disabled"}} if config.thinking == "disabled" else None
        )
        # 注入 RootConfig.retry；默认保留给直接构造客户端的测试兼容路径。
        self.retry_config = retry_config or RetryConfig()
        self.request_limiter = request_limiter or RequestLimiter(
            max_concurrency=config.max_concurrency,
            requests_per_minute=config.requests_per_minute,
            tokens_per_minute=config.tokens_per_minute,
        )

        self.client = openai.Client(
            api_key=self.api_key, base_url=self.base_url, timeout=config.timeout
        )
        self.async_client = openai.AsyncClient(
            api_key=self.api_key, base_url=self.base_url, timeout=config.timeout
        )
        logger.info(
            "请求限流器已启用: concurrency=%d rpm=%d tpm=%d",
            config.max_concurrency,
            config.requests_per_minute,
            config.tokens_per_minute,
        )

    @staticmethod
    def _estimate_request_tokens(
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None,
        max_tokens: int | None,
    ) -> int:
        """为 TPM 限制预留输入和最大输出预算。"""
        text = "".join(message.text_schema for message in messages)
        if tools:
            text += json.dumps(tools, ensure_ascii=False, default=str)
        # 图像的 token 算法由 provider 决定；每张预留固定额，
        # 避免将 base64 字节数误计为文本 token。
        image_budget = sum(len(message.images) for message in messages) * 1_024
        return max(1, estimate_text_tokens(text) + image_budget + (max_tokens or 0))

    def _request_extra_body(self, extra_body: dict | None) -> dict | None:
        """Return explicit request options or the configured reasoning policy."""
        return self.default_extra_body if extra_body is None else extra_body

    def invoke(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        extra_body: dict | None = None,
    ) -> LLMResponse:
        """在全局请求预算内执行同步调用。"""
        estimated_tokens = self._estimate_request_tokens(messages, tools, max_tokens)
        with self.request_limiter.slot(estimated_tokens):
            return self._invoke(messages, tools, max_tokens, temperature, extra_body)

    def _invoke(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        extra_body: dict | None = None,
    ) -> LLMResponse:
        """同步非流式调用（内部工具，测试/脚本用）。

        Args:
            messages: 消息列表（内部 Message 格式）。
            tools: OpenAI 工具 schema 列表。
            max_tokens: 生成 token 上限。
            temperature: 采样温度。
            extra_body: 附加请求体参数（如 thinking 开关）。

        Returns:
            组装好的 LLMResponse。

        Raises:
            翻译后的三分类异常（RetryableError/HandleableError/FatalError）。
        """
        try:
            response = self.client.chat.completions.create(
                messages=_openai_messages(messages),
                model=self.model_id,
                tools=tools or [],
                max_tokens=max_tokens,
                temperature=temperature,
                extra_body=self._request_extra_body(extra_body),
            )

            llm_response = LLMResponse()
            llm_response.finish_reason = response.choices[0].finish_reason
            message = response.choices[0].message
            llm_response.content = message.content or ""
            # reasoning 模型（如 deepseek-v4-flash）把思考放独立字段。
            # 不读它时思考会静默吃掉整个 max_tokens 预算、content 留空——
            # 捕获下来用于诊断与日志（编译流水线用 thinking=disabled 关掉它）
            llm_response.reasoning_content = getattr(message, "reasoning_content", "") or ""

            if message.tool_calls:
                llm_response.tool_calls = _parse_tool_calls(message.tool_calls)

            return llm_response
        except Exception as e:
            # 翻译成三分类异常——类型携带策略，上层按类型决策重试/放弃
            raise translate_openai_error(e) from e

    async def async_invoke(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        extra_body: dict | None = None,
        response_format: ResponseFormat | None = None,
    ) -> LLMResponse:
        """在全局请求预算内执行异步调用。"""
        estimated_tokens = self._estimate_request_tokens(messages, tools, max_tokens)
        async with self.request_limiter.async_slot(estimated_tokens):
            return await self._async_invoke(
                messages, tools, max_tokens, temperature, extra_body, response_format
            )

    async def _async_invoke(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        extra_body: dict | None = None,
        response_format: ResponseFormat | None = None,
    ) -> LLMResponse:
        """异步非流式调用。

        Args:
            messages: 消息列表（内部 Message 格式）。
            tools: OpenAI 工具 schema 列表。
            max_tokens: 生成 token 上限。
            temperature: 采样温度。
            extra_body: 附加请求体参数（如 thinking 开关）。
            response_format: API 级输出格式（如 {"type": "json_object"}）——
                编译阶段用它消灭 fence/前言类格式噪声重试；None 不传。

        Returns:
            组装好的 LLMResponse（含 usage 与 cache_hit/cache_miss）。

        Raises:
            翻译后的三分类异常。
        """
        try:
            response = await self.async_client.chat.completions.create(
                messages=_openai_messages(messages),
                model=self.model_id,
                tools=tools or [],
                max_tokens=max_tokens,
                temperature=temperature,
                extra_body=self._request_extra_body(extra_body),
                # SDK 签名不收 None——省略用 NOT_GIVEN 哨兵（等同不传）
                response_format=response_format if response_format is not None else openai.omit,
            )
            llm_response = LLMResponse()
            llm_response.finish_reason = response.choices[0].finish_reason
            message = response.choices[0].message
            llm_response.content = message.content or ""
            llm_response.reasoning_content = getattr(message, "reasoning_content", "") or ""
            usage = response.usage
            llm_response.usage = {
                "prompt": usage.prompt_tokens if usage else 0,
                "completion": usage.completion_tokens if usage else 0,
                "total": usage.total_tokens if usage else 0,
                # 磁盘缓存命中监控——prompt cache 纪律的执行机制
                "cache_hit": getattr(usage, "prompt_cache_hit_tokens", 0) or 0,
                "cache_miss": getattr(usage, "prompt_cache_miss_tokens", 0) or 0,
            }

            if message.tool_calls:
                llm_response.tool_calls = _parse_tool_calls(message.tool_calls)
            return llm_response
        except Exception as e:
            logger.warning("异步调用失败: %s", type(e).__name__)
            raise translate_openai_error(e) from e

    async def async_stream(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        on_delta: Callable[[str], None] | Callable[[str], Awaitable[None]] | None = None,
        extra_body: dict | None = None,
    ) -> LLMResponse:
        """在全局请求预算内执行异步流式调用。"""
        estimated_tokens = self._estimate_request_tokens(messages, tools, max_tokens)
        async with self.request_limiter.async_slot(estimated_tokens):
            return await self._async_stream(
                messages, tools, max_tokens, temperature, on_delta, extra_body
            )

    async def _async_stream(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        on_delta: Callable[[str], None] | Callable[[str], Awaitable[None]] | None = None,
        extra_body: dict | None = None,
    ) -> LLMResponse:
        """基于回调的流式调用。

        流式过程中每收到一段文本即调用 ``on_delta(delta)``，
        返回组装好的 ``LLMResponse``（content + tool_calls + usage）。

        on_delta 支持同步/异步两种回调（返回值 awaitable 则 await）——
        订阅方（agent 的 on_stream_delta hook）是 async 的，
        流式增量要按序触发。

        Args:
            messages: 消息列表（内部 Message 格式）。
            tools: OpenAI 工具 schema 列表。
            max_tokens: 生成 token 上限。
            temperature: 采样温度。
            on_delta: 每收到一段文本调用一次的回调（同步或异步）。

        Returns:
            组装好的 LLMResponse（content + tool_calls + usage）。

        Raises:
            翻译后的三分类异常。
        """
        tool_calls_buffer: dict[int, dict[str, str]] = {}
        content_buffer = ""
        tool_calls_list: list[ToolCall] = []
        finish_reason_str = None
        usage_info: dict[str, int] = {}

        try:
            async_stream_response = await self.async_client.chat.completions.create(
                messages=_openai_messages(messages),
                model=self.model_id,
                tools=tools or [],
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=self._request_extra_body(extra_body),
            )

            async for chunk in async_stream_response:
                if chunk.usage:
                    usage_info = {
                        "prompt": chunk.usage.prompt_tokens,
                        "completion": chunk.usage.completion_tokens,
                        "total": chunk.usage.total_tokens,
                        # 磁盘缓存命中监控（流式最后一个 chunk 才带 usage）
                        "cache_hit": getattr(chunk.usage, "prompt_cache_hit_tokens", 0) or 0,
                        "cache_miss": getattr(chunk.usage, "prompt_cache_miss_tokens", 0) or 0,
                    }

                # OpenAI-compatible providers may emit a final usage-only
                # chunk with ``choices=[]`` when stream_options includes
                # usage.  It is valid metadata, not a generation failure.
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if finish_reason:
                    finish_reason_str = finish_reason

                if delta.content:
                    content_buffer += delta.content
                    if on_delta:
                        result = on_delta(delta.content)
                        if inspect.isawaitable(result):
                            await result

                if delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        idx = tool_call.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                        buffer = tool_calls_buffer[idx]

                        if tool_call.id:
                            buffer["id"] += tool_call.id

                        if tool_call.function:
                            if tool_call.function.name:
                                buffer["name"] += tool_call.function.name
                            if tool_call.function.arguments:
                                buffer["arguments"] += tool_call.function.arguments

            for tool_call in tool_calls_buffer.values():
                try:
                    tool_calls_list.append(
                        ToolCall(
                            id=tool_call["id"],
                            name=tool_call["name"],
                            arguments=json.loads(tool_call["arguments"]),
                        )
                    )
                except json.JSONDecodeError:
                    # 保留空参数的调用——调用存在是事实（与 _parse_tool_calls 同语义）
                    logger.warning("工具调用 %s 参数解析失败——保留空参数", tool_call["name"])
                    tool_calls_list.append(
                        ToolCall(id=tool_call["id"], name=tool_call["name"], arguments={})
                    )

        except Exception as e:
            raise translate_openai_error(e) from e

        return LLMResponse(
            finish_reason=finish_reason_str or "",
            content=content_buffer,
            tool_calls=tool_calls_list,
            usage=usage_info,
        )

    def stream(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        on_delta: Callable[[str], None] | None = None,
        extra_body: dict | None = None,
    ) -> LLMResponse:
        """在全局请求预算内执行同步流式调用。"""
        estimated_tokens = self._estimate_request_tokens(messages, tools, max_tokens)
        with self.request_limiter.slot(estimated_tokens):
            return self._stream(messages, tools, max_tokens, temperature, on_delta, extra_body)

    def _stream(
        self,
        messages: list[Message],
        tools: list[ChatCompletionToolParam] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.5,
        on_delta: Callable[[str], None] | None = None,
        extra_body: dict | None = None,
    ) -> LLMResponse:
        """基于回调的同步流式调用。

        Args:
            messages: 消息列表（内部 Message 格式）。
            tools: OpenAI 工具 schema 列表。
            max_tokens: 生成 token 上限。
            temperature: 采样温度。
            on_delta: 每收到一段文本调用一次的回调（仅同步）。

        Returns:
            组装好的 LLMResponse（content + tool_calls）。

        Raises:
            翻译后的三分类异常。
        """
        tool_calls_buffer: dict[int, dict[str, str]] = {}
        content_buffer = ""
        tool_calls_list: list[ToolCall] = []
        finish_reason_str = None
        try:
            stream_response = self.client.chat.completions.create(
                messages=_openai_messages(messages),
                model=self.model_id,
                tools=tools or [],
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=self._request_extra_body(extra_body),
            )

            for chunk in stream_response:
                # Some compatible endpoints append a usage-only chunk with
                # no choices.  Ignore it instead of indexing an empty list.
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if finish_reason:
                    finish_reason_str = finish_reason

                if delta.content:
                    content_buffer += delta.content
                    if on_delta:
                        on_delta(delta.content)

                if delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        idx = tool_call.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}

                        if tool_call.id:
                            tool_calls_buffer[idx]["id"] += tool_call.id

                        if tool_call.function:
                            if tool_call.function.name:
                                tool_calls_buffer[idx]["name"] += tool_call.function.name

                            if tool_call.function.arguments:
                                tool_calls_buffer[idx]["arguments"] += tool_call.function.arguments

            for tool in tool_calls_buffer.values():
                try:
                    args = json.loads(tool["arguments"])
                    tool_calls_list.append(
                        ToolCall(id=tool["id"], name=tool["name"], arguments=args)
                    )
                except json.JSONDecodeError:
                    # 保留空参数的调用（与 _parse_tool_calls 同语义）
                    logger.warning("工具调用 %s 参数解析失败——保留空参数", tool["name"])
                    tool_calls_list.append(ToolCall(id=tool["id"], name=tool["name"], arguments={}))

        except Exception as e:
            logger.warning("同步流式生成失败: %s", type(e).__name__)
            raise translate_openai_error(e) from e

        return LLMResponse(
            finish_reason=finish_reason_str or "",
            content=content_buffer,
            tool_calls=tool_calls_list,
        )


if __name__ == "__main__":
    from pathlib import Path

    from wiki_agent.config import load_config

    root = Path(__file__).parent.parent.parent.parent
    client = LLMClient(load_config(project_root=root).llm)

    messages = [Message(role="user", content="hello")]
    response = client.invoke(messages)
    print(response.content)
    print(response.finish_reason)

    # import time

    # # 先测单次平均耗时
    # print("=" * 50)
    # h = [Message(role="user", content="hello")]
    # t = time.time()
    # client.invoke(h)
    # avg = time.time() - t
    # print(f"单次请求耗时: {avg:.2f}s")
    # print(f"估算 9 条同步: {avg*9:.1f}s")

    # async def call_one(query):
    #     h = ChatHistory(messages=Message(role="user", content=query))
    #     return await client.async_invoke(h)

    # async def test(count):
    #     tasks = [asyncio.create_task(call_one(f"一句话介绍第{i}种语言")) for i in range(count)]
    #     start = time.time()
    #     await asyncio.gather(*tasks)
    #     return time.time() - start

    # print("\n" + "=" * 50)
    # print("异步并发测试:")
    # for n in [3, 6, 9]:
    #     elapsed = asyncio.run(test(n))
    #     print(f"  {n} 条并发 → {elapsed:.2f}s (≈同步 {elapsed/n:.2f}s/条)")

    # print(f"\n结论: 3 条 {asyncio.run(test(3)):.1f}s, 9 条 {asyncio.run(test(9)):.1f}s")
    # print(f"请求量 3x, 耗时不随请求数线性增长")

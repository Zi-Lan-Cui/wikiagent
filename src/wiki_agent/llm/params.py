"""LLM 请求参数常量：跨各线共享的调用形状。"""

from __future__ import annotations

from openai.types.shared_params import ResponseFormatJSONObject

# 关闭 thinking：reasoning 模型的思考段可能占满 max_tokens
# 预算导致 content 为空。凡是要稳定拿正文的调用共用此常量。
NO_THINKING = {"thinking": {"type": "disabled"}}

# API 级 JSON 模式，输出必为合法 JSON 对象。check/parse 层仍接受顶层数组，
# 防兼容端点静默忽略该参数。
JSON_MODE: ResponseFormatJSONObject = {"type": "json_object"}

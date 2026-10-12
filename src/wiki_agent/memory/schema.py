"""dream 结构化输出的 JSON Schema（LLMClient.response_format 直传）。

统一字段 + op 枚举，不用 oneOf——部分兼容端点对 oneOf 支持不稳；
不适用当前 op 的字段填 null/空数组。ops 为空数组是合法的"本批无可改"出口。
sources 不在 schema 中：溯源由程序从 evidence 对应历史记录推导，模型无权填造。
"""

from __future__ import annotations

from openai.types.shared_params.response_format_json_schema import (
    JSONSchema,
    ResponseFormatJSONSchema,
)

MEMORY_OPS_SCHEMA: JSONSchema = {
    "name": "memory_ops",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["ops"],
        "properties": {
            "ops": {
                "type": "array",
                "description": "本批记忆操作列表；无可改动时为空数组",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "op",
                        "target",
                        "text",
                        "kind",
                        "persistence",
                        "evidence",
                        "reason",
                        "by",
                    ],
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["add", "revise", "confirm", "invalidate", "reactivate"],
                            "description": "操作类型",
                        },
                        "target": {
                            "type": ["string", "null"],
                            "description": "被操作条目 id（形如 m_0007）；add 时为 null",
                        },
                        "text": {
                            "type": ["string", "null"],
                            "description": "add/revise 的条目文本；其余为 null",
                        },
                        "kind": {
                            "type": ["string", "null"],
                            "enum": [
                                "preference",
                                "identity",
                                "project",
                                "knowledge",
                                "task_state",
                                None,
                            ],
                            "description": "内容分类，仅 add 必填；其余为 null",
                        },
                        "persistence": {
                            "type": ["string", "null"],
                            "enum": ["permanent", "durable", "ephemeral", None],
                            "description": "存活期档位，仅 add 必填；其余为 null",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "支撑该操作的历史记录 cursor 行号，必须落在本批区间内；add 之外可为空",
                        },
                        "reason": {
                            "type": ["string", "null"],
                            "enum": ["superseded", "contradicted", "obsolete", None],
                            "description": "invalidate 的失效原因；其余为 null",
                        },
                        "by": {
                            "type": ["string", "null"],
                            "description": "invalidate 专用：取代者 id；指向本批 add 时用 #序号 引用（#0 为本批第一条 op）；其余为 null",
                        },
                    },
                },
            }
        },
    },
}

MEMORY_OPS_RESPONSE_FORMAT: ResponseFormatJSONSchema = {
    "type": "json_schema",
    "json_schema": MEMORY_OPS_SCHEMA,
}

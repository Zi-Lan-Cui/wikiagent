"""集成层解析——把 LLM 原始输出变成模型对象。

与 checks 分工: checks 定义"什么算好输出"，本模块把校验过的输出
变成对象。解析信任校验结果——能到这里的原始内容必然已通过 check，
再解析失败是 bug，炸出来。
"""

from __future__ import annotations

import json
import re

from wiki_agent.compiler.models import (
    AnalysisResult,
    Disposition,
    IntegrationPlan,
    PageRelationship,
    PageTarget,
)
from wiki_agent.log import get_logger

logger = get_logger("PARSE")


def _try_repair_trailing_braces(cleaned: str) -> str | None:
    """确定性修复: 仅缺尾部闭合括号时补全（审计 I5 根因）。

    I5 实锤: 深嵌套 JSON（page_targets 数组套 references 数组）模型
    写完就停（finish=stop 非 token 截断），但少写一个尾部 }——重试
    两次仍犯同样错，烧 token 无收益。这里保守修复: 只尝试补
    ]} 组合，补完能 loads 才返回修复版，否则 None（不掩盖真截断）。

    Args:
        cleaned: 剥除 fence 后的 JSON 文本。

    Returns:
        补全后的 JSON 文本；无法修复返回 None。
    """
    import json as _json

    for closing in ("}", "]}", "}]}", "}]}]", "]}"):
        candidate = cleaned + closing
        try:
            _json.loads(candidate)
            return candidate
        except _json.JSONDecodeError:
            continue
    return None


def _strip_fence(content: str) -> str:
    """剥 LLM 输出的格式噪声——校验与解析共享的格式规约。

    fence 是格式化噪声不是内容错误：check 层和 parse 层必须用
    同一份剥除逻辑，否则出现"check 剥了能过、parse 没剥就炸"
    （2026-08-15 full_pipeline 验收 run 事故）。

    尾部缺闭合括号（I5）是同类格式化噪声——模型深嵌套 JSON
    少写一个 }。repair 也在此层: check 用修复版判定、parse 用
    修复版解析，两端自动一致。

    Args:
        content: LLM 原始输出。

    Returns:
        剥除 fence（可能含 I5 补全）后的文本。
    """
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # 仅当 loads 失败且补尾部 ]} 能修复时才补（确定性，不掩盖真截断）
    import json as _json

    try:
        _json.loads(cleaned)
    except _json.JSONDecodeError:
        repaired = _try_repair_trailing_braces(cleaned)
        if repaired is not None:
            logger.warning(
                "JSON 尾部缺闭合括号——已补全 %d 字符（I5 repair）", len(repaired) - len(cleaned)
            )
            return repaired
    return cleaned


def _parse_search_result(raw: str) -> list[str]:
    """解析 search 阶段 LLM 返回的 JSON 数组。

    路径规范化兜底: LLM 可能输出 wiki/ 前缀、缺 .md 后缀，
    统一 normalize 后再去重——避免 analyze 拼出 wiki/wiki/ 双路径。

    Args:
        raw: LLM 原始输出。

    Returns:
        规范化后的相对路径列表（坏数据跳过）。
    """
    raw = _strip_fence(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    # 新契约 {"paths": [...]}；兼容顶层数组（老 run 的 raw 证据、
    # 静默忽略 response_format 的端点）——解析宽进，prompt 严请。
    if isinstance(data, dict):
        data = data.get("paths", [])
    if not isinstance(data, list):
        return []

    seen: set[str] = set()
    paths: list[str] = []
    for item in data:
        if not isinstance(item, str) or not item.strip():
            continue
        path = _normalize_wiki_path(item)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _parse_analysis(raw: str, source_identity: str) -> AnalysisResult:
    """解析 analyze 两段式输出 → AnalysisResult。

    Args:
        raw: LLM 原始输出。
        source_identity: 源文档标识（写入结果）。

    Returns:
        分析结果（自由文本缺失时回退用原始输出）。
    """
    free_part, json_part = _extract_analyze_parts(raw)

    data: dict = {}
    if json_part:
        try:
            parsed = json.loads(json_part)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            pass

    return AnalysisResult(
        source_identity=source_identity,
        analysis_text=free_part or raw,
        raw_analysis=raw,
        entities=data.get("entities", []),
        concepts=data.get("concepts", []),
        relationships=[
            PageRelationship(
                from_page=r.get("from", ""),
                to_page=r.get("to", ""),
                relation=r.get("relation", ""),
                detail=r.get("detail", ""),
            )
            for r in data.get("relationships", [])
            if isinstance(r, dict)
        ],
    )


def _parse_plan(raw: str) -> IntegrationPlan:
    """解析 plan JSON 输出 → IntegrationPlan。

    fence 容错与 check_plan_json 共享 _strip_fence——能到这的
    内容必然已通过校验，解析失败是 bug，炸出来而不是吞掉
    （旧 _try_parse_json 的 JSONDecodeError 抢救分支已不可达）。

    Args:
        raw: LLM 原始输出。

    Returns:
        集成计划（路径/标题规范化，非法 disposition 跳过）。
    """
    data = json.loads(_strip_fence(raw))

    targets = []
    for t in data.get("page_targets", []):
        try:
            disposition = Disposition(t.get("disposition", "new"))
        except ValueError:
            # check_plan_json 已拦住非法 disposition（retry 后仍非法才到这）
            logger.warning("非法 disposition %r，跳过该 target", t.get("disposition"))
            continue
        targets.append(
            PageTarget(
                wiki_path=_normalize_wiki_path(t.get("wiki_path", "")),
                title=_clean_title(t.get("title", "")),
                disposition=disposition,
                reason=t.get("reason", ""),
                references=_parse_references(t.get("references", [])),
                page_type=str(t.get("page_type", "")).strip(),
            )
        )
    return IntegrationPlan(page_targets=targets)


def _parse_references(raw: list | None) -> list[dict[str, str]]:
    """安全解析 references 列表。

    Args:
        raw: 原始引用列表。

    Returns:
        {"slug", "reason"} dict 列表；坏数据跳过。
    """
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict) and "slug" in item:
            result.append(
                {
                    "slug": str(item.get("slug", "")),
                    "reason": str(item.get("reason", "")),
                }
            )
    return result


def _extract_headings(content: str, max_depth: int = 3) -> str:
    """提取页面标题（# / ## / ###），返回缩进大纲字符串。

    Args:
        content: 页面内容。
        max_depth: 最大标题层级（默认 3）。

    Returns:
        缩进大纲文本；无标题或空内容返回空串。
    """
    if not content:
        return ""
    lines_out: list[str] = []
    in_body = False
    for line in content.split("\n"):
        stripped = line.strip()
        if not in_body:
            if stripped == "---":
                in_body = True
            continue
        match = re.match(rf"^(#{{1,{max_depth}}})\s+(.+)$", stripped)
        if match:
            depth = len(match.group(1))
            lines_out.append("  " * (depth - 1) + "- " + match.group(2))
    return "\n".join(lines_out) if lines_out else ""


def _extract_analyze_parts(content: str) -> tuple[str, str]:
    """拆解 analyze 两段式输出 → (自由文本主体, JSON 尾巴文本)。

    支持三种形态:
    1. ```json ... ``` 包裹的尾巴在末尾（标准形态）
    2. 无 fence 的裸 JSON 在末尾
    3. 纯 JSON（无自由文本——宽容处理）

    Args:
        content: LLM 原始输出。

    Returns:
        (自由文本主体, JSON 尾巴文本)。
    """
    text = content.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n?```\s*$", text, re.S)
    if m:
        json_part = m.group(1)
        free_part = text[: m.start()].strip()
        return free_part, json_part
    # 无 fence: 找最后一个完整 JSON 对象
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        json_part = text[first : last + 1]
        free_part = text[:first].strip()
        return free_part, json_part
    return text, ""


def _clean_title(title: str) -> str:
    """去掉 LLM 可能包裹的 [[ ]] wikilink 语法。

    Args:
        title: 原始标题。

    Returns:
        清理后的标题。
    """
    title = title.strip()
    if title.startswith("[[") and title.endswith("]]"):
        title = title[2:-2].strip()
    return title


def _normalize_wiki_path(path: str) -> str:
    """规范化 wiki 相对路径。

    去掉 LLM 可能输出的 wiki/ 前缀，补 .md 后缀。

    Args:
        path: 原始路径。

    Returns:
        规范化后的相对路径（如 concepts/xxx.md）。
    """
    path = path.strip().replace("\\", "/")
    while path.startswith("wiki/"):
        path = path[len("wiki/") :]
    if not path.endswith(".md"):
        path += ".md"
    return path

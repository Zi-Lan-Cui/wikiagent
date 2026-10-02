"""集成层解析：把 LLM 原始输出转成模型对象。

与 checks 分工：checks 定义合格标准，本模块把校验过的输出转成对象。
解析信任校验结果——到这里的内容已过 check，再解析失败是程序 bug，抛异常。
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
    """确定性修复：仅在缺尾部闭合括号时补全。

    深嵌套 JSON 下模型常少写尾部 }，重试无法纠正这类错误。只尝试
    ]} 组合，补完能 loads 才返回修复版，不掩盖真正的截断。

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


def strip_fence(content: str) -> str:
    """剥除 LLM 输出的格式噪声，check 与 parse 共享的格式规约。

    fence 包裹与尾部缺闭合括号都是格式噪声。check 层和 parse 层必须
    用同一份剥除逻辑，否则会出现 check 通过、parse 失败的不一致；
    括号补全也在本层完成，两端自动一致。

    Args:
        content: LLM 原始输出。

    Returns:
        剥除 fence（可能含尾部括号补全）后的文本。
    """
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    import json as _json

    try:
        _json.loads(cleaned)
    except _json.JSONDecodeError:
        repaired = _try_repair_trailing_braces(cleaned)
        if repaired is not None:
            logger.warning("JSON 尾部缺闭合括号——已补全 %d 字符", len(repaired) - len(cleaned))
            return repaired
    return cleaned


def parse_search_result(raw: str) -> list[str]:
    """解析 search 阶段 LLM 返回的 JSON 数组。

    统一 normalize 路径再去重：LLM 可能输出 wiki/ 前缀、缺 .md 后缀，
    不处理会让下游拼出 wiki/wiki/ 双路径。

    Args:
        raw: LLM 原始输出。

    Returns:
        规范化后的相对路径列表（坏数据跳过）。
    """
    raw = strip_fence(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    # 契约是 {"paths": [...]}，顶层数组一并接受：兼容忽略
    # response_format 的端点
    if isinstance(data, dict):
        data = data.get("paths", [])
    if not isinstance(data, list):
        return []

    seen: set[str] = set()
    paths: list[str] = []
    for item in data:
        if not isinstance(item, str) or not item.strip():
            continue
        path = normalize_wiki_path(item)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def parse_analysis(raw: str, source_identity: str) -> AnalysisResult:
    """解析 analyze 两段式输出 → AnalysisResult。

    Args:
        raw: LLM 原始输出。
        source_identity: 源文档标识（写入结果）。

    Returns:
        分析结果（自由文本缺失时回退用原始输出）。
    """
    free_part, json_part = extract_analyze_parts(raw)

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


def parse_plan(raw: str) -> IntegrationPlan:
    """解析 plan JSON 输出 → IntegrationPlan。

    输入应已通过 check_plan_json；再解析失败是程序 bug，抛异常不静默处理。

    Args:
        raw: LLM 原始输出。

    Returns:
        集成计划（路径/标题规范化，非法 disposition 跳过）。
    """
    data = json.loads(strip_fence(raw))

    targets = []
    for t in data.get("page_targets", []):
        try:
            disposition = Disposition(t.get("disposition", "new"))
        except ValueError:
            # check 已拦截非法值，走到这里说明 retry 后模型仍输出非法
            logger.warning("非法 disposition %r，跳过该 target", t.get("disposition"))
            continue
        targets.append(
            PageTarget(
                wiki_path=normalize_wiki_path(t.get("wiki_path", "")),
                title=_clean_title(t.get("title", "")),
                disposition=disposition,
                reason=t.get("reason", ""),
                references=_parse_references(t.get("references", [])),
                page_type=str(t.get("page_type", "")).strip(),
            )
        )
    return IntegrationPlan(page_targets=targets)


def _parse_references(raw: list | None) -> list[dict[str, str]]:
    """解析 references 列表。

    Args:
        raw: 原始引用列表。

    Returns:
        {"slug", "reason"} dict 列表；坏条目跳过。
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


def extract_analyze_parts(content: str) -> tuple[str, str]:
    """拆解 analyze 两段式输出 → (自由文本主体, JSON 文本)。

    支持三种形态:
    1. ```json ... ``` 包裹的 JSON 在末尾（标准形态）
    2. 无 fence 的裸 JSON 在末尾
    3. 纯 JSON，无自由文本

    Args:
        content: LLM 原始输出。

    Returns:
        (自由文本主体, JSON 文本)。
    """
    text = content.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n?```\s*$", text, re.S)
    if m:
        json_part = m.group(1)
        free_part = text[: m.start()].strip()
        return free_part, json_part
    # 无 fence：取首 { 到末 } 的片段
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


def normalize_wiki_path(path: str) -> str:
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

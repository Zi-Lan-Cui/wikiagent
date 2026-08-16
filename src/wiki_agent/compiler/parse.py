"""解析层——把 LLM 原始输出变成模型对象。

与 checks.py 的分界: checks 定义"什么算好输出"（校验），
本模块执行"好输出 → 模型对象"（解析）。解析信任校验结果——
能到这里的原始内容必然已通过 check（_parse_plan 直接 loads，
失败是 bug 炸出来）。

依赖方向: parse 不依赖 checks（checks 可能引用 _strip_fence）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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
            logger.warning("JSON 尾部缺闭合括号——已补全 %d 字符（I5 repair）",
                           len(repaired) - len(cleaned))
            return repaired
    return cleaned


def _parse_search_result(raw: str) -> list[str]:
    """解析 search 阶段 LLM 返回的 JSON 数组。

    路径规范化兜底: LLM 可能输出 wiki/ 前缀、缺 .md 后缀，
    统一 normalize 后再去重——避免 analyze 拼出 wiki/wiki/ 双路径。
    """
    raw = _strip_fence(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
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
    """解析 analyze 两段式输出 → AnalysisResult。"""
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
    # fence 容错与 _check_plan_json 共享 _strip_fence——能到这里的
    # 内容必然已通过校验，解析失败是 bug，炸出来而不是吞掉
    # （旧 _try_parse_json 的 JSONDecodeError 抢救分支已不可达）。
    data = json.loads(_strip_fence(raw))

    targets = []
    for t in data.get("page_targets", []):
        try:
            disposition = Disposition(t.get("disposition", "new"))
        except ValueError:
            # _check_plan_json 已拦住非法 disposition（retry 后仍非法才到这）
            logger.warning("非法 disposition %r，跳过该 target", t.get("disposition"))
            continue
        targets.append(PageTarget(
            wiki_path=_normalize_wiki_path(t.get("wiki_path", "")),
            title=_clean_title(t.get("title", "")),
            disposition=disposition,
            reason=t.get("reason", ""),
            references=_parse_references(t.get("references", [])),
        ))
    return IntegrationPlan(page_targets=targets)


def split_frontmatter(content: str) -> tuple[dict, str]:
    """切分 frontmatter——返回 (字段 dict, 正文)。

    全项目唯一实现（surgery/consumer/pipeline 曾各有副本——
    2026-08-17 收敛）。正文不 strip：调用方各自决定尾部处理
    （surgery 拼接要保留原文形态）。

    简单解析语义: 逐行 partition(": ")——不做完整 YAML
    （嵌套/列表/引号转义超出 wiki 页面的 frontmatter 需求）。
    """
    fm: dict = {}
    body = content
    if content.startswith("---"):
        try:
            end = content.index("\n---\n", 3)
            for line in content[len("---\n"):end].split("\n"):
                if ": " in line:
                    k, _, v = line.partition(": ")
                    fm[k.strip()] = v.strip().strip("\"'")
            body = content[end + 5:]
        except ValueError:
            pass
    return fm, body


def parse_frontmatter(path) -> dict:
    """读文件 + 解析 frontmatter——返回字段 dict。

    读失败返回空 dict（页面不可读 = 无元数据，不抛异常阻塞流水线）。
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    fm, _ = split_frontmatter(content)
    return fm


def _parse_references(raw: list | None) -> list[dict[str, str]]:
    """安全解析 references 列表。"""
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict) and "slug" in item:
            result.append({
                "slug": str(item.get("slug", "")),
                "reason": str(item.get("reason", "")),
            })
    return result


def _extract_headings(content: str, max_depth: int = 3) -> str:
    """提取页面中全部标题（# / ## / ###），返回缩进大纲字符串。"""
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
        match = re.match(r"^(#{1,%d})\s+(.+)$" % max_depth, stripped)
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
    """
    text = content.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n?```\s*$", text, re.S)
    if m:
        json_part = m.group(1)
        free_part = text[:m.start()].strip()
        return free_part, json_part
    # 无 fence: 找最后一个完整 JSON 对象
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        json_part = text[first:last + 1]
        free_part = text[:first].strip()
        return free_part, json_part
    return text, ""


def _clean_title(title: str) -> str:
    """去掉 LLM 可能包裹的 [[ ]] wikilink 语法。"""
    title = title.strip()
    if title.startswith("[[") and title.endswith("]]"):
        title = title[2:-2].strip()
    return title


def _normalize_wiki_path(path: str) -> str:
    """去掉 LLM 可能输出的 wiki/ 前缀，补 .md 后缀，返回相对路径。"""
    path = path.strip().replace("\\", "/")
    while path.startswith("wiki/"):
        path = path[len("wiki/"):]
    if not path.endswith(".md"):
        path += ".md"
    return path

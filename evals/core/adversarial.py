"""对抗性评测样本：从已有正/负例注入【已知缺陷】，gold 由构造决定（=fail）。

目的：给评测"牙齿"——种子全 pass 时一致率无区分度。对抗例让 gold=fail，
judge 若判 pass 即 false-pass（评分器最危险、也最该证明的失效方向）。

缺陷种类：
  drop_page        删掉一张该产出的页（减少生成页面）        → A 层 integrity 可判
  strip_provenance 抹掉某页 sources 来源                      → A 层 integrity 可判
  refuse_violation 负例（应拒答）却产出了页                    → A 层 score_refusal 可判
  fabricate_claim  页里塞进 source 没有的论断                  → 只有 LLM judge 能判（测 judge 松紧）

每个 case 带 `candidate_pages`（损坏后的产出内联），judge/自测都对着它评，不污染真实 wiki。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.core.compile_graders import compile_structure_checks
from evals.core.dataset import score_refusal
from evals.core.outcomes import _parse_source_list
from wiki_agent.wiki.frontmatter import split_frontmatter


def _structure_pages(candidate: list[dict[str, str]]) -> list[dict[str, Any]]:
    out = []
    for p in candidate:
        fm, body = split_frontmatter(p["content"])
        src = fm.get("sources")
        if isinstance(src, str):
            import re

            src = re.findall(r"[\w.\-]+\.md", src)
        out.append(
            {"path": p["path"], "title": str(fm.get("title", "")), "body": body, "sources": src or []}
        )
    return out

# 注入的"编造"句子：source 里绝不含、且是通顺断言
_FABRICATION = "该特性在所有 Python 版本中均被官方标记为已废弃并将于 4.0 移除。"


def _page_pair(wiki: Path, rel: str) -> dict[str, str]:
    path = wiki / rel
    return {"path": rel, "content": path.read_text(encoding="utf-8") if path.is_file() else ""}


def _set_sources_empty(content: str) -> str:
    fm, body = split_frontmatter(content)
    fm.pop("sources", None)
    head = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n"
    return head + body


def build_adversarial_dataset(
    dataset: dict[str, Any], wiki: Path, *, per_kind: int = 3
) -> list[dict[str, Any]]:
    """从基集派生对抗例，gold=human_verdict=fail，缺陷类型已知。"""
    positives = [c for c in dataset["cases"] if c["polarity"] == "positive" and c.get("expected_pages")]
    negatives = [c for c in dataset["cases"] if c["polarity"] == "negative"]
    out: list[dict[str, Any]] = []

    for base in positives[:per_kind]:
        pages = [_page_pair(wiki, p) for p in base["expected_pages"]]
        pages = [p for p in pages if p["content"]]
        if len(pages) < 2:
            continue
        # drop_page：删掉最后一页
        out.append(_wrap(base, "drop_page", [dict(p) for p in pages[:-1]],
                         kept=pages[:-1], dropped=pages[-1]["path"]))
        # strip_provenance：抹掉第一页来源
        stripped = [dict(p) for p in pages]
        stripped[0]["content"] = _set_sources_empty(stripped[0]["content"])
        out.append(_wrap(base, "strip_provenance", stripped, target=stripped[0]["path"]))
        # fabricate_claim：仅在正文末尾追加一句 source 没有的断言（不动 frontmatter，
        # 保证 A 层 integrity 放行、只有 LLM judge 能判）
        fab = [dict(p) for p in pages]
        fab[0]["content"] = fab[0]["content"].rstrip() + f"\n\n{_FABRICATION}\n"
        out.append(_wrap(base, "fabricate_claim", fab, target=fab[0]["path"]))
        # over_split：复制一页为近重复 stub（同来源、正文近乎一致）→ 结构门禁抓
        dup_src, _ = split_frontmatter(pages[0]["content"])
        dup = {"path": "concepts/adversarial-dup.md",
               "content": pages[0]["content"].replace(
                   f'title: "{dup_src.get("title", "")}"',
                   f'title: "{dup_src.get("title", "")} 补充"',
                   1,
               )}
        out.append(_wrap(base, "over_split", [dict(p) for p in pages] + [dup]))
        # misroute：把一页写到非法目录（非 concepts/entities/topics/sources）
        mis = [dict(p) for p in pages]
        mis[0]["path"] = "misc/adversarial-misrouted.md"
        out.append(_wrap(base, "misroute", mis))

    for base in negatives[:per_kind]:
        fake = {"path": "concepts/adversarial-invention.md",
                "content": '---\ntype: concept\ntitle: "X"\nsummary: "s"\ngoal: "g"\n'
                           'sources: ["a.md"]\n---\n\n# X\n\n无依据的编造内容。\n'}
        out.append(_wrap(base, "refuse_violation", [fake]))
    return out


def _wrap(base: dict[str, Any], kind: str, candidate_pages: list[dict[str, str]], **meta: Any) -> dict[str, Any]:
    produced = {p["path"] for p in candidate_pages}
    return {
        "id": f"adv-{kind}-{base['id']}",
        "base_id": base["id"],
        "defect": kind,
        "task_type": base["task_type"],
        "polarity": base["polarity"],
        "suite": "adversarial",
        "source": base["source"],
        "source_identities": base.get("source_identities", []),
        "expected_pages": sorted(produced) if kind != "drop_page" else base["expected_pages"],
        "required_facts": base.get("required_facts", []),
        "forbidden_claims": base.get("forbidden_claims", []) or ([_FABRICATION] if kind == "fabricate_claim" else []),
        "expected_behavior": dict(base.get("expected_behavior", {})),
        "reference_solution": {"noop": False},
        "expected_verdict": "fail",
        "human_verdict": "fail",  # gold：缺陷由我们注入，正确答案必是 fail
        "reviewer": "adversarial-injection",
        "candidate_pages": candidate_pages,
        "meta": meta,
    }


def score_adversarial_deterministic(case: dict[str, Any]) -> dict[str, Any]:
    """A 层确定性抓住注入的【结构】缺陷；不跑全量 page-quality（避免孤立快照
    把指向真实页的 wikilink 误判死链）。fabricate_claim 故意让 A 层放过（留给 judge）。"""
    kind = case["defect"]
    cand = case["candidate_pages"]
    produced = {p["path"] for p in cand}

    if kind in ("over_split", "misroute"):
        structure = compile_structure_checks(_structure_pages(cand))
        return {"caught": not structure["passed"], "layer": "structure", "detail": structure}

    if kind == "refuse_violation":
        text = "\n".join(p["content"] for p in cand)
        refusal = score_refusal(case, produced_paths=produced, rendered_text=text)
        return {"caught": not refusal["passed"], "layer": "refusal"}

    if kind == "drop_page":
        missing = [p for p in case["expected_pages"] if p not in produced]
        return {"caught": bool(missing), "layer": "coverage", "detail": {"missing": missing}}

    if kind == "strip_provenance":
        target = case.get("meta", {}).get("target")
        content = next((p["content"] for p in cand if p["path"] == target), "")
        cited = _parse_source_list(split_frontmatter(content)[0].get("sources"))
        allowed = set(case.get("source_identities", []))
        caught = (not cited) or (bool(allowed) and not (set(cited) & allowed))
        return {"caught": caught, "layer": "provenance", "detail": {"cited": cited}}

    # fabricate_claim：正文编造，A 层结构门禁不判 → 只有 LLM judge 能抓
    return {"caught": False, "layer": "none", "detail": "structural gate blind to fabrication; needs LLM judge"}

"""Validate and summarize the seeded-baseline corpus without calling models."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from pydantic import ValidationError

from evals.core.reference_models import CorpusManifest, JudgementSet


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_sha256(path: Path) -> str:
    """Hash relative paths and bytes so a baseline is a reproducible snapshot."""
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        if item.is_symlink():
            raise ValueError(f"baseline 不允许符号链接: {item}")
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_corpus(manifest_path: Path) -> tuple[CorpusManifest | None, list[str]]:
    root = manifest_path.parent.resolve()
    errors: list[str] = []
    try:
        manifest = CorpusManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        return None, [str(exc)]

    for baseline in manifest.baselines:
        path = (root / baseline.wiki_path).resolve()
        if not path.is_relative_to(root) or not path.is_dir():
            errors.append(f"baseline {baseline.id}: 目录不存在或越界 {path}")
            continue
        if baseline.wiki_sha256 is not None:
            try:
                actual_hash = _tree_sha256(path)
            except ValueError as exc:
                errors.append(f"baseline {baseline.id}: {exc}")
                continue
            if actual_hash != baseline.wiki_sha256:
                errors.append(f"baseline {baseline.id}: Wiki SHA-256 不匹配")
        else:
            # wiki_sha256 空缺 = wiki 由 source 待编译生成——只要求目录存在
            # 且没有混入 .git（编译器会在输出目录初始化仓库）
            if (path / ".git").exists():
                errors.append(f"baseline {baseline.id}: wiki 待编译，不得包含 .git")
        if baseline.source_path is not None:
            source_dir = (root / baseline.source_path).resolve()
            if not source_dir.is_relative_to(root) or not source_dir.is_dir():
                errors.append(f"baseline {baseline.id}: source 目录不存在或越界 {source_dir}")
            else:
                try:
                    source_hash = _tree_sha256(source_dir)
                except ValueError as exc:
                    errors.append(f"baseline {baseline.id}: {exc}")
                else:
                    if source_hash != baseline.source_sha256:
                        errors.append(f"baseline {baseline.id}: source SHA-256 不匹配")
    return manifest, errors


def validate_judgements(manifest_path: Path) -> list[str]:
    """校验判词题集——schema、四维度覆盖、页面引用真实存在。"""
    root = manifest_path.parent.resolve()
    errors: list[str] = []
    try:
        gold = JudgementSet.model_validate_json(
            (root / "verdicts" / "judgements-v2.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        return [f"judgements-v2.json 无法解析: {exc}"]

    dimensions: dict[str, list] = {}
    for case in gold.cases:
        dimensions.setdefault(case.dimension, []).append(case)
        # 页面引用存在性
        for page in case.target.get("pages", []):
            if not (root / "baselines" / "seeded" / "wiki" / page).is_file():
                errors.append(f"{case.id}: 页面不存在 {page}")
        # source 存在性
        if not (root / "baselines" / "seeded" / "source" / case.source).is_file():
            errors.append(f"{case.id}: source 不存在 {case.source}")
    for dim in ("grounding", "coverage", "organization", "uncertainty"):
        count = len(dimensions.get(dim, []))
        if count < 3:
            errors.append(f"维度 {dim} 题量不足: {count} < 3")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 seeded 基线评测语料")
    parser.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        default=Path("evals/corpora/reference-v1/manifest.json"),
    )
    args = parser.parse_args()
    manifest, errors = validate_corpus(args.manifest)
    if manifest is None:
        print("manifest 无法解析:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    errors.extend(validate_judgements(args.manifest))
    print(f"corpus: {manifest.corpus_id} ({len(manifest.baselines)} baselines)")
    for baseline in manifest.baselines:
        print(f"  - {baseline.id}: {baseline.description[:60]}")
    if errors:
        print(f"校验失败 ({len(errors)}):")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

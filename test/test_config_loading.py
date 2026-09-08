from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from wiki_agent.config import load_config


def _write_env(root: Path) -> Path:
    env = root / "settings.env"
    env.write_text(
        "\n".join(
            (
                "LLM_API_KEY=from-env-file",
                "LLM_MODEL_ID=test-model",
                "WIKI_MATERIALS_DIR=from-dotenv-materials",
                "WIKI_WIKI_DIR=from-dotenv-wiki",
                "WIKI_WORKSPACE_DIR=from-dotenv-workspace",
                "COMPILE_CHUNK_SIZE=4096",
                "RETRY_LLM_MAX_ATTEMPTS=4",
                "LLM_THINKING=disabled",
                "LLM_MAX_CONCURRENCY=7",
                "LLM_REQUESTS_PER_MINUTE=80",
                "LLM_TOKENS_PER_MINUTE=600000",
                "VLM_MAX_CONCURRENCY=2",
                "VLM_REQUESTS_PER_MINUTE=15",
                "VLM_TOKENS_PER_MINUTE=120000",
            )
        ),
        encoding="utf-8",
    )
    return env


def test_paths_config_reads_the_selected_env_file(tmp_path: Path) -> None:
    cfg = load_config(project_root=tmp_path, env_file=_write_env(tmp_path))

    assert cfg.paths.resolved_wiki_dir() == tmp_path / "from-dotenv-wiki"
    assert cfg.paths.resolved_workspace_dir() == tmp_path / "from-dotenv-workspace"
    assert cfg.paths.resolved_materials_dir() == tmp_path / "from-dotenv-materials"
    assert cfg.paths.resolved_source_records_dir() == (
        tmp_path / "from-dotenv-workspace" / "provenance" / "sources"
    )
    assert cfg.paths.resolved_runs_dir() == tmp_path / "from-dotenv-workspace" / "runs"


def test_environment_and_overrides_take_precedence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WIKI_WIKI_DIR", "from-process-env")
    cfg = load_config(
        project_root=tmp_path,
        env_file=_write_env(tmp_path),
        overrides={"paths": {"wiki_dir": "from-override"}},
    )

    assert cfg.paths.resolved_wiki_dir() == tmp_path / "from-override"
    assert cfg.paths.resolved_workspace_dir() == tmp_path / "from-dotenv-workspace"


def test_absolute_paths_are_not_rebased(tmp_path: Path) -> None:
    absolute_wiki = tmp_path / "absolute-wiki"
    env = tmp_path / "absolute.env"
    env.write_text(
        f"LLM_API_KEY=key\nLLM_MODEL_ID=model\nWIKI_WIKI_DIR={absolute_wiki}\n",
        encoding="utf-8",
    )

    cfg = load_config(project_root=tmp_path / "project", env_file=env)

    assert cfg.paths.resolved_wiki_dir() == absolute_wiki


def test_storage_roots_must_not_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="必须是互不包含的独立目录"):
        load_config(
            project_root=tmp_path,
            env_file=_write_env(tmp_path),
            overrides={"paths": {"materials_dir": "wiki/materials", "wiki_dir": "wiki"}},
        )


def test_nested_overrides_are_deep_merged(tmp_path: Path) -> None:
    cfg = load_config(
        project_root=tmp_path,
        env_file=_write_env(tmp_path),
        overrides={"compile": {"context_window": 64_000}},
    )

    assert cfg.compile.context_window == 64_000
    assert cfg.compile.chunk_size == 4096


def test_retry_config_reads_env_and_allows_nested_override(tmp_path: Path) -> None:
    cfg = load_config(
        project_root=tmp_path,
        env_file=_write_env(tmp_path),
        overrides={"retry": {"source_base_delay_seconds": 5}},
    )

    assert cfg.retry.llm_max_attempts == 4
    assert cfg.retry.source_base_delay_seconds == 5
    assert cfg.retry.source_max_delay_seconds == 3_600
    assert cfg.llm.thinking == "disabled"


def test_llm_and_vlm_limits_are_loaded_independently(tmp_path: Path) -> None:
    cfg = load_config(project_root=tmp_path, env_file=_write_env(tmp_path))

    assert (
        cfg.llm.max_concurrency,
        cfg.llm.requests_per_minute,
        cfg.llm.tokens_per_minute,
    ) == (7, 80, 600_000)
    assert (
        cfg.vlm.max_concurrency,
        cfg.vlm.requests_per_minute,
        cfg.vlm.tokens_per_minute,
    ) == (2, 15, 120_000)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"llm": {"timeout": 0}}, "LLM_TIMEOUT 必须大于 0 秒"),
        ({"llm": {"max_concurrency": 0}}, "LLM_MAX_CONCURRENCY"),
        ({"vlm": {"requests_per_minute": -1}}, "VLM_REQUESTS_PER_MINUTE"),
        ({"agent": {"max_tokens": 128_000}}, "AGENT_MAX_TOKENS 必须小于"),
        ({"agent": {"snip_ratio": 1}}, "AGENT_SNIP_RATIO 必须在"),
        ({"watch": {"fallback_interval": 0}}, "WATCH_SETTLE_WINDOW"),
        ({"retry": {"llm_max_attempts": 0}}, "RETRY_LLM_MAX_ATTEMPTS"),
        ({"retry": {"source_max_delay_seconds": 1}}, "RETRY_SOURCE_MAX_DELAY_SECONDS"),
    ],
)
def test_invalid_runtime_boundaries_fail_at_config_load(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        load_config(project_root=tmp_path, env_file=_write_env(tmp_path), overrides=overrides)

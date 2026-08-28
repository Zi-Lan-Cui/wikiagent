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
                "WIKI_WIKI_DIR=from-dotenv-wiki",
                "WIKI_WORKSPACE_DIR=from-dotenv-workspace",
                "COMPILE_CHUNK_SIZE=4096",
                "RETRY_LLM_MAX_ATTEMPTS=4",
            )
        ),
        encoding="utf-8",
    )
    return env


def test_paths_config_reads_the_selected_env_file(tmp_path: Path) -> None:
    cfg = load_config(project_root=tmp_path, env_file=_write_env(tmp_path))

    assert cfg.paths.resolved_wiki_dir() == Path("from-dotenv-wiki")
    assert cfg.paths.resolved_workspace_dir() == Path("from-dotenv-workspace")


def test_environment_and_overrides_take_precedence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WIKI_WIKI_DIR", "from-process-env")
    cfg = load_config(
        project_root=tmp_path,
        env_file=_write_env(tmp_path),
        overrides={"paths": {"wiki_dir": "from-override"}},
    )

    assert cfg.paths.resolved_wiki_dir() == Path("from-override")
    assert cfg.paths.resolved_workspace_dir() == Path("from-dotenv-workspace")


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


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"llm": {"timeout": 0}}, "LLM_TIMEOUT 必须大于 0 秒"),
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

"""评判模型的配置选取——所有 LLM 评判链路共用一个入口。"""

from __future__ import annotations

from wiki_agent.config import LLMConfig, RootConfig


def resolve_judge_llm(cfg: RootConfig) -> LLMConfig:
    """JUDGE_* 配齐即生效，未配置回退 LLM_*。

    同族模型共享训练盲区，自评一致率会虚高——正式校准应配
    不同模型族的 JUDGE_*；回退只保证单模型也能跑通流程。
    """
    return cfg.judge if cfg.judge.configured else cfg.llm

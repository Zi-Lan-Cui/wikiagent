# 评测语料与判词题集

本目录是 wiki-agent 的评测基准，用于校准 LLM Judge 并评估 compiler 质量。

## 语料构成

- **12 篇种子笔记**（[baselines/seeded/source/](baselines/seeded/source/)）：受控编写的技术笔记，覆盖故障保护（熔断/限流/超时降级）与可靠处理（消息投递/幂等设计/Saga 补偿）两个方向。
- **42 页编译产物**（[baselines/seeded/wiki/](baselines/seeded/wiki/)）：由 compiler 从空库顺序编译 12 篇笔记生成。
- **编译过程证据**（[baselines/seeded/run-evidence/](baselines/seeded/run-evidence/)）：每篇笔记编译时的阶段产物、index 快照与编译时可见树。

## 判词题集

60 条判词题（[verdicts/judgements-v2.json](verdicts/judgements-v2.json)），四个维度各 15 条：

| 维度 | 判定语义 |
|---|---|
| grounding | 产物断言是否受编译输入支持 |
| coverage | 编译输入的关键事实是否在产物中保留 |
| organization | 建页/拆分决策是否成立（C1 独立论述段 + C3 与既有页职责重叠） |
| uncertainty | 材料中的张力与缺口是否如实保留 |

每题声明编译时的真实可见文件（view），Judge 与 compiler 信息量对齐。校准结果与判定标准详见 [verdicts/README.md](verdicts/README.md)。

## 使用

校验语料与题集（离线，不调用 LLM）：

```bash
uv run python evals/run.py corpus
```

运行 Judge 校准（需要 LLM API）：

```bash
uv run python -m evals.commands.judge_pilot --repeats 3
```

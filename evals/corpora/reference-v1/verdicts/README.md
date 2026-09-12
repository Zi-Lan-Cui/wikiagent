# 判词题集与判定标准

本目录保存 Judge 校准用的判词题集，以及四个评测维度的判定标准。

## 题集构成

- **60 条**，四维度各 15 条：grounding / coverage / organization / uncertainty。
- 覆盖 12 篇种子笔记在 extract / execute / plan 三个编译阶段的产物。
- 每题声明编译时的真实可见文件（view.inputs/outputs），Judge 与 compiler 信息量对齐。
- organization 题含编译时可见树（compile-time-trees/）：判定对照的是该 source 编译那一刻真实可见的页面。
- gold 分布每维配平（约 50/50）。

## 判定标准

| 维度 | 判定语义 |
|---|---|
| grounding | 产出断言受编译输入支持。失真形态：模态反转、数字交换、过度概括、归因反转 |
| coverage | 编译输入的关键事实在产出中保留。保留形态：paraphrase、跨页聚合、蕴含展开 |
| organization | C1 独立论述段（输入中该主题有 ≥2 个独立事实的论述段）∧ ¬C3 职责重叠（编译时可见树中无既有页 goal 已覆盖该主题）→ 建页/拆分决策成立 |
| uncertainty | 材料的张力与缺口如实保留。失真形态：软化（未实现→未提及）、丢失、实化（草案→规则） |

## 校准结果（模型 mimo-v2.5，repeats=3 多数票）

| 维度 | 一致率 |
|---|---|
| grounding | 100.0% |
| coverage | 100.0% |
| uncertainty | 93.3% |
| organization | 71.4% |
| 总计 | 91.5% |

organization 维度包含语义判断（"独立事实"计数、"同义覆盖"宽窄），单遍判定建议 repeats≥3。

## 使用方法

```bash
# 语料校验（离线，不调用 LLM）
uv run python evals/run.py corpus

# Judge 校准（repeats=3 多数票）
uv run python -m evals.commands.judge_pilot --repeats 3
```

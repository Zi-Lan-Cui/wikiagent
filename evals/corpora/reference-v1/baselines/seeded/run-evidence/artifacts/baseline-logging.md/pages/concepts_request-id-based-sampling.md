# generated at: 2026-09-10T23:12:25.580353

---
type: concept
title: "基于request_id的采样"
summary: "在请求入口按request_id做采样决策并随链传递，保证单个请求日志完整性，ERROR全量保留的采样策略。"
tags: [logging, sampling, request-id]
goal: "系统覆盖基于request_id的采样策略的决策点、传递方式及其保证，作为结构化日志规范中采样机制的完整参考。"
gaps: ""
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-logging.md"]
related: ["[[entities/request-id]]", "[[entities/structured-logging-specification]]"]
---

# 基于request_id的采样

基于request_id的采样是一种在请求入口做出采样决策，并将该决策随调用链传递的策略。其核心目标是保证单个请求的所有相关日志的完整性，同时确保所有`ERROR`级别的日志都被保留。

## 决策点与传递

采样决策发生在请求的入口处。决策的核心标识符是[[entities/request-id|request_id]]。一旦在入口处根据`request_id`决定了是否对该请求进行采样，这个决策结果（采样或不采样）需要在整个请求的调用链中传递，以确保链上所有服务的日志行为一致。

## 核心保证

该策略提供两个关键保证：
1.  **单个请求的日志完整性**：对于同一个`request_id`，其在整个调用链中产生的所有日志要么全部被采样保留，要么全部不被保留，避免了因采样导致的单个请求日志链断裂。
2.  **ERROR日志全量保留**：无论采样决策如何，所有`ERROR`级别的日志都必须被完整记录，不得采样丢失。这确保了需要立即人工介入的故障信息不被遗漏。

此策略是[[entities/structured-logging-specification|结构化日志规范]]中采样策略原则的具体实现。
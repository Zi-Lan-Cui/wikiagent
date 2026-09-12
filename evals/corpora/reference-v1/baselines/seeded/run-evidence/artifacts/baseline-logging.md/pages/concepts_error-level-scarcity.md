# generated at: 2026-09-10T23:12:25.580254

---
type: concept
title: "ERROR级别稀缺化"
summary: "ERROR级别仅用于需要立即人工介入的故障，且错误只应在调用链最外层打一次。"
tags: [logging, error-handling, observability]
goal: "系统阐述ERROR级别日志的稀缺化原则、使用边界及其在调用链中的唯一性规则，作为结构化日志规范中级别策略的核心组成部分。"
gaps: ""
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-logging.md"]
related: ["[[entities/structured-logging-specification]]", "[[entities/request-id]]"]
---

# ERROR级别稀缺化

ERROR级别稀缺化是[[entities/structured-logging-specification|结构化日志规范]]中关于日志级别使用的核心策略之一。它明确规定了`ERROR`级别日志的使用边界和目的，旨在避免日志洪泛并确保告警的有效性。

## 使用边界

根据规范，`ERROR`级别日志**仅用于需要立即人工介入的故障**。这意味着它不应被用于记录可恢复的错误、业务逻辑异常或预期的失败情况。其核心目的是触发运维或开发人员的即时关注和行动。

## 调用链唯一性

该策略要求**错误只应在调用链最外层打一次**。这意味着在分布式或多层调用的服务架构中，一个故障不应在每一层都被记录为`ERROR`。这依赖于[[entities/request-id|request_id]]来串联整个调用链，确保从入口到出口的错误只被报告一次，通常是在最初捕获该故障并决定需要人工介入的边界层。
---
type: concept
title: "不可变schema"
summary: "业务字段命名一旦写入存储即成为不可变的schema，是数据治理的核心原则。"
tags: [数据治理, 日志规范, schema]
goal: "系统阐述不可变schema原则的约束、原因及其在结构化日志规范中的体现，作为业务字段命名稳定性的权威参考。"
gaps: ""
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-logging.md"]
related: ["[[entities/structured-logging-specification]]"]
---

# 不可变schema

不可变schema是一个数据治理原则，它规定业务字段的命名一旦被写入存储（例如日志系统），就成为了不可变的schema。这意味着后续不能随意更改字段名，否则会破坏已存储数据的结构和可读性。

## 约束与原因

该原则的核心约束是**字段命名的稳定性**。其根本原因在于，字段名是数据结构的元数据，一旦数据被持久化，字段名就与数据本身绑定。更改字段名会导致历史数据无法被正确解析，或需要复杂的迁移和兼容性处理，从而影响数据的可用性和系统的可维护性。

## 在结构化日志规范中的体现

在[[entities/structured-logging-specification|结构化日志规范]]中，不可变schema原则被明确应用。规范要求业务字段的命名必须保持稳定，因为日志数据一旦写入存储，其字段名就构成了日志的schema。这一要求确保了日志数据在长期存储和后续分析中的结构一致性。
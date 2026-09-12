---
type: entity
title: "批量回填"
summary: "数据库迁移中，适合数据量大、切换时间紧场景的数据回填技术手段。"
tags: [database-migration, backfill, data-migration]
goal: "系统说明批量回填作为数据回填方式之一的定义、适用场景及其在数据库迁移三阶段流程中的位置。"
gaps: ""
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-migration.md"]
related: ["[[entities/lazy-backfill]]"]
---

# 批量回填

批量回填是数据库迁移中，在三阶段迁移流程的迁移阶段进行数据回填的一种技术手段。它与[[entities/lazy-backfill|懒回填]]共同构成了数据回填的两种主要方式。

## 适用场景

根据文档描述，批量回填适用于以下场景：
- 数据量大
- 切换时间紧

## 在迁移流程中的位置

在三阶段迁移流程中，批量回填是迁移阶段执行数据回填的具体方法之一。迁移阶段的目标是实现新旧数据结构并存，而批量回填是达成此目标的一种主动、集中的数据处理方式。

## 与其他回填方式的关系

批量回填与[[entities/lazy-backfill|懒回填]]是数据回填的两种互补方式。选择哪种方式取决于具体场景的数据特征和时间约束。
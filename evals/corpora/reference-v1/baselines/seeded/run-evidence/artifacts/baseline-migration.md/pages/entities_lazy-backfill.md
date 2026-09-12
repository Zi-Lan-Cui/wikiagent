# generated at: 2026-09-10T23:09:34.178190

---
type: entity
title: "懒回填"
summary: "一种数据回填方式，适合访问分散的场景，在数据被访问时按需回填。"
tags: [database-migration, backfill]
goal: "阐明懒回填作为一种数据回填技术手段的定义、适用场景及其在数据库迁移流程中的角色。"
gaps: "未涉及懒回填的具体实现模式（如触发器、应用层逻辑）及其与批量回填在性能、复杂度上的详细对比。"
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-migration.md"]
related: ["[[entities/batch-backfill]]", "[[entities/three-phase-migration-process]]"]
---

# 懒回填

懒回填是数据库迁移过程中进行数据回填的一种方式，其核心特点是在数据被访问时按需进行回填。

## 适用场景

根据文档描述，懒回填适合**访问分散**的场景。这与[[entities/batch-backfill|批量回填]]形成对比，后者更适合数据量大、切换时间紧的场景。

## 在迁移流程中的角色

懒回填是[[entities/three-phase-migration-process|三阶段迁移流程]]中**迁移阶段**的一种具体数据回填手段。其目的是实现新旧数据结构的并存，为后续的收缩阶段做准备。

## 核心原则

文档强调，在数据回填（包括懒回填）完成后，遵循**回滚代码而非数据**的原则。已执行的数据回填不做反向回滚，因为数据回滚成本高且易导致数据漂移。
# generated at: 2026-09-10T23:02:30.655531

---
type: concept
title: "续期失败只收尾不新增规则"
summary: "分布式锁续期失败时，完成当前原子操作后停止，不发起新操作，标记任务为pending-verify。"
tags: [distributed-lock, failure-handling, atomic-operation]
goal: "系统阐述分布式锁续期失败后的统一处理规则，包括其具体内容、落地机制（pending-verify状态）及其工程前提（可checkpoint任务设计），作为续期失败场景的决策锚点。"
gaps: ""
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-lock.md"]
related: ["[[concepts/distributed-lock-lease-contract]]", "[[entities/redis-distributed-lock-implementation]]"]
---

# 续期失败只收尾不新增规则

分布式锁的续期失败处理是确保系统在异常情况下仍能维持数据一致性的关键环节。该规则定义了当锁持有者无法续期时应采取的统一行动，旨在最小化业务影响并为后续裁决提供依据。

## 规则内容

该规则的核心是：当锁的续期失败时，持有该锁的进程必须立即停止发起新的业务操作，但允许其完成当前正在执行的原子单元。这意味着，如果一个任务被设计为由多个原子步骤组成，续期失败后，进程将执行完当前步骤，然后停止，不再进入下一个步骤。

## 落地机制与状态标记

完成当前原子单元后，任务状态将被标记为 `pending-verify`。此状态表明该任务因锁续期失败而被中断，其执行结果处于不确定状态，需要由后续流程（如对账或人工审核）进行验证和裁决。这一机制将故障处理与业务恢复解耦。

## 工程前提

此规则的有效实施依赖于任务的可检查点（checkpoint）设计。任务需要被设计为由多个可独立完成的原子单元构成，并在每个单元完成后能够持久化其进度。这样，在续期失败时，系统才能安全地在当前原子单元边界处停止，并准确记录已完成的部分，从而降低取消成本和恢复难度。

## 相关概念

该规则是[[concepts/distributed-lock-lease-contract|分布式锁租约契约模型]]在续期失败场景下的具体应对策略。其实现依赖于[[entities/redis-distributed-lock-implementation|Redis分布式锁实现]]中的续期机制。
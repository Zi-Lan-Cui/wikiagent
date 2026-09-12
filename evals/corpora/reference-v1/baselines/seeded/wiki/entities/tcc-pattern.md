---
type: entity
title: "TCC模式"
summary: "一种分布式事务模式，要求服务实现Try-Confirm-Cancel三态接口，通过全程锁资源追求更强的中间一致性。"
tags: [分布式事务, 一致性, 架构模式]
goal: "作为与Saga模式对比的核心架构模式，系统阐述TCC模式的三态接口要求及其在一致性与实现成本间的权衡。"
gaps: "TCC模式的具体实现细节、适用场景边界以及与Saga模式的详细对比尚未在本文档中展开。"
created: 2026-09-11
updated: 2026-09-11
sources: ["note-saga-compensation.md"]
related: ["[[entities/saga-pattern]]"]
---
# TCC模式

TCC模式是一种分布式事务解决方案，它要求参与的服务实现三个接口：Try、Confirm和Cancel。该模式通过在事务执行全程锁定资源，以追求比最终一致性更强的中间一致性，但其实现代价也相对较高。

## 核心特征

TCC模式的核心在于其三态接口设计：

*   **Try**：执行业务检查，预留（锁定）必需的资源。
*   **Confirm**：确认执行业务，使用Try阶段预留的资源完成操作。
*   **Cancel**：取消执行业务，释放Try阶段预留的资源。

## 与Saga模式的对比

在分布式事务方案选型中，TCC模式常与[[entities/saga-pattern|Saga模式]]进行对比。根据相关讨论，选择Saga模式而非TCC模式的一个决策因素是实现成本。TCC模式要求服务实现复杂的三态接口并全程锁资源，这带来了更高的开发和运维成本，而Saga模式通过补偿操作追求最终一致性，允许中间状态可见，在实现上可能更为直接。
# generated at: 2026-09-10T23:02:30.655486

---
type: entity
title: "Fencing Token"
summary: "一种通过单调递增序号防止锁过期后旧持有者污染共享资源的系统性方案。"
tags: [distributed-systems, concurrency, fencing-token]
goal: "系统阐述 Fencing Token 的工作原理、实现前提及其作为分布式锁租约契约缺陷补偿方案的角色。"
gaps: "尚未覆盖 Fencing Token 的具体实现细节、性能影响以及在不同资源方（如数据库、存储服务）中的集成模式。"
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-lock.md"]
related: ["[[concepts/distributed-lock-lease-contract]]", "[[concepts/mutual-exclusion-is-not-correctness]]"]
---
# Fencing Token

Fencing Token（栅栏令牌）是一种用于解决分布式锁在租约过期后，旧持有者可能继续操作共享资源而导致数据不一致问题的系统性方案。

## 工作原理

Fencing Token 的核心机制是为每次成功获取锁的操作分配一个**单调递增**的序号。当持有锁的客户端需要向共享资源（如数据库）发起写入时，必须同时提交此序号。资源方（如数据库）在执行写入前，会检查该序号是否大于其已记录的最大序号。只有当新序号更大时，操作才被允许，否则拒绝。这确保了即使旧持有者因延迟（如GC停顿）在锁过期后发起操作，其携带的旧序号也会被资源方拒绝，从而防止了“污染”。

## 实现前提

Fencing Token 方案的实现有一个关键前提：**共享资源方必须参与此协议**。也就是说，资源方（例如数据库）需要具备检查并比较单调递增序号的能力。文档指出，当前在所述的订单服务系统中，此方案**尚未实现**。

## 作为租约契约缺陷的补偿

Fencing Token 被明确为解决 [[concepts/distributed-lock-lease-contract|分布式锁租约契约]] 模型固有缺陷的系统性补偿方案。分布式锁的租约（TTL）机制依赖于时间流逝来判定锁的失效，但客户端的进程暂停（如GC）可能导致其在租约过期后仍认为自己持有锁。Fencing Token 通过引入一个不依赖于客户端本地时间的、全局递增的序号，将锁的“有效性”校验从客户端转移到了资源方，从而解决了 [[concepts/mutual-exclusion-is-not-correctness|互斥不等于正确性]] 的问题。
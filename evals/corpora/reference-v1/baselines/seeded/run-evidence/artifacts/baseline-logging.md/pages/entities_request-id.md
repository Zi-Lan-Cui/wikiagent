# generated at: 2026-09-10T23:12:25.580163

---
type: entity
title: "request_id"
summary: "结构化日志中用于串联跨服务调用链的核心标识符。"
tags: [logging, distributed-systems, core-field]
goal: "作为结构化日志规范的核心字段实体，系统阐述 request_id 的定义、生成时机、传递方式及其在调用链串联中的核心作用。"
gaps: "文档明确指出 request_id 与分布式追踪系统的 trace_id 尚未打通。"
created: 2026-09-10
updated: 2026-09-10
sources: ["baseline-logging.md"]
related: ["[[entities/structured-logging-specification]]"]
---

# request_id

在结构化日志规范中，`request_id` 是每条日志必须包含的四个基础字段之一，其核心作用是**串联跨服务调用链**。

## 核心作用

`request_id` 是贯穿日志体系的关键标识，用于将属于同一请求的所有日志事件关联起来，无论这些日志产生于哪个服务。它是实现端到端请求追踪的基础。

## 生成与传递

- **生成时机**：`request_id` 在**请求入口**生成。
- **传递方式**：它必须**随调用链传递**，确保下游服务在处理请求时使用相同的 `request_id` 记录日志。

## 与采样策略的关系

在采样策略中，`request_id` 是采样决策的依据。规范要求在请求入口按 `request_id` 进行采样决策，并将此决策随调用链传递，以确保单个请求的所有日志要么全部保留，要么全部丢弃，从而保证日志的完整性。

## 与 trace_id 的关系

文档明确指出，当前的 `request_id` 与分布式追踪系统中的 `trace_id` **尚未打通**，这是一个已知的缺口。

## 所属规范

`request_id` 是 [[entities/structured-logging-specification|结构化日志规范]] 中定义的核心字段。
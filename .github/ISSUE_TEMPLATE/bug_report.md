---
name: Bug 报告
about: 报一个问题 —— 请按 CONTRIBUTING 第 5 节带上这几样，能省一整轮来回
title: "[Bug] "
labels: bug
---

## 现象

<!-- 发生了什么，期望是什么 -->

## 复现步骤

1.
2.

## 请附上这四样（CONTRIBUTING 第 5 节）

- [ ] `docker compose ps` 的输出
- [ ] Run 的 `id`，以及 `/runs/<id>/detail?project_id=<pid>` 页面里的复述
- [ ] 相关 `trace_id`（Web 与 Worker 日志里都有，一条 grep 能串起整条链路）
- [ ] 解析/脱敏类问题：一段**脱敏后**的样本

## 环境

- 部署方式：compose 全栈 / 只起 postgres+redis 后直跑
- Python 版本：
- 系统：

## 红线提醒

**不要贴真实密钥、真实敏感日志原文。** 本仓库的 `.env` 被 `.gitignore` 挡住，
issue 里也请不要绕过它。需要给 Key 相关证据时，只描述"配了没有 / 读到没有"。

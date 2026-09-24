---
name: db-migration
description: 加表/改表/改字段前做对象边界检查并规范执行 Alembic 迁移。触发词：加表、改表、加字段、迁移、migration、alembic、改表结构。
---

# db-migration —— 迁移规范与对象边界检查

## 何时用
用户说「加表」「改表」「加个字段」「跑迁移」「alembic」「改表结构」。

## 第 0 步：先做边界检查（不做完不许生成迁移）

对照《V1 编码计划》第 7 节冻结的 **9 张表**：
`User / Project / DataSource / Event / EventGroup / Incident / AgentRun / Insight / Evidence`

逐条自查并回答：
1. 这次改动动的是这 9 张表之一，还是新增第 10 张表？**新增表 = 改对象边界 → 必须先问用户，不许擅自加。**
2. 是否新增/删除字段却动了「真源」？（见下）
3. 是否碰 7 态状态机（`AgentRun.status`）或 L0–L3 分级？**改了必须先问。**

**真源铁律（违反即返工）：**
- `Event.group_id` 是分组唯一真源；**禁止**给 `EventGroup` 加 `event_ids` 反向数组。
- `Incident.group_ids` 是事故真源；**禁止**给 `Incident` 加冗余 `event_ids`。
- `EventGroup.event_count` 是分组时一次性写好的缓存计数，不随事件逐条更新。
- `AgentRun.status`（7 态）与 `current_phase`/`phase_history`（阶段进度）**正交**，不要混进一个字段。
- 所有业务表必须带 `project_id` 并建索引（Project 是上下文边界）。

## 步骤
1. 确认改动落在 9 表内；若越界，先停下问用户。
2. 生成迁移：`alembic revision --autogenerate -m "阶段 NN：<改了什么>"`
3. **人工审阅生成的脚本**（autogenerate 会漏索引/外键/可空性）：
   - 新列是否 `nullable` 正确？已有数据的表加非空列必须给 server_default。
   - `project_id` 索引、`Event.group_id` 外键是否都在。
   - 有没有误加 `event_ids` 之类反向数组。
4. 补 `downgrade()`：**必须可回滚**，不能留 `pass`。
5. 用真实 PG 实测：
   ```bash
   alembic upgrade head
   alembic downgrade -1
   alembic upgrade head
   ```
6. 查真实表结构核对：
   ```bash
   psql "$DATABASE_URL" -c "\d+ <表名>"
   ```
7. 全部通过后走 `stage-commit` 技能提交。

## 输出格式
| 检查项 | 结果 | 证据 |
|---|---|---|
| 未新增第 10 张表 | ✅/❌ | 改动清单 |
| 未加反向数组 / 未破坏真源 | ✅/❌ | diff |
| downgrade 可回滚 | ✅/❌ | 命令 + 输出 |
| project_id 索引齐全 | ✅/❌ | `\d+` 输出 |

## 失败处理
- autogenerate 报错或漏字段 → 不手改数据库，改模型后重新生成，保持「模型是唯一真源」。
- `downgrade` 跑不通 → 判**未完成**，不许只交 upgrade。
- 改动越出 9 表边界 → 停止执行，把「为什么要加第 10 张表」写清楚交给用户决策。

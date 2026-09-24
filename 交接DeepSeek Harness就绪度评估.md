# 交接 DeepSeek Harness（接 deepseek-v4.1-flash）开发就绪度评估

> 评估目的：在补齐「真实 JSONL 日志」缺口后，检验本计划**是否可以直接交给 DeepSeek Harness（DSH）接管开发**。
> 评估日期：2026-09-23。结论基于当前工作区文件 + 官方资料核实。

---

## 结论（先说答案）

**可以交给 DeepSeek Harness 开发，方向完全对路；但不是「零动作直接扔进去」。**

- 计划第 23 节「给执行 Agent 的最终指令」**本来就是写给 Codex / Claude / DeepSeek harness 等 harness 的**——文档从设计上就为 harness 执行做了准备。
- DeepSeek Harness V0.1.5 已**深度适配 V4.1 Flash**，而 V4.1 Flash 在训练阶段就针对 DSH 的标准 / PTC / 极简三种模式做了专项优化。**模型与 Harness 是最优组合**，不是将就。
- 剩下的是 **1 个硬前置 + 2 个决策点 + 少量环境注意事项**，都不是文档缺陷。

---

## 一、缺口补齐情况

| 缺口 | 状态 | 证据 |
|---|---|---|
| 真实 JSONL 日志 | ✅ 已补 | `logs/nginx_json_access.log`（1000 行，100% 合法 JSON）、`logs/nginxplus_status_json.log`（500 行，100% 合法 JSON），均来自真实 Nginx 日志 |
| 真实 TXT 系统日志 | ✅ 已有 | 5 份 loghub 真实日志，各 1999 行 |
| 模型映射 + Key 方案 | ✅ 已成文 | 《模型与Key配置方案.md》（已按 V4.1 Flash 更新） |
| 开工前 6 条文档不一致 | ✅ 已修订进计划 | 计划文档 9 处编辑，见《开工前6条文档不一致-修订说明.md》 |

> 小注：JSONL 样本是 HTTP 访问日志，语义上是 web access 而非系统监控；但阶段 04 的 JsonlParser 需要的正是「真实逐行 JSON」，这一点已满足。若要纯系统语义的 JSONL，属锦上添花，不是阻塞项。

---

## 二、DeepSeek Harness 是什么、怎么对接（已核实）

- **定位**：DeepSeek 官方开源、插件化 Agent 运行时（不是模型、不是本地模型运行器）。它负责「让 AI 读文件、跑命令、调工具、保留会话、改东西前请求许可」。
- **启动**（项目目录内，需 Node 24）：
  ```text
  npx @deepseek-ai/dsh web        # 本地 Web UI：http://127.0.0.1:3080
  ```
  只服务本机；`--host 0.0.0.0` 会被 CLI 拒绝（官方未设计为对外服务）。
- **配置模型**：Web UI → Settings → Models → DeepSeek 卡片填 API Key。Key **只写**，落盘在 `~/.dsh/.credentials.yaml`，设置文件只留引用（脱敏）。
- **默认模型目录**：`deepseek-flash`（V4.1 Flash，新建会话默认）、`deepseek-v4-pro`（旗舰，面向 Agent 任务优化）、`deepseek-v4-flash`（上一代条目，仅在已有配置明确需要时保留）。支持 `off / low / high / max` 推理档位；1M 上下文默认；文本 + 图片输入。
- **能力**：Web UI、无头 CLI、PTC 编排、Python SDK（`deepseek-harness-sdk`）。支持通过 Profile Bundles 挂 Claude Code / Codex 子代理；插件经 `dsh-plugin` 发现。
- **注意**：DSH 目前是**开发者预览（developer preview）**，可能含破坏性变更。用于「个人全栈项目开发」没问题，但要有版本升级的心理准备。

---

## 三、就绪度检查清单

### ✅ 已就绪（可交给 Harness）

1. **总纲 + 施工图齐备**：`Log_Intelligence_Agent_总工程文档.md`（跨版本总纲）+ `Log_Intelligence_Agent_V1_个人全栈工程编码计划.md`（1159 行，含阶段 01–14、验收、不做清单、假设证伪信号、给执行 Agent 的最终指令）。
2. **对象边界自洽**：9 表 / 7 态 / L0–L3 三处不打架，且已消除 6 条内部不一致中的表结构相关项。
3. **环境已实测跑通**：Docker Desktop 已启动；`logagent-postgres 15.19` / `logagent-redis 7.4.11` healthy；项目内 `.venv` Python 3.13.14；`pytest 10 passed`；`/healthz`、`/healthz/deps`、`/docs` 均 200；Celery worker 可跑。
4. **真实日志到位**：8 份真实样本（含 JSONL），阶段 04 有真实数据可调。
5. **成本参数到位**：模型单价配置项 + V4.1 Flash 现价 + 单次成本预估。
6. **Git 已初始化**：阶段 01 骨架已提交（`ccb72e9`），有回滚基线。

### ⛔ 唯一硬前置：DeepSeek API Key（只有你能给）

Harness 要跑 `deepseek-flash`，**必须**有一个有效 API Key 填入 Settings → Models。
- 步骤见《模型与Key配置方案.md》第 4 节。
- 在你给出 Key 之前，Harness 无法真正调用模型——但**可以先接管阶段 02（数据模型）等不依赖模型的部分**，Key 到位后再进入阶段 06。

### ⚠️ 2 个需要你点头的决策点

1. **L1/L2/L3 是否接受「同型号 + 推理档位」映射**：
   当前在售主力已合并为 `deepseek-flash`，方案建议三级都用它、靠推理档位区分。若你坚持三级三型号，需指定 L3 用什么（V4 Pro 仍在售但不推荐）。
2. **预算货币单位**：示例里的 `MONTHLY_BUDGET=10` / `DEFAULT_RUN_MAX_COST=0.30` 未声明货币。建议统一为**元**并按第 5 节重设。

---

## 四、环境注意事项（Windows / 本机）

这些是「会用起来才知道」的坑，交接前替你列出：

1. **Node 版本**：DSH 需要 **Node 24**；若版本不符先升级（`node -v` 确认）。
2. **Celery + Windows**：必须加 `-P solo`（已在长期记忆中）。
3. **代理端口**：已从 7890 换为 **7897**（7890 已不通）——Harness 若走代理访问 API 需用 7897。
4. **DSH 只服务本机**：不要指望用 `dsh web` 直接共享给他人；如需远程，另配端口转发并自行承担暴露风险。
5. **DSH 为预览版**：升级可能破坏现有 Profile / 插件组合，升级前先备份 `~/.dsh`。

---

## 五、建议的交接动作（可直接执行）

1. 你：注册 DeepSeek 平台 → 充值 → 创建 API Key（见方案第 4 节）。
2. 你：在项目目录 `npx @deepseek-ai/dsh web`，Settings → Models 填 Key，默认选 `deepseek-flash`。
3. Harness：读《总工程文档》与《V1编码计划》，从**阶段 02 数据模型与迁移**起，按「编码 → 测试 → Commit」推进（阶段 6 条不一致已修，表结构不再需要猜）。
4. 每阶段对照该阶段「验收」逐条过；阶段 04 用 `logs/` 里的真实数据测可解析率与脱敏误伤率。
5. 阶段 06 前确认 Key 与 L1/L2/L3 映射已定。

---

## 六、一页速览

| 维度 | 判定 |
|---|---|
| 计划作为 harness 施工图 | ✅ 够格（本就面向 harness 编写） |
| 模型 `deepseek-v4.1-flash` 可用性 | ✅ 真实在售，API 名 `deepseek-flash` |
| 模型 ↔ Harness 适配 | ✅ V0.1.5 深度适配，V4.1 Flash 专项训练 |
| 环境（Docker/PG/Redis/Python） | ✅ 已实测跑通 |
| 真实日志（含 JSONL） | ✅ 8 份 |
| 6 条文档不一致 | ✅ 已修订 |
| **API Key** | ⛔ **唯一硬前置，待你提供** |
| 映射策略 / 预算单位 | ⚠️ 2 个决策点待确认 |

**总判定：补齐 Key 与两个决策点后，本计划可直接交给 DeepSeek Harness 接 `deepseek-flash` 开发；在此之前，Harness 可从阶段 02 等非模型依赖阶段先行。**

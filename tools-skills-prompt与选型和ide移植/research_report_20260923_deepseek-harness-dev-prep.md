# DeepSeek Harness 开发就绪研究报告：专业提示词、Skills 与 Tools 选型

**日期**：2026-09-23 ｜ **适用项目**：Log Intelligence Agent V1（E:\日志agent开发）
**研究模式**：standard ｜ **结论面向**：即将在 DeepSeek Harness（DSH）中启动本项目开发的个人开发者

---

## 执行摘要（Executive Summary）

本报告回答一个问题：**当开发平台从通用 IDE 切换到 DeepSeek Harness（DSH）后，应如何配置提示词、技能（Skills）与工具（Tools / MCP），才能让 Agent 稳定地按既定计划推进？**

核心发现有三。第一，DSH 的提示词体系不是「一段超级 System Prompt」，而是**分层 + 按需披露**：项目级 `AGENTS.md` 充当 50–150 行的「路由文件」，详细规则外推到子文档与 Skill，命中触发词才加载——这直接决定了本项目应如何组织「计划/总纲」两份大文档 [5][6]。第二，DSH 的能力扩展有两条正交路径：**Skills**（过程知识，按需加载，不占常驻上下文）与 **MCP**（外部工具，经 `@deepseek-ai/dsh-mcp-client` 桥接为 `mcp__<server>__<tool>` 的普通工具）[1][9]；两者分工清晰，不应混用。第三，模型侧现役主力 `deepseek-flash`（V4.1 Flash）在训练阶段即针对 DSH 的标准 / PTC / 极简三种模式做了适配 [8]，因此「模型 + Harness」是最优组合而非将就。

据此，本报告给出一套可直接落地的交付：一份 `AGENTS.md`、7 条专业提示词、3 个可用 Skill 模板，以及一份按优先级排序的 MCP 选型清单。**就绪度判定：在补齐 API Key 与两个决策点后，本项目可直接交给 DSH 开发。**

---

## 引言（Introduction）

### 范围
本研究覆盖三块：(1) DSH 的提示词与指令装载机制；(2) Skill 的编写与加载规范；(3) 工具体系（内置工具 + MCP 选型）。不覆盖 DSH 内核插件开发、不覆盖模型微调。

### 方法
以官方文档（deepseekdocs.com）为主证源，辅以工程实践文章交叉核验；对命令 / 字段等易变信息标注「以本机版本为准」。共检索并交叉比对约 10 组来源，见文献表。

### 假设
- 读者为本项目的唯一开发者，使用 Windows + 已启动的 Docker 环境。
- DSH 为 developer preview，字段与命令可能随版本变化。
- 默认模型为 `deepseek-flash`。

---

## 主要分析（Main Analysis）

### 发现 1：DSH 的提示词是「分层指令 + 动态装配」，不是单段长提示词

DSH 在会话开始时按**从全局到局部**的层级自动装载指令文件：`$DSH_HOME/AGENTS.md`（全局）→ 项目根 `AGENTS.md` → 子目录 `AGENTS.md`，越具体优先级越高；并兼容 `CLAUDE.md`（同名内容去重）与 `AGENTS.local.md`（本机偏好，不进 git）[5]。系统提示本身由多个插件段（system prompt sections）在运行时装配，静态行为定义归 System Prompt、动态任务指令归 User Message [4]。

**对本项目的含义**：两份大文档（V1 编码计划 1159 行、总工程文档）**不应整篇塞入上下文**。正确做法是把「不可协商的硬约束、验证命令、真源约定」提炼进 ≤150 行的 `AGENTS.md`，把阶段细节留在文档里按需读取。多处实践指向同一结论：`AGENTS.md` 应控制在 50–200 行（约 100 行最佳），否则出现「指令稀释」[6]。

### 发现 2：Skills 与 MCP 是两条正交的扩展路径

**Skills** 承载「过程知识」：平时只加载 name + description，命中 `whenToUse` 才读全文，因此**不占常驻上下文**。放置于项目级 `.dsh/skills/<name>/SKILL.md` 或 `.agents/skills/` [4]。这使它特别适合「阶段验收流程」「成本核算流程」这类**步骤固定、但只在特定时刻用**的知识。

**MCP** 承载「外部能力」：一个插件实例 = 一个 MCP server，经 `@deepseek-ai/dsh-mcp-client` 接入后，外部工具注册进 `ctx.tools`，对模型就是普通工具，命名 `mcp__<serverName>__<rawName>`（如 `mcp__github__create_issue`）；并统一受 `tools/pre-execute` 的门控、超时与取消治理 [1]。支持 `stdio`（本地子进程）与 `streamable-http`（远程服务）两种传输 [1][9]。

**对本项目的含义**：把「怎么验收一个阶段」写成 Skill，把「查数据库 / 查 GitHub」接成 MCP，二者分工不重叠。

### 发现 3：模型侧已为 DSH 做专项适配，模式选择应跟随任务类型

`deepseek-flash`（即 V4.1 Flash）在训练阶段就针对 DSH 的**标准模式、PTC（程序化工具调用）模式、极简模式**做了适配 [8]。DSH 侧另有 `/plan` 计划模式（软引导、不换模型、退出需用户 Approve）[2]。

**对本项目的含义**：常规阶段实现在「标准」模式；批量跑 Golden Set / 批量解析 `logs/` / 批量成本核算适合 PTC；快速问答用极简；Domain 契约、归并算法等从零设计用「创造」模式并配合 `/plan`。

### 发现 4：安全边界由沙箱三档 + 审批构成

DSH 内置进程沙箱三档：只读（read-only）、工作区可写（workspace-write）、完全访问（danger-full-access）；需更高权限时 Web UI 弹审批，用户确认才放行 [10]。**对本项目的含义**：默认用 workspace-write；API Key、`.env`、`~/.dsh/.credentials.yaml` 永不入 git / 镜像 / 库。

### 发现 5：对本项目的工具选型（按优先级）

| 优先级 | 工具 | 用途 | 关键约束 |
|---|---|---|---|
| P0 内置 | 文件读/写/编辑、shell、web 搜索/抓取、LSP、subagent、todo | 日常开发 | 用「精确替换」改代码，diff 更小 |
| P0 MCP | 只读 PostgreSQL MCP | 查库验证迁移 | **只读**、只连 127.0.0.1:5432/logagent |
| P0 MCP | GitHub MCP | 翻依赖库源码 / issue | `command` 在 Windows 用 `npx.cmd` |
| P1 MCP | Playwright / 浏览器 MCP | 阶段 11 前端端到端验证 | 相对重量级，按需 |
| P1 MCP | 文档型 MCP（如 Context7） | 拉 FastAPI/SQLAlchemy/Celery 文档 | 减少幻觉 |
| P2 | Docker MCP | 查容器状态/日志 | 也可直接用 shell |

---

## 综合与洞察（Synthesis & Insights）

1. **「上下文预算」是第一约束，不是功能多寡。** DSH 的整套设计（分层 AGENTS.md、按需 Skill、按需 MCP）都指向同一目标：让模型每次只看到「此刻必需」的信息。本项目文档体量大，若不主动分层，阶段 06 之后极易触发上下文污染。
2. **规则要「可验收」，提示词要「可执行」。** 官方 `AGENTS.md` 的经验是写具体行为（如「非平凡改动必须补 Agent Note」）而非「注意最佳实践」[4]。本项目据此把硬约束写成 10 条可判定的红线。
3. **Skills 是把「运行时知识」沉淀下来的正确容器。** 验收流程、成本核算、解析评估都是「步骤固定、触发明确」的知识——这正是 Skill 的定义域。

---

## 局限与提示（Limitations & Caveats）

- DSH 为 **developer preview**，命令、配置字段（如 profile 入口在 `cordis.yml`/`config.yaml` 间的差异）可能随版本变化 [1][9]；落地前请以 `dsh --version` 与官方文档复核。
- 部分实践来源（dev.to / CSDN / 第三方站点）为社区文章，非官方原文；本报告对「机制类」结论优先采信官方文档，对「经验类」结论标注为实践建议。
- 模型定价与型号为 2026-09-23 快照，随时可能变动。
- 本报告未在真实 DSH 实例上端到端跑通（无 API Key），命令示例需你本机验证。

---

## 建议（Recommendations）

1. **立即**：把交付的 `AGENTS.md` 放到项目根；把 `dsh-skills/` 下 3 个 Skill 拷进 `.dsh/skills/`。
2. **立即**：注册 DeepSeek 平台、充值、创建 API Key，填入 DSH Settings → Models，默认模型 `deepseek-flash`。
3. **第一周**：接入只读 Postgres MCP + GitHub MCP；用标准模式从阶段 02 起按「计划 → 编码 → 验收 → Commit」推进。
4. **每阶段**：用「阶段启动」提示词进 `/plan` 讨论，用「阶段验收」提示词独立复核，用「红线自查」提示词兜底。
5. **跨会话**：用「暂停/交接」提示词生成 `docs/handoff.md`，避免重复踩坑。

---

## 参考文献（Bibliography）

[1] DeepSeek Docs — *MCP Integration*. https://deepseekdocs.com/en/docs/features/mcp
[2] DeepSeek Docs — *Agent Presets and Personas*（含 plan-mode 说明）. https://deepseekdocs.com/en/docs/features/persona
[3] DeepSeek Docs — *Tool Execution*（tools/pre-execute 门控）. https://deepseekdocs.com/en/docs/learn/core/tools-execution
[4] DeepSeek Harness Series (06): System Prompt Assembly — Engineering Dynamic Prompts. dev.to/wonderlab
[5] DeepSeek Harness AGENTS.md：项目指令文件怎么写. ai-indeed.com/encyclopedia/29668.html
[6] A Guide to Harness Engineering: Building Reliable AI Agent Workflows（AGENTS.md 50–200 行）. strangelove-ai.com
[7] DeepSeek API Docs — 模型与定价（deepseek-flash 峰谷价）. https://api-docs.deepseek.com/zh-cn/quick_start/pricing
[8] DeepSeek 官方公告 — DeepSeek-V4.1-Flash / Harness 适配. https://www.deepseek.com/zh/news/
[9] DeepSeek Harness MCP 集成：stdio 与 HTTP 配置. ai-indeed.com/encyclopedia/29674.html
[10] DeepSeek Harness 能干嘛？一切皆插件. ai-indeed.com/encyclopedia/29689.html

---

## 方法附录（Methodology Appendix）

- **检索**：多组并行关键词（DSH 插件/skills/MCP、AGENTS.md 最佳实践、PTC 模式、模型定价）。
- **交叉核验**：机制类结论以 official deepseekdocs.com 为准；命令/字段类结论与社区实践文章比对。
- **证据留存**：关键结论均可在上述文献中定位；未落地的命令标注「需本机验证」。
- **未覆盖**：DSH 内核插件开发、多 agent 编排的深度用法、模型微调。

---

### 附：随报告交付的即用文件（同一交付目录）
- `AGENTS.md` — 项目级路由文件（直接放项目根）
- `DSH开发提示词与Skills-Tools清单.md` — 提示词库 + 工具/技能清单
- `dsh-skills/stage-verify/SKILL.md`、`dsh-skills/log-parser-eval/SKILL.md`、`dsh-skills/cost-estimate/SKILL.md`

*风险与免责声明：本报告中的价格、型号、产品能力与配置信息来自公开渠道，可能随时变动；内容仅供一般信息参考，不构成投资建议或产品推荐。请以 DeepSeek 官方页面与本机实测为准。*

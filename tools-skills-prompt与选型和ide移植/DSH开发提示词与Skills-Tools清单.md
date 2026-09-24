# DeepSeek Harness 开发提示词与 Skills / Tools 清单（Log Intelligence Agent V1）

> 适用：用 DSH（DeepSeek Harness）驱动开发本项目。
> 目标：让 Agent 每次会话都「知道自己在哪、该守什么、下一步做什么」，把计划变成可执行节奏。
> 说明：涉及 DSH 的命令 / 字段以你本机 `dsh --version` 与官方文档为准（DSH 为 developer preview，字段可能变）。
>
> **本文是方案存档**：里面的提示词与清单记录"当初怎么定的"。生效的 skills 只有一份 ——
> 在 `.dsh/skills/`（其中 `stage-verify` 已按 2026-09-24 的**真机口径**重写，
> 与本文附录里的原始快照不再一致）。同目录下的 `dsh-skills/` 是**来源快照**，不进版本库。

---

## 0. TL;DR —— 使用顺序

1. 装好并启动 DSH（见 §1），选好工作区 = `E:\日志agent开发`。
2. 把已生成的 `AGENTS.md` 放到项目根，DSH 每次会话自动加载。
3. 新建会话选 **标准** 模式；复杂设计讨论用 **/plan**；批量小改动用 **PTC** 模式。
4. 复制 §4 对应提示词开工。每阶段走「计划 → 执行 → 验收 → Commit」四步。
5. 先装 §6 的只读 Postgres + GitHub 两个 MCP，其余按需。
6. 把 §5 的 skills 放进 `.dsh/skills/`，按需加载，不占常驻上下文。

---

## 1. 先决条件（DSH 起步）

| 项 | 要求 | 说明 |
|---|---|---|
| Node | **24** | `node -v` 确认；不够先升 |
| 启动 Web | `npx @deepseek-ai/dsh web` | 默认 `http://127.0.0.1:3080`，仅本机 |
| 无头模式 | `dsh --profile headless "把测试跑一遍并修复失败"` | 脚本化 / 批处理，输出即结果 |
| 配 Key | Settings → Models → DeepSeek 填 `sk-...` | 只写不读，存 `$DSH_HOME/.credentials.yaml` |
| 选工作区 | 指向 `E:\日志agent开发` | Agent 的读写根 |
| 默认模型 | `deepseek-flash`（=V4.1 Flash） | 推理档 `off/low/high/max` |

> 代理端口用 **7897**（7890 已不通）；Celery 在 Windows 必加 `-P solo`。

---

## 2. 四种 Agent 模式怎么选

| 模式 | 适合 | 本项目用法 |
|---|---|---|
| **标准** | 常规功能开发（默认） | 阶段 02–14 逐阶段实现 |
| **PTC**（程序化工具调用） | 在一段脚本里编排多步工具、循环处理 | 批量跑 Golden Set、批量解析 `logs/`、批量成本核算 |
| **极简** | 快速问答、小改、看代码 | 「这函数干嘛的」「改这一行」 |
| **创造** | 从零设计、方案探索 | 阶段 03 Domain 契约、阶段 09 归并算法设计 |

**/plan 计划模式**：`/plan` 进入（软引导，不换模型），`/plan off` 退出；退出需你点 **Approve**。
设计阶段、改表结构前，**一律先进 /plan 讨论清楚再执行**。

---

## 3. 项目级 AGENTS.md（核心，已单独交付）

见同目录 `AGENTS.md`。要点：
- DSH 分层加载：`$DSH_HOME/AGENTS.md`（全局）→ 项目根 → 子目录；越具体优先级越高。
- 兼容 `CLAUDE.md`（同名内容去重）；本机偏好写 `AGENTS.local.md`（不进 git）。
- **保持 ≤150 行**，当「路由文件」用，细节指向两份大文档，避免上下文膨胀（长文件会「指令稀释」）。

---

## 4. 专业提示词库（直接复制）

### 4.1 新会话冷启动 / 接续（每次开新会话先发）

```text
你是本项目的执行 Agent。请先做三件事，不要写代码：
1) 读项目根 AGENTS.md，复述你理解的 10 条硬约束（一句话每条）；
2) 读《Log_Intelligence_Agent_V1_个人全栈工程编码计划.md》的 §5 编码顺序总表 + 你当前要做的那个阶段；
3) 读《总工程文档》的文档地图，确认跨版本边界。

然后回答：
- 当前项目处在哪个阶段？依据是什么（用 git log --oneline 和文件状态判断）；
- 下一个该做的阶段及其「验收」清单逐条列出；
- 有没有需要我先决策/提供的阻塞项（如 API Key、模型映射）。

在我确认前不要开始编码。
```

### 4.2 阶段启动（计划模式，先想清楚）

```text
进入计划模式。我们要做【阶段 NN：<名称>】。
请：
1) 先复述该阶段的目标、涉及的表/模块、以及它的「验收」清单；
2) 给出实现方案：文件清单、每个文件职责、关键函数签名、依赖与迁移；
3) 指出与现有代码/文档可能的冲突点，以及你打算怎么处理（先记录后提，不擅自改对象边界）；
4) 给出测试计划（单测 + 需要真实数据的部分）；
5) 列出本阶段**不会**做的事（对齐 §20 不做清单）。
只出计划，不写代码；等我 Approve 后再进入执行。
```

### 4.3 阶段执行（编码 → 测试 → Commit）

```text
按刚才 Approve 的计划执行【阶段 NN】。要求：
- 每写完一个模块，同步补该模块单元测试；
- 遵守 AGENTS.md 硬约束（尤其：确定性计算不调 LLM、型号只在配置层、无证据不标 fact、失败不静默）；
- 全部写完后运行 `pytest -q`，把真实输出贴出来；失败先分析原因再修，不要跳过测试；
- 对照本阶段「验收」逐条自评，给出「通过/未通过 + 证据」；
- 全部通过后 `git add -A && git commit -m "阶段 NN：<做了什么>"`；
- 最后给我：改动文件清单、测试结果、验收逐条结论、下一阶段建议。
```

### 4.4 阶段验收（单独复核，防止自评放水）

```text
你现在是验收员，不是开发者。请对【阶段 NN】做独立验收：
- 逐条对照计划里该阶段的「验收」，每条给：命令/证据 + 实际输出 + 通过与否；
- 特别检查：有没有为了「过」而放宽断言、mock 掉真实依赖、或绕过真实日志；
- 用 logs/ 下的真实数据实测需要真实数据的项；
- 输出一张表：验收项 | 证据 | 结论 | 未通过原因。
任何一条未通过，明确说「阶段 NN 未达标」，并给出最小修复项。
```

### 4.5 Bug 修复

```text
出现 Bug：<现象 + 报错原文 + 复现步骤>。
请按「先解释后执行」：
1) 先给出最可能的 1–3 个根因假设，以及各自如何证伪；
2) 用最小复现定位（不要一上来大改）；
3) 说明修复方案与影响面（会不会动对象边界 / 迁移）；
4) 修复后补一个能复现该 Bug 的回归测试；
5) 说明是否触及 AGENTS.md 任一硬约束。
不要静默吞异常，不要用 try/except 掩盖。
```

### 4.6 红线自查（提交前自检）

```text
对本阶段改动做一次红线自查，逐条回答是/否 + 证据：
1 确定性计算有没有偷偷调 LLM？2 有没有把具体型号写进业务代码？
3 有没有无 evidence 的 fact？越界 event_id 会被拦截吗？
4 失败会不会静默？5 敏感原文有没有可能落盘？
6 有没有改动 9 表/7 态边界（若改了，是否已获批）？
7 长任务是否都异步？8 Domain 是否走 Registry？
9 是否只留了最小接口缝、没有过度设计？10 是否已 commit 且消息带阶段号？
```

### 4.7 暂停 / 交接（跨会话续接）

```text
请生成本轮交接摘要（写入 docs/handoff.md）：
- 当前阶段与完成度；已完成文件/提交哈希；
- 未决问题与阻塞项（含需要我决策的）；
- 下一步的第一动作；
- 本次会话踩过的坑与结论（避免下次重复）。
摘要要短、具体、可执行。
```

---

## 5. 好用的 Skills（DSH SKILL.md，放进 `.dsh/skills/`）

DSH 技能**按需加载**：平时只加载 name+description，命中 `whenToUse` 才读全文，**不占常驻上下文**。
目录：项目级 `.dsh/skills/<name>/SKILL.md` 或 `.agents/skills/`。

建议为本项目写这 6 个（每个都短）：

| Skill | 触发时机 | 作用 |
|---|---|---|
| `stage-verify` | 说「验收阶段 NN」 | 对照计划该阶段验收逐条跑并出表 |
| `log-parser-eval` | 说「测解析率/误伤率」 | 用 `logs/` 真实数据算可解析率与脱敏误伤率 |
| `db-migration` | 说「加表/改表/迁移」 | Alembic 迁移规范 + 对象边界检查 |
| `cost-estimate` | 说「估成本」 | 按 deepseek-flash 峰谷价核算单次/累计成本 |
| `golden-set-run` | 说「跑 Golden Set」 | 跑 5 个场景并记录成本与命中 |
| `stage-commit` | 说「提交阶段」 | 执行「编码→测试→commit」并生成消息 |

已生成 3 个可直接用的示例：`dsh-skills/stage-verify/SKILL.md`、`log-parser-eval/SKILL.md`、`cost-estimate/SKILL.md`。
拷进 `E:\日志agent开发\.dsh\skills\` 即生效。

> 编写要点：frontmatter 至少给 `name` 与 `description`（含触发词）；正文用「何时用 / 步骤 / 输出格式 / 失败处理」四段；给确定性命令，别写空泛建议。

---

## 6. 好用的 Tools

### 6.1 DSH 内置工具（默认就有，直接用）

- **文件类**：读 / 写 / 编辑（精确替换）/ 全局搜索 / 列目录 —— 让 Agent「先读后改」，不要凭文件名猜内容。
- **命令类**：执行 shell（PowerShell/bash）—— 跑 `docker compose`、`pytest`、`celery`、`alembic`。
- **检索类**：网页搜索 / 网页抓取 —— 查库文档、DeepSeek API、报错含义。
- **代码智能**：LSP（跳转/诊断）—— 在大仓里定位符号。
- **协作类**：子代理（subagent，并行分工）、待办清单（todo）、向你提问（ask_user）、后台任务（jobs）。
- **编排类**：计划模式（/plan）、目标（goal）、工作流（workflow）、长任务循环。
- **扩展类**：技能（skill，见 §5）、MCP（见 §6.2）。

> 提示：让 Agent 用「精确替换工具」改代码，比整文件重写更安全、diff 更小。

### 6.2 推荐接入的 MCP Server（按优先级）

**第一优先（马上用得上）：**

1. **只读 PostgreSQL MCP** —— 让 Agent 直接查库验证迁移结果、看表结构。**只读**、只连 `127.0.0.1:5432` 的 `logagent` 库。
2. **GitHub MCP**（`@modelcontextprotocol/server-github`）—— 查 issue/PR、翻依赖库源码。

**第二优先（阶段 11 / 13 时加）：**

3. **Playwright / 浏览器 MCP** —— 阶段 11 前端报告页端到端验证。
4. **Context7 / 文档 MCP** —— 拉 FastAPI / SQLAlchemy / Celery 最新文档，减少幻觉。
5. **Docker MCP** —— 查容器状态、日志（也可直接用 shell 代替）。

**配置示例（stdio，加到你的 profile / cordis.yml）：**

```yaml
- id: mcp-github
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: github
    transport: stdio
    command: npx            # Windows 若报找不到命令，改成 npx.cmd
    args: ['-y', '@modelcontextprotocol/server-github']
    env:
      GITHUB_TOKEN: !!js process.env.GITHUB_TOKEN

- id: mcp-pg
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: postgres
    transport: stdio
    command: npx
    args: ['-y', '@modelcontextprotocol/server-postgres',
           'postgresql://logagent:logagent@127.0.0.1:5432/logagent']
```

- 接入后工具名形如 `mcp__github__create_issue`、`mcp__postgres__query`，对 Agent 就是普通工具。
- 校验：`dsh web --dump-config | grep -A3 mcp`。
- **只读 Postgres**：用只读账号或只读 server，避免 Agent 误改库。

### 6.3 本项目专用「工具化」建议（降本增效）

- 写 `scripts/eval_parse.py`：吃 `logs/*`、输出可解析率/坏行/误伤计数表 → 让 Agent 用**运行脚本**代替反复读原始日志。
- 写 `scripts/cost_calc.py`：读 token 统计 + 单价，输出成本表 → 阶段 07 直接调用。
- 把这两个脚本登记为**技能**，触发关键词即调用，稳定又省上下文。

---

## 7. 安全与权限（DSH 内置沙箱三档）

| 模式 | 能力 | 建议 |
|---|---|---|
| 只读 read-only | 只能看 | 看陌生代码、评估阶段时用 |
| 工作区可写 workspace-write | 只能改工作区内 | **本项目默认** |
| 完全访问 danger-full-access | 不限制 | 仅确有必要时临时开 |

- 需更高权限时 Web UI 会弹**审批**，你确认才放行 —— 不要为省事常开完全访问。
- Key、`.env`、`~/.dsh/.credentials.yaml` 不进库、不进镜像、不进 git。

---

## 8. 排错清单（Windows / 本机）

| 症状 | 处理 |
|---|---|
| `npx` 子进程报「找不到命令」 | 配置里 `command` 改用 `npx.cmd` |
| 连不上模型 API | 代理用 7897；确认 Key 与 `BASE_URL` |
| Celery 卡住 | 加 `-P solo` |
| MCP 握手成功但工具拉不出来 | stdio 不要加 headers/timeout；HTTP 确 URL 路由 |
| DSH 升级后配置失效 | 预览版可能有破坏性变更，升级前备份 `~/.dsh` |
| 上下文被占满 | 缩短 AGENTS.md，细节挪进 skills/子文档，用 PTC 跑批处理 |

---

## 9. 一页速查

```text
启动:      npx @deepseek-ai/dsh web          → 127.0.0.1:3080
无头:      dsh --profile headless "..."
模型:      deepseek-flash  (off/low/high/max)
项目指令:  项目根 AGENTS.md（≤150 行，路由文件）
计划:      /plan  → 讨论 → Approve → 执行
节奏:      计划 → 编码(含单测) → 验收逐条 → commit(阶段号)
技能:      .dsh/skills/<name>/SKILL.md（按需加载）
MCP:       只读 Postgres + GitHub（先装这两个）
沙箱:      workspace-write（默认）
真数据:    logs/（8 份，含 JSONL）
```

---

## 附：来源
- DSH MCP / Persona / 预设 / 工具执行：deepseekdocs.com 官方文档
- DSH AGENTS.md 机制与「路由文件 ≤150 行」：官方实践文章
- 模型与定价：《模型与Key配置方案.md》（DeepSeek 官方）

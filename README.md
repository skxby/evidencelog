# Log Intelligence Agent

把日志变成**带证据、可追溯、成本可控**的分析报告。

V1 是个人全栈、单领域、**只读**的日志分析 Runtime：上传日志 → 脱敏 → 解析 →
分组/归并 → 规则 + 模型分层分析 → 带证据的 Insight 报告。
核心不是「更聪明」，而是**受约束、可恢复、可观测、成本可控**。

> 本 README 只写**怎么把它跑起来、怎么用**。架构与阶段设计见
> 《Log_Intelligence_Agent_V1_个人全栈工程编码计划》。

### 文档地图

| 文档 | 什么时候读 |
|---|---|
| `README.md`（本文件） | 想把它跑起来、想用页面走一遍最小流程 |
| `Log_Intelligence_Agent_总工程文档.md` | **新会话 / 新窗口的第一站**：项目最高层总纲，读完即可接续工作 |
| `Log_Intelligence_Agent_V1_个人全栈工程编码计划.md（最新版）.md` | 想知道"为什么这么设计"、要核对某个阶段的验收标准 |
| `logs/README.md` | 要改 Parser / 脱敏规则，需要真实日志语料与量化基线 |
| `.dsh/skills/*/SKILL.md` | 要按既有节奏做加表、验收、提交、成本核算等固定动作 |

> 代码与测试里大量出现「计划第 N 行」这类引用（`tests/datasets/*/expect.json`
> 的 `sources` 字段尤其如此）。**编码计划就在本仓库里**，所以那些引用在
> clone 之后是可核对的 —— 引用一份不在仓库里的文档等于没引用。

---

## 1. 前置要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Docker Desktop | 已安装并**保持运行** | 全栈都在容器里跑 |
| Docker Compose | v2 及以上（`docker compose`，不是 `docker-compose`） | |
| DeepSeek API Key | 一把可用的 `sk-...` | 没有也能起，但模型分析会降级为纯规则报告 |

**只用 Docker 跑的话，本机不需要装 Python。**
（想直跑测试才需要 Python 3.13；不要用 3.14，Celery 官方支持列表到 3.13 为止。）

---

## 2. 一键起全栈

```bash
git clone <本仓库>
cd <仓库目录>

# ① 准备配置
cp .env.example .env
```

打开 `.env`，**至少填这两项**：

```dotenv
MODEL_PROVIDER_API_KEY=sk-你的真实Key
SECRET_KEY=换成一串你自己的随机值
```

其余项都有可用默认值（模型映射、单价、预算、时区）。
**`.env` 已被 `.gitignore` 与 `.dockerignore` 双双挡住 —— 不会进 Git，也不会进镜像。**

```bash
# ② 起全栈（首次会构建镜像，约 1-3 分钟）
docker compose up -d

# ③ 确认都健康
docker compose ps
```

期望看到：

```
logagent-postgres   Up (healthy)
logagent-redis      Up (healthy)
logagent-migrate    Exited (0)      ← 迁移跑完即退出，这是正常的
logagent-web        Up (healthy)
logagent-worker     Up
logagent-beat       Up
```

浏览器打开 **http://127.0.0.1:8000** 。

> 端口只绑 `127.0.0.1`，不暴露到局域网 —— 与「只读 + 本地」的定位一致。
> 需要远程访问请自行配端口转发，并自行承担暴露风险。

---

## 3. 最小使用流程（全程不碰命令行）

1. **登录** —— 首次使用点「首次使用？注册这个邮箱」，用同一个邮箱 + 口令即可。
2. **建项目** —— 项目是数据边界，所有日志与结论都归属某个项目。
3. **上传日志** —— 选 `.txt`（syslog / log4j / Apache 风格）或 `.jsonl`（逐行 JSON）。
   上传后页面会显示**解析统计**（总行 / 成功 / 坏行 / 跳过 / 合并）与**脱敏计数**。
   坏行会列出样本 —— 不会被静默丢弃。
4. **发起分析** —— 选数据源，可留空时间范围。API **立即返回** `run_id + queued`。
5. **看状态** —— 状态页每 3 秒刷新，显示阶段进度、token、成本、心跳。
6. **看报告** —— 结论按四层语义着色：
   - `✓ 确认`（fact）：**证据可展开逐条核对**，点开能看到关联事件 id
   - `→ 推测`（inference）：附推理过程
   - `? 可能`（possibility）：附局限说明
   - `− 未知`（unknown）：附信息缺口

   命中异常时会附上对应的**处置步骤（runbook）**，V1 只读展示、不自动执行。
7. **审核知识** —— 报告页下方「待确认知识」列出分析中发现的候选；
   可以确认 / 编辑 / 丢弃，或点「这其实不是问题」记为误报。
   确认后写入数据卷，下次分析自动加载。

**部分成功与失败会显著标注，并都能一键重新分析**：
`partial_success` 顶部显示「⚠ 结果不完整」及中断原因；`failed` / `timeout`
显示失败阶段与原因。两种状态下都有「重新分析（新建 Run）」按钮 ——
它按**原 Run 存下来的输入快照**新建一个 Run（不用你回项目页重填时间窗，
填得不一样就不是同一次分析了），新 Run 用 `parent_run_id` 指回原 Run。

> `completed` 的 Run **不提供**重新分析：同样的输入再跑一遍只是重复计费。
> 要换口径请回项目页调整时间窗或过滤条件后重新发起。

---

## 4. 环境变量说明

完整清单见 `.env.example`。关键项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `...@127.0.0.1:5432/logagent` | 宿主机直跑用。**compose 里会被覆盖成 `@postgres:5432`** |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 同上，compose 里覆盖成 `@redis:6379` |
| `POSTGRES_PASSWORD` | `logagent_dev_pw` | compose 与 `DATABASE_URL` 共用；换它要一起换 |
| `SECRET_KEY` | `change-me` | JWT 签名密钥，**必须换**；空值会让鉴权失效 |
| `MODEL_PROVIDER_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `MODEL_PROVIDER_API_KEY` | 空 | 供应商密钥。**留空则模型分析降级为纯规则报告** |
| `MODEL_L1/L2/L3` | `deepseek-flash` | 等级 → 型号映射。**型号只出现在这里**，业务代码只认 L1–L3 |
| `MODEL_L*_REASONING` | `off` / `low` / `high` | 合法值只有 `off`/`low`/`high`/`max`；`medium` 是非法值 |
| `MODEL_L*_PRICE_*_PER_1M` | `1` / `4` | 单价，**元 / 每百万 token**；用于成本核算 |
| `MODEL_CACHE_HIT_INPUT_PRICE_PER_1M` | `0.02` | 缓存命中的输入单价（比输入价低一个数量级）。按命中 token 数单独计费，不配则按正常输入价 |
| `MONTHLY_BUDGET` / `DEFAULT_RUN_MAX_COST` | `10` / `0.30` | 预算上限，货币单位统一为**人民币元** |
| `DATA_DIR` | `./data` | 运行时数据（知识库 confirmed/staging）；compose 里是 `/app/data`（卷） |
| `DEFAULT_TIMEZONE` | `Asia/Shanghai` | 日志时间戳缺时区时按它解析，再转 UTC 存库 |

### 密钥放哪

优先级：**`.env` 的 `MODEL_PROVIDER_API_KEY` > 环境变量 > `$DSH_HOME/.credentials.yaml`**。
第三条是为了复用已在 DeepSeek Harness 里配好的 Key，避免同一把 Key 存两份。
都找不到时会**启动即报错并说明去哪里配**，不会等到第一次调用才含糊地失败。

---

## 5. 常用操作

```bash
# 看日志（JSON 行，带 trace_id —— 排障时按 trace_id 一 grep 就能串起整条链路）
docker compose logs -f web
docker compose logs -f worker

# 只重启应用（改代码后）
docker compose up -d --build web worker

# 手动跑迁移
docker compose run --rm migrate

# 进数据库看一眼
docker compose exec postgres psql -U logagent -d logagent -c '\dt'

# 停止（保留数据）／清空数据
docker compose down
docker compose down -v
```

**接口文档**：http://127.0.0.1:8000/docs

---

## 6. 不用 Docker 直跑（开发 / 跑测试）

```bash
# 只起基础设施
docker compose up -d postgres redis

python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt    # macOS / Linux

alembic upgrade head
pytest -q                                          # 单测 + 集成测试
uvicorn app.main:app --reload                      # http://127.0.0.1:8000
celery -A app.celery_app worker -P solo -l info    # Windows 必须 -P solo
```

### 跑 Golden Set

五个固定场景（正常 / CPU 异常 / 内存增长 / 崩溃 / 坏格式），逐条对照
`tests/datasets/<场景>/expect.json` 里的期望：

```bash
pytest tests/golden -q
```

期望值是可编辑的 JSON，每条都带 `sources` 字段注明依据（对应编码计划哪一行）。
默认**不调用模型**，所以跑一次是零成本、结果可复现。

### 真实模型连通性验证（会产生费用）

默认跳过。要跑需显式开启：

```bash
RUN_LIVE_MODEL_TESTS=1 pytest tests/integration/test_gateway_live.py -v -s
```

### 评测与验收脚本（`.dsh/`）

四个脚本都是**确定性、可复现**的；前两个不调模型，因此零成本。

| 脚本 | 作用 | 命令 |
|---|---|---|
| `.dsh/parser_eval.py` | 用 `logs/` 下 8 份真实日志量化可解析率与脱敏误伤率 | `python .dsh/parser_eval.py` |
| `.dsh/golden_eval.py` | 跑 Golden Set 五个场景并打印逐条断言与成本 | `python .dsh/golden_eval.py`（加 `RUN_GOLDEN_LIVE=1` 走真模型，**付费**） |
| `.dsh/stage_verify.py` | 按「阶段 → 承载验收的测试文件」出可核对的计数表 | `python .dsh/stage_verify.py` |
| `.dsh/final_verify.cjs` | 对着**真跑起来的全栈**做 16 项端到端检查（真实 HTTP） | `node .dsh/final_verify.cjs` |
| `.dsh/budget_gate_check.py` | 对着真栈核验成本闸门（预算不足拒绝创建 / 金额精度 / 未设预算放行） | `python .dsh/budget_gate_check.py` |
| `tests/unit/test_web_ui_contract.py` | 核对「页面 JS 调用的接口」与真实路由逐条对齐（阶段 11「全程不碰命令行」的机器化检查） | `pytest tests/unit/test_web_ui_contract.py -q` |

最近一次实测（2026-09-24）：

```text
解析率   域内 5 份系统日志 10000/10000 = 100%；含负样本 nginx_plain 11500/12500 = 92.00%
脱敏     替换 12268 次；11575 个排障字段（uid=/pid=/HTTP 状态码/请求路径/容器 ID）零误伤
Golden   离线 18/18 断言 ¥0；真模型 18/18 断言 ¥0.025524，0 条无证据的 fact
E2E      16/16；真实调用 1607/3100 tokens、¥0.0140、8 条结论
全量     pytest 693 passed / 3 skipped（跳过的是需 RUN_LIVE_MODEL_TESTS=1 的付费用例）
```

> `logs/` 是外部公开真实日志语料（出处见 `logs/README.md`），已在版本库内，
> 故**全新 clone 也跑得了这些基线**（`.gitattributes` 对 `logs/*.log` 关掉了
> 换行符规范化：语料字节不能被悄悄改写，否则基线不可比）。

---

## 7. 排障

| 症状 | 处理 |
|---|---|
| `docker compose ps` 里 migrate 显示 `Exited (0)` | **正常**：迁移跑完即退出，web/worker 等它成功后才启动 |
| web 起不来，日志显示连不上数据库 | 确认 postgres 是 `healthy`；`docker compose logs postgres` |
| 模型分析总是降级成纯规则报告 | `.env` 里 `MODEL_PROVIDER_API_KEY` 是否为空／是否有效 |
| 报告里没有结论 | 看 Run 状态：`partial_success`/`failed` 会写明中断原因；L0（正常日志）本就无异常结论 |
| 想确认某次分析到底做了什么 | 打开 `/runs/<id>/detail?project_id=<pid>`，页面会复述阶段、花费、结论与证据 |
| 端口被占用 | 改 compose 里的端口映射；注意只绑 `127.0.0.1` |
| Celery 在 Windows 卡住 | 直跑时必须 `-P solo`（compose 里已加） |

---

## 8. V1 有意不做的事

这些不是遗漏，是**刻意不做**（完整清单与再评估信号见编码计划第 20 节）：

- 跨文件 / 长期历史 Baseline、季节性建模（只做文件内统计）
- 自动分片聚合、实时流处理、Multi-Agent
- 自动执行处置步骤（只读定位：runbook 仅展示，不自动跑命令）
- CSV / JSON 导入（V1 只支持 TXT / JSONL）
- Kubernetes、多环境流水线、复杂计费与角色权限
- 自动化 LLM Judge（Golden Set 先人工核对）

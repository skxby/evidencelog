# Log Intelligence Agent

产品/设备日志智能分析 Agent 平台。V1 只做一件事：把日志变成**带证据、可追溯、成本可控**的分析报告。

> 施工依据：《Log_Intelligence_Agent_V1_个人全栈工程编码计划》。本 README 只写**怎么把环境跑起来**，架构与阶段设计看那份计划。

---

## 1. 前置要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Docker Desktop | 已装并**保持运行** | PG 与 Redis 都跑在容器里，本机没有原生安装 |
| Python | **3.13**（不要用 3.14） | Celery 官方支持列表到 3.13 为止 |
| Git | 任意近期版本 | |

## 2. 首次启动

```bash
# 1) 起基础设施（PostgreSQL 15 + Redis 7）
docker compose up -d

# 2) 确认两个容器都 healthy
docker compose ps

# 3) 建虚拟环境并装依赖
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS / Linux

# 4) 配置
cp .env.example .env
# 至少填：SECRET_KEY、MODEL_L1/L2/L3、MODEL_PROVIDER_BASE_URL、MODEL_PROVIDER_API_KEY、
#        以及各等级的成本单价

# 5) 启动 API
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

## 3. 验证环境真的通了

```bash
# 存活检查
curl http://127.0.0.1:8000/healthz

# 就绪检查：会真的去连 PG 和 Redis，任一不通返回 503
curl http://127.0.0.1:8000/healthz/deps

# 接口文档
# 浏览器打开 http://127.0.0.1:8000/docs
```

跑测试：

```bash
# 全量（需要容器在跑）
.venv/Scripts/python.exe -m pytest

# 只跑不需要外部依赖的单测
.venv/Scripts/python.exe -m pytest -m "not integration"
```

## 4. 验证 Celery Worker 能消费任务

需要开两个终端。

```bash
# 终端 A：worker
.venv/Scripts/python.exe -m celery -A app.celery_app:celery_app worker -l info -P solo
# 注：Windows 上必须加 -P solo（或 threads），默认的 prefork 在 Windows 不可用

# 终端 B：投一个冒烟任务，期望返回 2
.venv/Scripts/python.exe -c "from app.tasks.health import ping; print(ping.delay(1).get(timeout=30))"
```

## 5. 环境变量

见 `.env.example`，每项都有注释。两条硬规则：

1. **具体型号只出现在 `.env`**。业务代码只认 `L1` / `L2` / `L3`，通过 `Settings.tier_model()` 翻译。
2. **密钥不进代码、不进 Git**。`.env` 已在 `.gitignore` 里。

## 6. 目录结构

```text
app/
├── main.py              FastAPI 入口（阶段 01 只有健康检查）
├── config.py            pydantic-settings 配置 + 等级→型号映射
├── db.py                engine / SessionLocal / get_db / Base
├── celery_app.py        Celery 实例
├── models/              SQLAlchemy 模型（阶段 02 起，共 9 张表）
├── domains/             领域「目录包」插件
│   ├── base.py          DomainBase（阶段 03）
│   ├── registry.py      DomainRegistry（阶段 03）
│   └── computer_monitoring/
│       ├── parser.py
│       ├── analyzers/
│       ├── knowledge/{confirmed,staging}/
│       ├── runbooks/
│       └── config.py
├── parsers/             TXT / JSONL 通用 parser 基类（阶段 04）
├── tools/               通用数据算子与注册器（阶段 05）
├── gateways/            模型网关与路由（阶段 06）
├── policy/              策略与成本控制（阶段 07）
├── analysis/            分组 / 复杂度评估 / Context 构造 / 编排（阶段 09）
├── tasks/               Celery 任务
├── api/                 路由（阶段 10）
├── web/                 Jinja2 模板与静态资源（阶段 11）
└── utils/               脱敏 / token 估算 / 时间戳归一
uploads/                 脱敏后的日志文件（运行数据，不进 Git）
tests/{unit,integration,datasets}
migrations/              Alembic
```

## 7. 本仓库相对计划文档的差异

以下几处是我落地时做的判断，**都可以推翻**：

| 项 | 计划原文 | 实际做法 | 理由 |
|---|---|---|---|
| 阶段 01 的 compose | 只写「起 PG/Redis」 | 只含 postgres + redis，web/worker/beat 留到阶段 14 | 与阶段 14 的分工一致，避免现在就要写 Dockerfile |
| 容器端口 | 未指定绑定地址 | 一律绑 `127.0.0.1` | 与「只读 + 本地」的隐私定位一致，不暴露到局域网 |
| 模型单价配置 | 只说「自行填入配置」 | 补了 `MODEL_{L1,L2,L3}_PRICE_{INPUT,OUTPUT}_PER_1M` | 原 `.env` 示例里没有这两项，但阶段 07 要算成本 |
| 密码哈希 | 未指定库 | 直接用 `bcrypt` | `passlib` 自 2020 年起无人维护，与 `bcrypt>=4.1` 组合会抛版本读取错误 |
| `pytest-celery` | 列为可选项 | 暂不引入 | 它默认拉起 Docker 托管的 worker 容器、测试期额外拉镜像；计划同时给了「直接测函数」的替代路径 |
| 领域目录位置 | 3.3 节写 `domains/`，阶段 01 写 `app/domains/` | 采用 `app/domains/` | 与阶段 01 的目录树一致 |

## 8. 尚未解决的设计问题

这几处计划文档内部不一致，**开工前需要拍板**，详见评估结论：

1. 阶段 11 要做知识审核（确认/编辑/丢弃/标记误报），但阶段 10 的 API 清单里没有对应端点。
2. 知识 YAML 到底是「随 domain 进 Git 的代码资产」还是「运行时写入的数据」——影响 Docker 卷设计。
3. `AgentRun` 没有 `current_phase` / `progress` 字段，但阶段 11 要显示阶段进度。
4. `Event.group_id` 与 `EventGroup.event_ids` 互为反向索引，真源未定义。
5. Incident 归并的「同根因」判定算法未定义。
6. 鉴权机制（JWT vs session cookie）未定。

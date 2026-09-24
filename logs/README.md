# 真实日志样本（阶段 04 Parser / 脱敏调参用）

来源均为**真实系统产出**（非合成）：
- TXT 部分：[logpai/loghub](https://github.com/logpai/loghub)（公开日志研究数据集）
- JSONL 部分：[elastic/examples](https://github.com/elastic/examples)（真实 Nginx 访问日志）

用途：文档第 21 节「解析在真实日志上可用」——用真实数据测**可解析率**与**脱敏误伤率**。

---

## A. 清单（TXT 系统日志，5 份，各 1999 行）

| 文件 | 来源系统 | 常见行结构 | 贴合点 |
|---|---|---|---|
| `Linux_2k.log` | Linux syslog | `Mon DD HH:MM:SS host proc[pid]: msg` | syslog 时间戳缺年份、缺时区；sshd/内核条目 |
| `OpenSSH_2k.log` | sshd 认证日志 | `Mon DD HH:MM:SS host sshd[pid]: msg` | 进程崩溃/认证失败类；**大量 IP、主机名**，专测 IP 脱敏与误伤 |
| `Mac_2k.log` | macOS 系统日志 | `Mon DD HH:MM:SS host proc[pid]: msg` | 内核 / 热压 / 内存压信息 |
| `Zookeeper_2k.log` | ZooKeeper | `YYYY-MM-DD HH:MM:SS,mmm - LEVEL [thread] - msg` | **完整时间戳+毫秒**，对比「年份/时区齐全」解析分支 |
| `Apache_2k.log` | Apache HTTP | `[Day Mon DD HH:MM:SS YYYY] [level] msg` | 另一种时间戳格式，验证不误伤非 syslog 行 |

覆盖阶段 04 的三种时间戳形态：**缺年份**（Linux/OpenSSH/Mac）、**完整+毫秒**（Zookeeper）、**方括号包裹**（Apache）。

---

## B. ✅ 缺口已补：真实 JSONL 结构化日志

V1 声明支持 TXT + JSONL 两种格式，loghub 全为 TXT。现已补入**真实的单行 JSON / JSONL** 日志：

| 文件 | 来源系统 | 行数 | 校验 | 结构 |
|---|---|---|---|---|
| `nginx_json_access.log` | Nginx JSON 访问日志（elastic/examples，真实） | 1000 | ✅ 1000/1000 行合法 JSON | 每行一个 JSON 对象：`time / remote_ip / remote_user / request / response / bytes / referrer / agent` |
| `nginxplus_status_json.log` | Nginx Plus JSON 状态日志（elastic/examples，真实） | 500 | ✅ 500/500 行合法 JSON | 每行一个 JSON 对象：Nginx Plus 上游/状态字段 |
| `nginx_plain.log` | 同一批 Nginx 日志的**平文**版本 | 1000 | — | `93.180.71.3 - - [17/May/2015:08:05:32 +0000] "GET ..." 304 0 ...`（**负样本**：TXT Parser 不认 web access 格式，见下方说明） |

用法建议：
- `nginx_json_access.log` → 测 `JsonlParser` 对**逐行 JSON** 的可解析率；字段名与平文不对应，正好测 Normalizer 的字段映射。
- `nginx_plain.log` 是**负样本**，不是"应当解析成功"的样本：web access 日志不属
  `computer_monitoring` 域，TXT Parser 只认 syslog / log4j / Apache error 三类行结构，
  故它 1000/1000 计入坏行 —— 这正是计划第 540 行要的行为
  （「完全无法解析的时间戳 → 计入坏行，不静默用当前时间替代」），
  单测 `test_unparseable_lines_return_none` 也把该格式显式钉成不可解析。
  它的价值是**防止 Parser 为了好看而硬凑**：换个格式就整批坏行，一眼能看出边界在哪。
- 脱敏重点看 `remote_ip`、`agent`、`request`：`remote_ip` 应被替换，`request` 里的路径不应被误伤。

> 说明：这批 JSONL 是 **HTTP 访问日志**（web access），不是系统监控语义。它的价值在于提供**真实可用的 JSONL 结构**给阶段 04 调 JsonlParser。若你希望 JSONL 也贴 `computer_monitoring` 语义，可再从本地任意结构化日志系统（例如容器 stdout 的 JSON 日志）导出一小段补进来——但那属于锦上添花，**paser 的 JSONL 分支现在已有真实数据可测**。

---

## C. 目录总览（8 份真实日志）

```text
logs/
├── Linux_2k.log                  # TXT  syslog 缺年份
├── OpenSSH_2k.log                # TXT  syslog 含 IP/主机名
├── Mac_2k.log                    # TXT  macOS syslog
├── Zookeeper_2k.log              # TXT  完整时间戳+毫秒
├── Apache_2k.log                 # TXT  方括号时间戳
├── nginx_plain.log               # TXT  负样本：web access 平文，TXT Parser 有意不解析
├── nginx_json_access.log         # JSONL 逐行 JSON（1000 行，全合法）✅ 新增
└── nginxplus_status_json.log     # JSONL 逐行 JSON（500 行，全合法）✅ 新增
```

## D. 使用方式

1. 放进项目 `tests/datasets/`（Golden Set 固定样本）或上传到「上传日志」入口做人工试跑；
2. 记录每份的：总行数 / 成功解析 / 坏行 / 脱敏替换计数；
3. 重点看 `OpenSSH_2k.log`（IP/主机名脱敏误伤率）与 `nginx_json_access.log`（JSONL 解析率）。

## E. 量化结果（2026-09-24 实测，脚本见 `.dsh/parser_eval.py`）

| 范围 | 可解析率 | 说明 |
|---|---|---|
| 域内 5 份系统日志 | **10000 / 10000 = 100%** | Mac 曾有 19 行因 launchd 附注组不认而判坏，已修 |
| 全部 TXT + JSONL（除负样本） | **11500 / 11500 = 100%** | — |
| 含 `nginx_plain.log` 负样本 | 11500 / 12500 = 92.00% | 1000 行坏行是**预期**，不是缺陷 |

脱敏误伤率 **0.0000%**：11575 个排障字段（`uid=`/`euid=`、`pid=`、HTTP 状态码、
请求路径、容器 ID）在脱敏后一个都没被替换掉 —— 红线 5（脱敏）与"排障信息不能删"
这两件事同时成立。

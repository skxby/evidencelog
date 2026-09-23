# Seed 知识（代码资产）

放**你手写的初始 confirmed 知识**。这些随领域目录包一起版本化、进 Git、进镜像（只读）。

## 为什么这里是空的

计划第 498 行明确：

> 具体知识内容（真实错误码、故障模式、修复经验）必须来自你的真实日志与经验，
> 我不虚构这些条目，只提供结构与流程。

所以这里只给结构与校验，**不代填内容**。等你从 `logs/` 与真实排障经验里
总结出第一条模式时，按下面的格式放一个 `*.yaml` 进来即可。

## 格式

```yaml
- id: kp_cpu_001
  kind: error_pattern          # error_pattern | root_cause_hint | fix_suggestion | false_positive
  title: CPU 持续高占用
  match:
    event_type: metric
    metric_name: cpu_used
    condition: "value > 90"    # V1 只支持 value <op> <number>，op ∈ > < = >= <= !=
    min_samples: 3
  message_pattern: null        # 可选：message 正则
  severity_hint: medium
  description: ""
  related: []
  evidence:                    # confirmed 必填
    run_id: ""
    event_ids: []
  confidence: 0.8
  status: confirmed            # draft | confirmed | deprecated
```

## 校验规则（加载时会强制）

- 无 `evidence`（run_id / event_ids）或 `confidence <= 0` 的条目**不允许** confirmed；
- `condition` 只接受 `value <op> <number>`，不做通用表达式引擎；
- `status` 只接受 draft / confirmed / deprecated。

## 运行时数据的写入位置（不要写到这里）

人工确认后生效的知识属于**运行时数据**，要写数据卷：

    ${DATA_DIR}/knowledge/computer_monitoring/confirmed/

**不要把运行时数据写进本目录**——本目录是代码资产，写进来会让镜像不可复现、
Git 变脏（修订说明第 2 条）。
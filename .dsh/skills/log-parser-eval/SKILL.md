---
name: log-parser-eval
description: 用 logs/ 下真实日志量化 Parser 可解析率与脱敏误伤率。触发词：测解析率、误伤率、parser 效果、脱敏误伤、真实日志测试。
---

# log-parser-eval —— 真实日志解析 / 脱敏评估

## 何时用
用户说「测解析率/误伤率」「用真实日志验证 parser」「脱敏有没有伤到排障信息」。

## 步骤（用确定性脚本，不调 LLM）
1. 遍历 `logs/*.log`（含 5 份 TXT + 2 份 JSONL）。
2. 每份统计：总行数 / 成功解析 / 坏行 / 脱敏替换计数。
   - TXT 用对应 Parser；JSONL 逐行 `json.loads` 校验。
3. 重点样本：`OpenSSH_2k.log`（IP、主机名替换比例，是否误伤 `uid=`/`euid=` 字段）、`nginx_json_access.log`（JSONL 解析率，`request` 路径是否被误伤）。

## 输出格式
| 文件 | 总行 | 成功解析 | 坏行 | 脱敏替换 | 误伤样本 |
|---|---|---|---|---|---|

结尾给：整体可解析率、平均误伤率、值得修的 Top3 规则。

## 失败处理
某文件解析报错 → 保留报错原文与首个坏行，不跳过、不静默。

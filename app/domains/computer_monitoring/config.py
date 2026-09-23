"""computer_monitoring 的默认配置与版本号。

版本号会写进 Run 的幂等键（计划第 700 行），**改了就必须变**，
否则同一幂等键会命中不同语义的结果，历史不可复现。
"""

from __future__ import annotations

#: 领域标识：注册表按 (domain_id, version) 精确加载
DOMAIN_ID = "computer_monitoring"

#: 领域版本。改动解析规则 / 阈值 / runbook 语义时都要升版本。
DOMAIN_VERSION = "1.0.0"

#: 新增领域时只加目录包 + 注册一行，内核不动（计划第 172 行）。
DESCRIPTION = "计算机监控日志领域：syslog / dmesg / log4j 风格的系统日志与指标"

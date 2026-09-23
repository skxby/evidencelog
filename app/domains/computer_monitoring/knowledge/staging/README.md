# Staging：模型生成的候选知识（运行时数据，**不是**这里）

本目录在代码仓库里只是占位。真正的候选写在数据卷：

    ${DATA_DIR}/knowledge/computer_monitoring/staging/candidates.yaml

理由（修订说明第 2 条）：运行时数据不写代码目录，否则镜像不可复现、Git 变脏。

## 约束（计划第 491–496 行）

1. 候选**一律是 draft**，不参与自动结论，只作提示；加载器会强制降级为 draft；
2. 候选必须绑定真实 `event_id`（子集校验），不允许凭空总结；
3. 无 evidence、无 confidence 的条目不允许被确认为 confirmed；
4. `match` 规则由模型生成草案，**必须人工确认才生效**。
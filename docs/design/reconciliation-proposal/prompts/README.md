# Reconciliation 中文 Prompt 的修改入口

本目录保留设计复阅稿。运行时已采用同版内容，实际修改入口是 src/cortex/capabilities/graph_merge/llm/prompts/reconciliation；此处生成器只构建设计报告，不部署或覆盖运行时。

| 文件 | 内容 |
| --- | --- |
| common.zh.md | 两个角色共用的图语义与读取前提。 |
| writer.zh.md / assessment.zh.md | 各自简短角色职责。 |
| writer-contract.zh.md / assessment-contract.zh.md | 各自输出、工具使用与恢复语义。 |
| ../signals/<kind>.md | 当前 Signal 的共用依据及两个角色分段。 |

Signal 文件用 `## Writer 判断与调整步骤` 和 `## Assessment 核查要点` 分隔角色内容；构造器也兼容原有冒号标题。角色前缀各出现一次且 Writer 在前，其前面的部分是共用判断依据。不要把评估状态或 Review 填写规则放入共用段。

运行仓库虚拟环境的 Python 执行 `docs/design/reconciliation-proposal/build_proposal.py`，重新生成 output/reconciliation_proposal_20261120 的完整 Prompt、示例与 HTML；构造器校验角色隔离。修改源文件后应重新生成，输出文件不是独立维护入口。

呈现格式采用用户提供的 abstract_predicate 示例：加粗标签、分层条目、字段表格；共用契约、角色、工具输出、当前 Signal 之间用分隔线分开。Signal 标题在 Writer 中生成“处理规范”，Assessment 中生成“核查规范”。

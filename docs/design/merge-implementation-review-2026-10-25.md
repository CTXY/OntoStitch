> 2026-10-25 更新：运行时现已统一为官方文件型 SQLite / SqliteSaver，移除 PostgreSQL 依赖。下文涉及旧 PostgreSQL 决策的内容仅保留为设计历史。

# Merge 实现与验收记录

本轮在现有工作区重构基础上实施；没有新增 Concept/target 字段，没有提交 Git commit。完整工作流说明见 [事务式 merge 设计](merge-transaction-workflow.md)。

## 已实施

- GraphManager 允许单 target 引用多张物理表，继续检查 schema、引用、端点、分支与图内部一致性。业务等价、计算正确性和适用范围由外部 Agent 判断。
- target 去重不再对完整 SQL casefold，保留字面值和标识符大小写。模型、Learning 与 Merge 的主要契约说明统一为完整物理表达。
- 三类 matching 继续采用语义身份。Concept 增加完整 target 召回材料；Metric 提供完整 plans；Rule 使用请求结果、业务角色、独立要求、实现差异四问。operation_intent 不再硬过滤。
- Matching 可以读取限定来源的 question/SQL，截断明确标记。未证明同身份时保留独立节点；未评估的跨类型替换不进入普通 merge。
- 父 LangGraph 逐组保存工作图；每组使用固定 existing revision。原始输入、当前图和候选图分开读取，包含 incoming consumers。
- Agent 返回完整候选，程序在 GraphManager 副本中预览。结构反馈进入修订，Assess 检查泛化与逐来源覆盖，随后通过同一 apply 入口原子更新工作图。
- 受信 reviewed_reconciliation 支持已有 conflict 整合为新普通定义。它不向 Agent 开放；冲突没有被证据解释时保留完整 alternatives 和 conflict 标记。
- 显式依赖和引用闭包参与调度与变化传播。分支事实变成无条件事实必须由 Assess 逐 edge ID 批准；未批准不能接受候选。
- 身份误分组可经独立评估后撤销，保留原始成员；这属于工作流协议，不进入 ontology schema。
- LangChain ToolStrategy 继续负责结构化结果。自动 tool choice 兼容 reasoning provider；非法 JSON/tool call 会得到具体反馈并重新提交，调用预算仍生效，不宽松修复后直接接受。
- LangSmith 原生 model/usage 追踪和 transaction span 保留 run/group/revision/事务状态，原生摘要客户端隐藏图和 SQL payload。
- 恢复指纹包含相关实现源码摘要、配置与 prompt 版本；拒绝混用不同代码或输入的 checkpoint。生产默认 Postgres；本次本地验证显式使用官方 SQLite saver。

## 测试与审查

`python -m pytest -q`：**514 passed**，一项既有 Pydantic deprecation warning。相关文件 Ruff lint/format 和 diff whitespace 检查通过。

新增或扩充测试覆盖：多表 target、大小写常量、事务回滚、已有 conflict 的受控融合、完整竞争定义、候选预览、结构反馈修订、身份纠正、物理 owner 映射、显式依赖与消费者处理顺序、关系范围审批、组间 checkpoint 恢复、nested agent 工具读取恢复、自动 tool choice、非法 JSON 反馈及预算耗尽保留。

三个 subagent 分别审查图契约/事务、工作流与恢复、语义匹配与最终图。审查发现并修复：预处理绕过显式关系保护、conflict 预览过早拒绝、仅改分支边缺少 owner 操作、边 variant 映射歧义、遗漏显式依赖传播、分支事实未经评估变成无条件事实。最终未报告新的 P1。

## 五个真实 case 的结果

最终输出在 `artifacts/merge_review/thrombosis_prediction/reviewed_final/`。为了集中检验 Consolidate/Assess，本次最终运行复用前一轮已独立核对的 matching；脚本验证 existing、incoming、schema 完全相同，并将复用分组摘要加入恢复指纹。三组合并的 Consolidate/Assess 全部重新调用真实配置模型。

| 内容 | 结果 |
| --- | --- |
| 输入 | 1151、1153、1154、1157、1171；18 个语义节点 |
| 输出图 | 83 节点、85 边 |
| 物理节点 | 69 |
| 语义节点 | 14：9 Concept、1 Metric、4 GenerationRule |
| 实际整合 | Diagnosis/disease、sex、三个 Patient–Examination JOIN Rule |
| 原样保留 | 11 个 singleton，除 canonical ID 外逐字段保持 |
| 最终校验 | GraphManager 验证通过，0 diagnostic |
| 冲突 | 0；本批未发现需要保留的真实竞争定义 |

独立逐内容审查确认：Diagnosis 与 sex 的完整 target 和来源保留；Patient–Examination JOIN 继续使用两 ID 关联，condition 明确要求保留患者具有匹配检查记录，action 保留患者—检查记录对，没有引入 DISTINCT 或唯一性假设。患者计数、列表去重、Patient–Laboratory 关联继续独立保存。

本轮早期真实运行遇到 provider 不支持 forced tool choice，以及模型工具参数非法 JSON 转义的问题。失败运行保留了原始完整 alternatives 并正确标记 conflict/unresolved；它们不是已确认的业务竞争，也不被当作成功语义验收。修复后最终三组均 accepted。

使用相同 run ID、输入和当前实现再次运行最终评估，成功恢复已完成结果；本地 Agent trace 仍为 9 条，没有新增模型调用。

## 明确边界

- GraphManager 通过不等于业务正确性证明；514 项测试通过也不替代 SQL replay。
- 原始 1171 的 COUNT 口径仍有抽取质量疑点，本轮没有静默改成 COUNT DISTINCT。
- singleton 是原样保留，没有重新抽取或全面语义复核。
- 当前 Concept 没有通用的外部条件 target 选择结构；无法清楚表达时保留完整未决定义。
- 本轮没有执行数据库 SQL replay。真实验收范围是上述五个 case 的来源覆盖、图完整性和独立语义审查。

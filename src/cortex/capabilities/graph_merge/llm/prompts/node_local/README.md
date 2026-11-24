> Active runtime prompts are the `.en.md` assets. The `.zh.md` files are retained earlier review copies and are not loaded.

# 节点整合 Prompt 的编辑入口

本目录用于节点 Writer（consolidate）与独立节点 Assessment（node_assess）。
`node_contract.py::node_system` 按固定顺序构造每次请求的 System：

1. `common-fields.en.md`：两角色共用的保义与整合原则。
2. `writer.system.en.md` 或 `assessment.system.en.md`：当前角色、局部输入与工具边界。
3. 对应的 `*-output-contract.en.md`：必须遵守的输出字段与覆盖、引用约束。
4. `types/<node_type>.en.md`：只加入当前 group 的唯一类型 guidance。

通用规则放在前三层；类型文件只写类型专属要求，避免复制整个共用契约。
角色工具固定为按 ID 限制的 `read_evidence` 与唯一结果工具，不开放整图查询或边编辑。
边的机械映射由代码完成；必须进行语义决策的关系通过 `relation_followups` 移交，节点通过不代表最终 graph 已通过。

字段 shape 的可执行来源是 `node_contract.py` 的 `NodeWriterResult` 与 `NodeAssessmentResult`；修改 Prompt 时须同步检查实际工具 schema 和局部验证。
此目录由用户复阅的 V4 草案接入，旧草案保留在 `output/node_consolidation_local_review_20261120/simplified_v4_archive/`。

当前 V6 契约：一次输入一个固定 Identity 候选组，Writer 可输出多个互不重叠的整合子组，以及显式独立保留的成员。所有成员恰好覆盖一次；各子组使用固定输入 manifest 中自己的来源 ID，不按结果 owner 重新生成。Assessment 的 candidate.integrated_groups 展示每组成员及路由，candidate.nodes 展示全部结果，candidate.singleton_ids 展示独立成员。全部结果在同一任务内编译、评估和原子提交，不返回 Identity、不创建子任务。

# Merge operator 与现行图契约、论文的复查

日期：2026-10-25。基于当前工作区（含此前尚未提交的重构），不是 Git HEAD。对照用户提供的 `/path/to/user/SIGMOD_AutoOnto/secs/05-Integration.tex`。本轮只新增数据转换脚本、输入产物和复查记录，不修改 merge 运行时。

## 结论

算法主线已经与论文基本一致，建议保留现有 LangGraph 双角色修订工作流；优先补齐图契约与候选接受边界，而不是再重写一套 merge 框架。目前不能以测试通过认定已经完成真实数据上的语义验收。

## 已经对齐的部分

| 论文要求 | 当前实现 | 判断 |
| --- | --- | --- |
| 物理对象按身份匹配 | identity.py 的 exact_identity_key / _select_exact | 已实现 |
| 从既有组和未处理池检索同类型语义候选 | IdentityGrouper.group / _select_semantic | 已实现；top-k 召回影响结果 |
| 共同内容、差异解释、未决冲突可在一组共存 | ConsolidationAnalysis + candidates + difference_points | 数据模型与 prompt 对齐；K_B 在 difference_points 中，不在 analysis 内单独重复存储 |
| E_B 包括 question、gold SQL 和依赖定义 | evidence resolver + GroupContext + read_evidence/read_nodes | 已接入；依赖细节可按需读取 |
| Consolidate 与独立 Assess 循环修订 | workflow/group.py，generalization 与逐 source coverage | 已实现；分析与候选可一起修改 |
| 有界失败后保留原定义 | unresolved_refs / preserved_proposal / materialize | 已实现定义保留路径；最终图失败尚不进入该回退 |
| 保留完整竞争定义及关联依赖 | compact conflict overrides + alternative_ids 派生边 | 分支存储已实现；同一 candidate 内的条件依赖主要仍由文本表达 |
| 最终结构与引用有效才接受 | operator.merge 的 GraphManager 输入/输出验证 | 最终边界已有校验，但它与组内接受标准不一致 |

LangChain 用于有调用预算的结构化角色和只读工具；LangGraph 管理 run/group checkpoint 与修订；LangSmith tracing 接入运行时。采用这些库本身不能证明语义合并正确。

## 优先讨论和修改的事项

### P1：候选校验与最终 GraphManager 契约不一致（已复现）

位置：`reconciliation/validation.py::validate_proposal`、`workflow/group.py::consolidate`、`operator.py::_merge`。

组内只调用基础 schema 和 validate_semantic_properties，再手写引用类型检查。它没有完整复用 GraphManager 的跨节点约束。例如 Concept 的一个 target 同时引用 `Patient.ID` 与 `Examination.ID`：当前 validate_proposal 返回成功，而 GraphManager.prepare_fragment 拒绝，报 `Concept target spans multiple physical owners.`。

后果：即使 Assessment 已通过，最后 prepare_state 仍可能抛 FinalGraphStateInvalid；错误没有作为 feedback 返回 Consolidate，也不会自动保留原定义。这与论文“结构、引用有效后接受，有界失败保留”的要求存在实际缺口。

建议：在组内临时 overlay 上使用 GraphManager 的同一内容/关系规则，结构化 issues 返回修订回路；最终 remap 后再做整图验证。整图层面错误也要有明确的安全回退或失败报告，不能把局部 accepted 等同于已可发布。不要再扩展第二套手写校验表。

### P1：依赖变化传播只覆盖 existing + incoming 的混合组（代码确认）

位置：`operator.py` 108–119 行与 128–148 行；`reconciliation/service.py::reconcile_group` 的 direct shortcut。

dependency_variants 仅登记同时包含 existing_node 和 incoming_node 且内容不同的组。首次批量整合中的 incoming-only 差异组不会登记。已有 conflict 本轮没有新增定义时，也不会因为该 conflict 的存在自然触发相同机制。

例如上游两个指标定义在同一次 batch 中发生竞争，下游节点即使直接依赖它，仍可能作为 singleton 走直接保留路径；本轮新产生的 conflict 也没有被送回其它组的 dependency_conflicts（传入的只是 existing.node_conflicts）。ID remap 可以完成，但不能证明上游竞争口径与下游定义的语义关联已经得到评估。

建议：先计算全组依赖图和所有可能改变含义的身份组，不以 origin 组合判断是否影响依赖；在本轮候选/冲突结果可见时检查受影响的消费节点。若下游接受新冲突依赖，必须明确其分支选择或保留原语义绑定。用“空图 + 多 case 同批输入”和“已有 unresolved 依赖 + 新消费节点”分别验收。

### P1：跨类型替换位于普通整合之前，缺少完整定义保留路径（代码确认）

位置：`identity.py::_cross_type_replacements`、`IdentityGrouper.group` 的 discarded、`operator.py` 的 cross_type_replacements。

论文 Node Matching 检索同类型节点。当前额外对同名 Concept/Metric 做跨类型选择，并将 loser 从普通分组池排除；该定义不会再进入 Consolidate/Assess 的逐 source coverage，随后只把 ID 指向 winner，端点类型不再合法的边还会被丢弃并记录 diagnostic。

候选包含 existing 与 incoming 的全部 Concept/Metric 组合，没有要求至少一个成员来自 incoming；因此一次新增图合并还可能重新裁决两个既有资产。它适合处理抽取类型纠正，但需要独立的语义与证据契约，不能仅当作普通同义匹配。

建议：本轮论文一致性路径默认只做同类型整合。若要保留纠错能力，把它作为明确的类型纠正操作，要求 loser 的定义、来源与依赖得到 coverage 验证；禁止无关 incoming 触发 existing-only 类型替换。是否启用该能力需要先确定产品范围。

### P2：条件相关关系尚缺少与候选定义共同修改的协议（设计边界）

位置：`reconciliation/models.py::ConsolidationCandidate`、`schema/relationships.py::synchronize_reference_edges`、`edges.py::normalize_edges`。

Candidate 只能输出 NodeContent 与 supporting_refs；不能显式提出关系保留、修改、删除。派生关系随 refs 重建，竞争 alternatives 的依赖可以用 alternative_ids 关联，这是已有的正确基础。但同一个 generalized candidate 里多个条件分支的依赖主要依靠 description、plans 或 rule action 文本解释，边上没有通用的条件分支身份。显式关系通常 remap 后保留或去重，不受候选的语义选择直接控制。

建议先确定消费方式：若 SQL Agent 阅读完整定义后选择条件，允许文本条件，但 Assessment 应验证“条件 + 实现 + 依赖”可重建；不能让检索到的全部边被解释成同时必需。如果要求程序自动选择，则需最小的分支标识与依赖归属表达。不要仅因论文用了参数符号就额外建设一套参数 DSL。

### P2：恢复粒度与运行版本边界应明确（代码确认）

位置：`workflow/run.py::run_merge` 的 match_nodes / assemble_graph，`bootstrap/graph_merge.py::workflow_version`。

当前保存整个 identity grouping 之后才完成 match_nodes checkpoint。若在 group() 循环途中中断，恢复会重新执行该阶段已做过的 identity 模型判断。组内完成的 consolidate/assess 则有更细的 checkpoint。这是合理的粗粒度取舍，但不能宣称任意 identity 调用中断均无需重复。

请求包括图、schema 和配置/prompt 派生的 workflow_version，已有输入变更保护；它没有自动包含运行时代码版本。相同 run-id 在改过 materialization/validation 代码后恢复时，仍可能混用旧决策与新实现。

建议：短期明确 match 阶段可重做，并记录 build/revision 标识；若实际费用或稳定性需要，再按 seed decision 保存。恢复测试应覆盖 match 循环中断、模型/工具调用中断、assemble 失败及完成结果重新验证。

## 五个 case 的适配与质量边界

产物：`artifacts/merge_review/thrombosis_prediction/`。脚本：`scripts/prepare_merge_review_cases.py`。

选取 1151、1153、1154、1157、1171；共 18 个语义节点：1 Metric、11 Concept、6 GenerationRule。公共物理图共 69 节点、68 边。旧 Concept maps_to_value 只连 Value 时会漏掉 supporting Column；新输入从完整 target 重建 grounded_on，所以 case 1151、1157 的关系数各增加一条。没有删除公式 refs，也没有从名称猜物理端点。

该组覆盖同义概念、同名而描述不同的概念、相同物理 JOIN 的不同语境规则、去重与计数；不保证构成论文 N1/N2/N3 那样的混合冲突组。下一轮应专门补充真实或明确标记为合成的混合组。不要为制造冲突去改写这五个来源定义。

旧抽取质量也需分开处理：1171 的 COUNT 非空 ID 在一对多连接后可能是在计匹配记录，而其 condition 仍概括为“患者数量”；1157 的 JOIN reason 将 Patient ID 关联解释为每个患者自己的检查，但没有证明结果行唯一。迁移保留这些内容，后续审查应结合 gold SQL 判断，不能自动把 COUNT 改成 COUNT DISTINCT 或补唯一性。

## 已做验证与未做验证

- 五个 standalone 图均经 GraphManager.prepare_state(strict=True) 和 validate_state；公共物理图同样验证；所有 case fragment 与 batch 经 prepare_fragment。
- 四个现有测试文件共 37 项通过：test_graph_merge_workflow、test_graph_merge_agent_resume、test_graph_merge_edges、test_sync_training_merge。出现一项已有依赖的 Pydantic deprecation warning。
- 使用五个 case 实际执行 offline operator：87 节点、91 边、18 项 incoming 映射，最终 GraphManager 校验通过。该模式不调用语义匹配模型，0 conflict 不代表“没有业务冲突”。
- 人工构造跨物理 owner 的 Concept 候选，确认局部 proposal 接受、GraphManager 拒绝的校验差异。
- 转换脚本通过 Ruff lint/format。未运行真实 LLM、embedding、Postgres 恢复、LangSmith 云端追踪或数据库 SQL replay；本轮没有修改运行时修复上述问题。

## 建议下一轮顺序

1. 统一组内与最终图的校验，确保反馈能够进入已有修订循环，并明确整图失败的保留策略。
2. 补齐 batch 内依赖变化/冲突的传播；限制跨类型替换的范围与证据要求。
3. 确定文本条件与自动选择的消费边界，补齐依赖分支验收样例。
4. 用本输入跑 live semantics，检查每个匹配组、差异解释、逐来源覆盖和图校验结果；再做 SQL replay 与恢复测试。

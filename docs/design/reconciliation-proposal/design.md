> 状态更新：本页保留设计阶段记录；实际运行代码、当前Prompt入口与验证范围见[Reconciliation运行时](../reconciliation-runtime.md)。

# Reconciliation 完整设计方案（待 Review）

本方案重整图关系协调的职责、Ontology 契约、Signal guidance、局部子图呈现、文件式草稿、两个 Agent 的完整 Prompt 及上下文管理。**本轮不修改生产代码/Prompt，不切换后台 runtime，不迁移 checkpoint，不调用模型。** 文档目录中的可执行 schema/模拟仅用于让拟议字段和示例可核对，不能当作新运行链路已完成。

## 1. 先解释“直接 owner”，并从主叙述中去掉这个术语

旧文档的直接 owner 只指这条边：

```text
Concept C --grounded_on--> 物理字段或值 P
```

它不表示业务上的“拥有者”，不是另一类 Ontology 节点，也不要求加 owner 属性。来源manifest中的owner_id同样只是编码中表示知识承载节点的句柄，不是新增的业务归属关系。Metric/Event 也可以有自己的物理输入/表示边；“每个物理端点只一个 Concept”只数 Concept 的直接 grounded_on，不能推广成所有语义节点只允许一条边。

当前代码有一项**本项目建图政策**：同一 GraphState 内，一个物理 ID 最多接收一个不同 Concept 的直接 grounded_on。现行校验按物理 ID 全局计数，并未按描述中的范围或 alternatives 分支隔离计数。它有助于约束直接绑定的规范表示，但不是普适 Ontology 公理，也不能证明这些 Concept 实际同一身份。

本方案将其改称“Concept 直接物理绑定”，只放在 Concept 契约中，不单列一个含糊的大类。默认保留现行政策以保持设计/代码一致；如果 review 决定允许多个不同 Concept 直接绑定同一字段，应另改 validator、binding_contest、检索/消费者约定并迁移验证，不可只删 Prompt 条款。

它与多种匹配方法完全是两回事：同一个 Concept 可以有多个 targets 匹配组；不同 Concept 的 targets 也可以重合；targets 出现某物理 ID 不要求必有 grounded_on。移除一条竞争直接绑定，不能同时偷偷删除有效 targets 或编造业务关系。

## 2. Ontology 契约怎样重新组织

详细规则见[Ontology 契约](ontology-contract.md)与[Alternatives](alternatives.md)，分三层：

| 层 | 内容 | 谁负责 |
| --- | --- | --- |
| 结构硬约束 | ID/引用/端点类型、物理只读、属性形状、Concept匹配结构、Metric直接输入、metadata合法性等 | controller/validator检查，模型在提交前能读到准确约束 |
| 业务保义 | 指代对象、关系含义/方向/范围、条件、配对、粒度、来源、分支和消费者是否保留 | Writer判断并给出理由，独立Assessment核查实际候选 |
| 条件性质量要求 | 确实需要新建/实质重构时的类型字段、最小完整含义、计算/Rule/Event要求 | 按需write_contract，不对未变已接受节点重做extraction审计 |

**Concept.targets**：外层每个元素是一种完整可能匹配；内层多个物理输入共同完成该匹配。例如学校可由学校代码匹配，也可由学校名称匹配；联合姓名组是另一种完整方法。不能把全部外层组拼成必须同时满足的联合组。相同端点但条件/角色不同也不自动重复；target_expressions随组下标同步，匹配不等于SQL投影或可无条件互换表示。

**Concept.relation_metadata**：仅在Concept.properties内，描述多条真实相邻边的组级含义，如顺序、重叠、互斥或覆盖；不创建独立节点、边或顶层关系组列表。它必须同谓词、同相对方向且分支相容，成员至少两条。改边/合边不能只修ID而丢组级知识。

**Alternatives**：相容匹配方法通常进入普通targets；真实不兼容完整版本才保留alternatives。普通/冲突转换统一用保持同一身份的node_rewrite，完整内容、真实来源单元和关系去向一起处理。不能机械合并正文、广播公共证据、混搭action/reason，也不能用起点alternative_ids表示终点版本。

当前能力边界单列：Concept grounding分支targets检查存在“取并集而非逐适用分支验证”的缺口；同正文仅关系不同的alts也有存储判重限制。设计要求完整保义，不把这些现状包装成已被代码完全保证；实施清单包含对应validator/表示能力review。

## 3. Signal决定具体检查任务

先用Signal/validator/followup定位待检查子图，再由Writer核实。本方案不把所有新增/未覆盖节点补成没有具体目标的全面审计。检测遗漏通过变更影响触发、确定性结构检查和最终gate补齐；Signal检测器的覆盖率本身也需要测试，不能因为没Signal就宣称全图语义正确。

十二个任务模块按“触发与理想状态、核查材料与例子、Writer调整、Assessment核查”组织。前两部分说明模式、风险、健康图和关键材料；最后两部分按角色选择，Writer不会收到Assessment核查或Review输出要求。当前只维护十二个叶子模块，通用保义、读取和恢复规则各集中说明一次。

| 模块 | 主要检查对象 |
| --- | --- |
| abstract_predicate | 抽象谓词被当作实际关系 |
| binding_contest | 同物理端点多Concept直接绑定 |
| parallel_assertions | 相同端点间多个可能同义或不同义的断言 |
| triangle | 直接关系与两步路径是否存在有据冗余 |
| same_role_neighbors | 多个同谓词邻居的角色/组级含义 |
| related_decisions | 作用对象相关的Rules是否相容、重复或需区分 |
| source_selection_variants | 不同来源选择不同表示/输入的方法 |
| value_concepts | 同列不同值类别的定义/层级/业务联系 |
| metric_direct_inputs | Metric公式与直接输入不一致 |
| concept_metadata_repair | Concept内关系分组引用或组级语义损坏 |
| target_alternative_scope | 关系原目标变成冲突版本，需保留目标版本含义 |
| node_relation_followup | 节点阶段移交的明确关系保义义务 |

Signal是疑似时允许keep；已核实错误不能用一句“组织不同”绕过。新的身份判断交回Identity，关系Writer不重新做未经授权的合并。

## 4. 局部子图默认“粗节点、详关系”

详见[子图选择](selection.md)。初始节点只给ID/类型/名称/简短含义/分支目录与可读字段，不默认展开全部targets、长Rule、SQL plans。已选择关系给完整端点、谓词、properties、分支scope和evidence IDs。涉及当前边的metadata直接保留在对应Concept中。

缺少哪个前提就read_nodes哪个精确字段；准备替换字段要读完整before值，完整node_rewrite要读完整内容/全部相关alts/来源单元。request_context只补必要关系、准确目录或写入契约。原Question/Gold SQL按evidence IDs读，不初始全量注入。controller维护coverage和必要影响闭包，节点摘要未显示不证明字段不存在。

这份视图会演进，但不累积副本。同对象/view/snapshot/字段只一份；读取完整content后替换分散字段投影；before/candidate不同值不误并。来源正文可去重，但ID及各断言支持关系保留。

## 5. 文件式canonical草稿

草稿是一份相对固定before的当前修改文档：

```text
固定基线 B + 当前草稿 D_v → 实际候选 C_v
                                     ↓
                          完整结构验证 → 独立Assessment
```

模型使用edit_draft按change_id编辑，controller管理version；submit_draft只提交expected_version，不再复述整份Patch。撤回草稿项与从graph退役对象是不同操作。

八类修改为edge_update/edge_create/edge_merge/retire/node_update/node_create/node_rewrite/relation_declare；完整payload和字段限制见[接口契约](contracts.md)。新协议不是给旧MergePatch换名字：它需要canonical editor、严格版本与新单owner重写adapter。不能把新增字段直接塞进当前旧工具。

| 草稿阶段 | 允许的状态 | 能否提交graph |
| --- | --- | --- |
| editing | operation结构/权限正确，跨项语义可暂不完整，Signal可pending | 否 |
| validating | 固定版本重新编译；报完整结构错误，草稿保留 | 否 |
| assessing | 实际候选合法，独立读取/评估该版本及影响 | 否 |
| revising | 反馈当前缺陷与pending_recheck旧问题；编辑后再验 | 否 |
| deferred | 缺前提/身份问题/表示能力不足，保留未闭合任务 | 否 |
| accepted/committed | 所有任务义务与旧问题复核通过，原子提交/持久化 | 局部工作区接受；最终发布另有全图gate |

工具参数损坏、版本过期或越权时整批编辑不执行；没有半写入。成功edit不等于候选合法，代码合法也不等于语义接受。

## 6. Writer与Assessment各自看什么

Writer动态看到局部视图、当前canonical draft和版本反馈，按本轮Signal决定keep/repair/needs_*。它不直接提交图，不调用Review，不按当前偏好删历史知识。

Assessment看到实际候选受影响关系、必要before对比、Signals和Writer理由；当前草稿解释意图，不能替代candidate事实。no-op也逐项评估。旧问题通过prior_issue_reviews明确resolved/still_open/needs_*，不能因新版本或省略问题就清掉。新回归没有原Signal时也可在实际affected closure内提出，不能泛审无关图。

两角色初始都只有read_nodes/read_evidence/request_context，Writer另外edit_draft/submit_draft，Assessment另外Review。精确工具schema全部导出，需写节点时另取得当前write_contract；不向任意文件路径写入，不开放整图搜索。

## 7. 上下文固定六条，内容按状态更新

详见[上下文与恢复](context-and-recovery.md)：

1. System：共用语义 → 当前角色 → 该角色工具/输出契约 → 本task Signal的共用依据与当前角色指导。
2. immutable_task：固定任务、before、Signal、范围规则、契约引用；不重复图。
3. working_graph_view：粗节点/详边与按需字段，明确before/candidate和批准范围。
4. evidence_view：原文正文去重与IDs映射。
5. canonical_draft：当前version/changes/decisions。
6. feedback：当前机械错误、语义问题状态、版本绑定、必要读回执及恢复模式。

不保留AI/Tool历史消息对；原始调用全文独立审计。六条是消息布局，不是恒定token承诺：当前草稿和必要前提仍可能变大。无read_draft分页契约时不能静默省略当前草稿；先按独立义务分任务，必要时另review显式分页/引用协议。

前两条稳定；读取变化更新working view，编辑变化更新draft/feedback。必须承认这些位置之后的缓存后缀可能失效。共用prefix和同角色工具schema稳定，Signal模块固定顺序位于尾部，不向schema动态枚举任务IDs。报告字符数不是tokens，也没有新供应商命中实测。

## 8. 错误、恢复与保留已完成任务

机械错误保留code/path/change/object/branch/expected/actual，不被模糊描述覆盖。当前候选/hash、draft版本、facts版本、契约policy由controller绑定；模型不造内部hash。旧候选事实明确标历史，不作为当前断言。

损坏JSON按result_only_recovery.target_tool恢复该次原参数：坏edit不能跳成submit，坏submit不重发全文，Assessment只恢复Review。恢复轮无读取工具。版本过期不盲目重放；网络重试不计新决策。

无进展基于相同实际决策/候选、相关可见事实与策略，而非version/空格/工具ID；新读相关事实只在真正进入下一请求后才改变判断。读取也有预算与前提目标，不允许无限变换查询来耗费轮次。

checkpoint必须保存fixed before、canonical draft、已编译候选、sources/aliases、working/evidence views、旧问题状态、协议计数、提交/Review绑定。恢复已有效草稿只重编译校验，不重跑Writer；同candidate/facts/policy的已接受Assessment可复用。新政策需明确migration，把旧评估作为历史，不伪装成当前政策结论。commit与任务完成标记/待办闭合同事务，防止提交后中断重跑。

## 9. 当前与拟议实现的差距

| 当前实现 | 拟议调整 |
| --- | --- |
| 可能无Signal也把新增节点补成全面检查 | 统一有明确检查目标的trigger；扩充变更影响/最终gate检测 |
| Signal投影丢原始差异，只剩kind/IDs | 保留完整observed及检查目标/coverage |
| 初始node focus全部properties、原evidence注入 | 节点摘要、关系详细、节点/evidence按需 |
| 宽inspect_graph与通用读取工具 | 三个受控、精确、可审计读取接口 |
| TaskDialogue按args+result保留AI/Tool对 | canonical view/evidence合并，固定六条上下文 |
| submit_patch重复完整当前修改集合 | 按稳定change_id编辑，提交只版本引用 |
| conflicts/resolutions无法一次合成新普通内容 | trusted单owner node_rewrite支持完整转换，保留来源/关系 |
| Review只issues，旧问题消失需人工解释 | 逐Signal、逐旧issue明确复核及关闭 |
| 代码部分scope/分支约束不足或消息误导 | 与Prompt/schema同表核对、补逐分支/影响闭包验证 |

任何实施都应先保存当前代码/记录，另建隔离runtime与测试输出；不直接把生产checkpoint按新格式解释。迁移先验证来源/固定基线和已完成history，保留已接受任务，拒绝不兼容active草稿的静默转换。

## 10. Review后建议的实施和小测试顺序

1. 先冻结schema、binding政策和八类Signal任务契约，清理重复/隐藏约束；以本文可执行schema校验正反例。
2. 实现局部选择、覆盖声明、精确读权限及详情合并；验证metadata不成为独立实体、alternatives不被摘要误删。
3. 实现canonical editor的原子版本/撤回与提交适配，单owner完整重写、实际候选/来源/影响门禁。
4. 接入独立Review逐Signal/no-op/旧issue闭合；新回归、scope/分支和来源精确反馈。
5. 实现六条上下文、恢复与缓存布局，旧checkpoint隔离迁移；不重跑已完成Writer/Assessment。
6. 删除该角色的旧结果工具注册、整份Patch对话及专用兼容包装；内部编译/节点适配迁完使用者再清理，更新实现指纹并验证checkpoint覆盖不误失效。
7. 只取既有case graphs小样本，覆盖多targets、合法平行边、owner竞争、真假三角形、conflict普通化、起点/终点分支、metadata边退役、旧issue复核和中断恢复；通过后再讨论后台迁移，不先全量跑库。

本轮产物是可完整复阅的方案和中文Prompt。离线示例/shape校验通过仅证明设计字段一致，不证明尚未实施的新runtime或真实模型表现已通过。

当前精简版本与旧实现删除边界见[本轮Prompt精简](prompt-simplification.md)。

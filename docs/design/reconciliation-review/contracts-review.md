# Reconciliation 两个 Agent：真实输入、输出与代码契约

本页记录当前代码实际提供和实际校验的内容，供 review；没有修改 Prompt、运行行为或 checkpoint，也没有调用 API。这里的 Writer 是 `phase=reconcile`，独立 Assessment 是 `phase=graph_assess`。两者仍使用通用关系工具和 `MergePatch` / `Review`，与新节点阶段的单 group、只读 evidence、窄输出不同。

源码链接按本次阅读的当前工作区行号定位；后续代码修改可能使行号移动。

## 1. 三种入口：相同角色，不同义务和 before

| 入口 | 谁发起、任务 ID | before 是什么 | Writer 的附加材料 | Assessment / 完成条件 |
| --- | --- | --- | --- | --- |
| 普通 structure 协调 | `_reconcile()` 根据新/受影响语义节点、结构检查与覆盖记录分批；`structure-{cycle}-{index}` | 当前任务开始时已经接受的 workspace graph，固定不变 | `signals` 仅保留 kind、当前 node_ids/edge_ids；`feedback` 可以为空 | 实际有变化时独立 GraphAssessment；普通无变化、无反馈、无身份义务的事务可以跳过评估。已审查区域用内容 signature 避免重跑。 |
| 节点通过后的 followup | 持久队列逐项生成 `node-followup-{id前20位}` | 处理该待办时的当前 workspace；原边另存于待办，不伪装成当前图 | `feedback` 中的 `kind=node_relation_followup`，含原关系、原→当前映射、problem、required_outcome | 即使 Writer no-op，仍必须独立 GraphAssessment。事务 commit 后才从队列删除；若两步之间中断，用 history 防重放。 |
| 节点编译前的 coupled repair | 窄节点编译发现 Metric 依赖、metadata 或入边目标版本问题；`原node任务-node-relations-{node draft前12位}` | **尚未接受的节点整合中间态**；外层已接受 graph 不改变 | 顶层 `node_relation_obligations`；包含诊断、原关系/目标版本、原→当前映射。这里没有节点 identity group 义务 | 先严格编译关系 Patch，再独立 GraphAssessment；通过后才回到外层节点 Assessment。保存 Patch/Review/hash；恢复不重跑已完成关系调用。非 accept 保留事务并停止。 |

定位：[普通调度 incremental.py:487](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/incremental.py:487)、[followup 调度 relation_followups.py:30](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_followups.py:30)、[coupled repair node_relation_repair.py:43](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/node_relation_repair.py:43)。

**signals 和 obligations 不能混为一谈。** signals 是定位线索，不预先要求“必须合并/删除/泛化”。followup 与 `node_relation_obligations` 是必须闭合的保义目标；Writer no-op 并不能自行证明已闭合，Assessment 必须判断原要求是否成立。当前共享 Prompt 已明确这一点：[incremental_relation_shared.md:110](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/llm/prompts/incremental_relation_shared.md:110)。

## 2. 初始图材料怎样选取

两角色都通过 `relation_context()` 构造 `view=light_relations_v1`，不是整张 graph 的序列化，也不保证局部图包含全部可能关系。模型可以按真实 ID 继续读取指定快照。

### Writer 的选取

1. `task.targets` 中的当前节点成为 roots，当前边直接加入 chosen。
2. 加入与 roots 相邻、两端都是语义节点的边。`applies_to` / `depends_on` 只自动加入从 root 出发的边，避免把全部入向消费者展开。
3. 若选中节点的 `relation_metadata` 或已选边涉及一个关系组，补齐该组现存边，并把 metadata owner 纳入 roots；重复至闭合。
4. 加入已选节点的物理引用（targets、表达式 refs 等），以及指向这些物理端点的 Concept grounding owner 边。由此可展示真实的规范 owner，不把 targets 当成必须生成 grounding 的清单。
5. 所选边的两端进入节点集合；物理节点仅展示 ID、类型、表/列/值及 grounding owner 提示，不发送样本值或完整 catalog metadata。

### Assessment 的选取

先按 `delta(before,candidate)` 选取新增/修改节点、变化边及其前后端点，再加入语义影响目标。额外保留：受影响物理端点的其他 grounding owner、受影响 Metric 的全部现存直接依赖。这样删除一条错误边时，评估器能看到正确后继。

对于 mandatory followup / coupled repair，**即使 delta 为空也保留任务 targets 与必要邻接**。这是 no-op 仍可独立评估的输入保证；coupled helper 为 review_task 加 `feedback=[{"kind":"node_relation_obligations"}]` marker。

选取逻辑定位：[relation_context.py:65](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:65)、[mandatory 分支 :104](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:104)、[metadata 与物理闭包 :119](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:119)。

### “完整”实际指什么

| 对象 | 当前默认展示 | 不能据此推断 |
| --- | --- | --- |
| focus 语义节点 | ID、type、name、aliases、全部 properties；非重复 description；若 conflict 列 branch IDs / conflict_points | 不是完整 NodeContent：tags 等不在 node_view。普通节点也未默认附 evidence_ids。 |
| 邻居语义节点 | ID、type、name、aliases、description；properties 仅 definition/action/condition/relation_metadata | 摘要未出现 expression、reason、targets 等，不表示这些字段不存在。 |
| 当前 conflict focus | `current_conflict_views.alternatives[].resolved_content` 为完整解析 NodeContent，另附分支 evidence IDs、当前适用出边 IDs | owner 存储层 null/占位名称不表示完整分支缺字段。 |
| 当前 conflict 邻居 | 每分支只给 semantic_summary：名称、别名、description 及少量关系相关 properties | 不是完整公式/理由；必要时读取 `alternatives`。 |
| before_nodes 的 conflict | node_view(full=True) 保留 owner properties、branch IDs、conflict_points | 这里不自动展开完整旧 alternatives；需要从 before 读取。 |
| 已选边 | ID、谓词、真实起终点、全部 properties、evidence IDs | 其他未选边可能仍存在；不能以局部缺边证明整图不存在。 |
| 关系契约 | 已选谓词的 ID、description、允许两端类型 | 初始摘要不含所有 fields/direction/evidence 细则；可读 contracts。 |

定位：[node_view relation_context.py:16](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:16)、[完整/摘要分支 :176](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:176)、[before 投影 :199](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:199)。

## 3. 输入字段总表

### 两角色共用的 `light_relations_v1` 字段

| 字段 | 实际值/结构 | 含义 |
| --- | --- | --- |
| view | `light_relations_v1` | 本地关系展示格式；不是 graph snapshot 名称 |
| phase / task_id / revision | 角色、任务 ID、workspace_revision（未设置时 0） | 控制信息；revision 不是候选内容 hash |
| presentation | focus properties 完整、邻居摘要、可精确补读的说明 | 完整性限于已声明部分 |
| nodes | 上述 focus / 邻居 / physical 行 | 局部节点集合 |
| edges | `{id,from_node_id,edge_type,to_node_id,properties,evidence_ids}` | 已选关系的完整断言字段 |
| contracts | `{id,description,from_node_types,to_node_types}` 列表 | 已选谓词摘要 |
| current_conflict_views | 每 owner 的 node_id、可选 common_evidence_ids、alternatives；每分支 alternative_id/resolved_content/evidence_ids/active_outgoing_edge_ids | 当前分支语义及起点出边范围 |
| conflict_record_semantics | 有 conflict 时附加字符串 | 解释共同字段/占位字段与解析分支的区别 |
| evidence | `{id,content}`，内容相同时可用 `same_content_as`，同正文多个 ID 可附 evidence_ids | 本阶段初始会注入选中来源的原始 Question/Gold SQL/evidence；不同于节点阶段只给 ID |
| feedback | task.feedback | 普通修复反馈或 mandatory 待办；不是第二份 graph |
| allowed_inference | 当前固定为同范围、分支相容的 is_a 传递 | 不授权根据任意路径自行发明推导 |

### 共用行对象的字段

| 行对象 | 字段 |
| --- | --- |
| nodes[] 语义行 | id、node_type、name、aliases；按上述规则附 properties、description、alternative_ids、conflict_points、common_evidence_ids |
| nodes[] 物理行 | id、node_type、name；table 节点附 table，column 节点附 table/column，dimension_value 附 table/column/value；有直接 Concept owner 时附 owners=[{id,edge_id,alternative_ids?}] |
| actual_changes | nodes 与 edges 两项，每项均为 created/updated/deleted 的 ID 列表 |
| source_unit_manifest[] | owner_id、member_ids、required_operation、source_units；每个 unit 为 unit_id/node_id/kind，以及旧 alternative 单元的 alternative_id。kind 为 normal_content/alternative/common；字段看似共同不自动生成 common 单元 |

### Writer 独有/常见追加字段

| 字段 | 结构/何时出现 |
| --- | --- |
| scope | `{targets:[当前task目标],writable_ids:[展示出来的局部目标与已选边]}`；真实可写性仍以 ProposalScope/编译器为准 |
| signals | 每项 `{kind,node_ids,edge_ids}`，来自当前仍可定位的 task.checks；不保留完整原始 signal 细节 |
| value_domains | `{column_id,column,value_concepts,column_concepts}`；仅多个值 Concept 的物理 owner 提示 |
| task | 普通 run_task 追加 `{id,phase,base:"before",draft_semantics:"complete-replacement",stage}`；有候选时加 candidate_draft_id/candidate_matches_draft |
| identity_plan | 普通 run_task 追加；关系事务通常为 `[]`，不能自行建立身份整合计划 |
| source_unit_manifest | 普通 run_task 对目标中的已有语义 owner 追加；必要耦合 owner 可用工具补读 |
| draft / draft_id | 有当前草稿时出现；这是尚未应用的完整操作草稿 |
| issues / issues_draft_id | 绑定当前草稿的编译诊断；不能与其他草稿的旧错误混用 |
| semantic_issues / semantic_issue_basis | 仍有效的独立评估问题及其旧候选事实绑定 |

普通 Writer 追加字段定位：[task_runner.py:247](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/task_runner.py:247)。coupled helper 直接构造关系 payload，不经过该整段普通 run_task envelope；其恢复信息是独立 `coupled_relation` record，不能假定两入口初轮都有相同 task/source manifest。

### Assessment 独有/常见追加字段

| 字段 | 结构/用途 |
| --- | --- |
| actual_changes | `delta(before,candidate)`，含节点和边的新增、更新、删除 ID | 真实编译差异，不采用 Writer 自述 |
| before_nodes | 更新/删除节点的合并前投影 | 供比较旧含义；旧分支完整内容需按需读 |
| before_edges | 更新/删除边的合并前完整关系行 | 供检查删除、改向与范围变化 |
| new_contracts | 新增/改变的完整 graph relation_contracts 对象 | 供独立审核新谓词定义 |
| required_context | 普通流程按 needs_context 请求补入的读取结果，带请求快照和候选绑定 | 不把历史 candidate 伪装成当前候选 |
| semantic_issues / semantic_issue_basis | 普通复评时附加 | 检查当前候选是否修复；过期候选问题不应重新当当前事实 |
| node_relation_obligations | 仅 coupled repair 顶层附加 | 以下表定义的必须闭合义务；Writer 与 Assessment 收到同一份保义目标 |

### 两种 mandatory envelope 的完整字段

| envelope | 实际字段 |
| --- | --- |
| `feedback[]` 中 `node_relation_followup` | kind、followup_id、source_task_id、source_candidate_hash、original_edge_ids、original_edges、original_to_current、problem、required_outcome |
| 顶层 `node_relation_obligations` | owner_ids、diagnostics、relation_followups、original_targets、target_owner_map、original_edges、edge_aliases、instruction |
| obligations.diagnostics | 按问题类型携 owner_id、alternative_id、missing/stale 依赖，或 metadata problem；目标 conflict 携 original_edge_id/before_target_id/target_owner_id/required_outcome |
| obligations.original_targets | 去重的原目标完整普通 content 或完整 alternatives/conflict_points，附可获得的 evidence IDs；当前目标完整分支已在 focus，不再重复发送 |
| obligations.relation_followups | `{edge_ids,problem,required_outcome}`；edge_ids 是原节点事务 before 中的稳定原边，不猜当前 ID |

这两种结构是**输入义务**，不是关系 Agent 的新增输出字段。关系输出仍是通用 MergePatch/Review，不发送节点阶段的 `relation_followups` sidecar。

## 4. 工具：两角色都能读图，只有 Writer 的结果会编译为写操作

| 工具 | 完整公开参数 | 当前权限/限制 |
| --- | --- | --- |
| inspect_graph | mode=`items/relations/consumers/contracts`；ids?、fields?、view=`input/before/candidate`、offset=0、limit=30、direction=`incoming/outgoing/both`、edge_types? | items/relations/consumers 必须有真实 ID；每页 limit 1–100。`content` 读节点内容，`alternatives` 读完整解析分支；精确字段如 properties.expression。不接受 `*`、整个 properties、内部 sources；分页不完整不能证明不存在。 |
| lookup_catalog | table、columns?、view、offset、limit | 按精确表名/ID定位表列 ID、当前 owner，不能执行 SQL；相同分页限制 |
| read_evidence | ids | 原始 Question/Gold SQL/evidence；关系阶段调用 refresh=True，可重新取已知正文，不等于新增写权限 |
| read_guidance | topic=`concept/metric/generation_rule/event/operations/conflicts/relations` | 按需读取已有指导；conflicts 返回当前 conflict_contract |
| read_source_units | owner_ids | 从固定 before 返回真实 source unit；计划成员返回整个计划组，独立 owner 返回自身单元；不授权改无关对象 |
| inspect_draft | owner_ids?、sections? | 当前草稿/错误及 draft/version 绑定；可投影 source_routes/content/alternatives/conflict_points 等部分；投影不能当完整替换草稿 |
| submit_patch | MergePatch 全部公开字段 | 仅 Writer；结果调用是该回复唯一调用；编译候选，不直接提交最终图 |
| Review | verdict/issues（下文） | 仅独立 Assessment；不能调用它来执行 graph 修改 |

参数与描述定位：[task_tools.py:13](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/llm/task_tools.py:13)、[分页/字段校验 relation_reads.py:27](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/llm/relation_reads.py:27)。

**读取不是写授权。** batch 初始只允许 task targets 及其关联边；ProposalScope 对明确命名、与本地任务必要耦合的后继/依赖/metadata owner 才能扩展。所需对象没展示时，返回 `ScopeContextRequired` 和确切缺失字段，模型补齐后提交；无关对象直接拒绝。物理 catalog 节点与边只读，新业务边必须涉及局部 target 或本次新节点。`scope.writable_ids` 是展示，不是允许编辑全图的凭证。[ProposalScope:43](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/proposal_scope.py:43)、[batch.py:400](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/batch.py:400)。

## 5. Writer 输出：全部 MergePatch 字段

外层禁止未知字段，参数平铺，不包 `patch/arguments`，不输出完整 graph、内部 sources、overrides 或账本。每次草稿**整体替换操作集合**，不再列出的旧操作被撤回；但一次 node_update/edge_update 内省略字段保留固定 before 值。properties 按键更新，每个给出的键值整体替换；不是无限递归 merge。

| 顶层字段 | 类型/默认 | 当前含义 |
| --- | --- | --- |
| explanation | 必填非空字符串 | 当前完整草稿的简短解释；合法 no-op 仍需此字段 |
| failure | 可选 string/null；默认 null | 具体不可完成原因；不能用字符串 null/none/空串冒充成功。真实 failure 终止任务，不当普通格式错误循环 |
| node_updates | 列表，默认 [] | 已有语义节点的必要局部调整 |
| edge_updates | 列表，默认 [] | 已有边局部修改、改向、分支范围或追加证据 |
| create_nodes | 列表，默认 [] | 新语义节点，必须有已知 evidence IDs；不能创建物理节点 |
| create_edges | 列表，默认 [] | 新业务边，必须有完整属性、合法当前端点与证据 |
| retirements | 列表，默认 [] | before→after 的明确语义后继/有据弃用；执行必要删除并记录来源去向 |
| edge_merges | 列表，默认 [] | 声明同一事实的边吸收到既有 survivor，保留来源；不兼容分支/角色不能仅凭端点相同合并 |
| conflicts | 列表，默认 [] | 为一个 owner 创建/重写至少两个完整 alternatives 与来源路由 |
| resolutions | 列表，默认 [] | 只选一个**已有** alternative；为每个其他分支提供理由，不合成新组合 |
| relation_contracts | 列表，默认 [] | 登记新的具体业务谓词；不重复重发已安装谓词定义 |
| integrate_groups | 列表，默认 [] | 继承通用 MergePatch schema；只允许实际计划的完整身份组。普通关系任务和 coupled repair 的计划为空，不能自行用它重新做 Identity |

定义：[RelationPatch relation_edits.py:93](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_edits.py:93)、[MergePatch patch.py:45](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/patch.py:45)。

### 每种操作项的完整字段

| 操作项 | 字段 |
| --- | --- |
| node_updates[] | **id、explanation**；可选 name、aliases、description、tags、add_evidence=[]、properties={} |
| edge_updates[] | **id**；可选 edge_type、from_node_id、to_node_id、explanation、add_evidence=[]、properties={} |
| create_nodes[] | **id、node_type、name、description、properties、evidence_ids**；aliases=[]。注意公开 NewNode 没有 tags 参数，不能照搬完整 NodeContent 增加 tags |
| create_edges[] | **id、edge_type、from_node_id、to_node_id、properties、evidence_ids** |
| retirements[] | **before** 非空 Element 列表、**explanation** 非空；after=[] 或真实后继 Element 列表 |
| Element | **kind=node/edge、id**；schema 另有 alternative_id?，但 retirements 实际拒绝分支级 alternative_id，应使用 conflicts/resolutions |
| edge_merges[] | **source_ids**（至少一个 graph 边 ID，不是 evidence ID）、**survivor_id**（既有边）、**explanation**；无 properties 字段，改说明用 edge_updates |
| conflicts[] | **node_id、alternatives**（至少2个）、**conflict_points**（至少1项）；common_evidence_ids=[]、source_routes=[] |
| alternatives[] | **alternative_id、content**；evidence_ids=[]。content 必须完整 name/description/aliases/tags/properties，不是存储层 overrides |
| conflict_points[] | **point_id、aspect、description、choices**；choices 至少2项，每项 **alternative_ids**（至少1个真实结果分支）与 **statement** |
| source_routes[] | **source_unit_id、destination、explanation**；真实单元恰好一次。destination.kind=result/alternatives/common/discard；只有 alternatives 携非空、无重复 alternative_ids |
| resolutions[] | **node_id、keep_alternative_id、discard_reasons**（其余所有已有分支ID→非空具体理由） |
| relation_contracts[] | **id、name、description、from_node_types、to_node_types、direction_semantics、evidence_requirement**；extends=null 或既有父谓词 |
| integrate_groups[] | **owner_id、member_ids**（至少2且包含owner）、**explanation**；content 与 alternatives 二选一；source_routes=[]、common_evidence_ids=[]、conflict_points=[]；仍须与 controller 计划完整一致 |

定义定位：[relation_edits.py:21](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_edits.py:21)、[EdgeMerge batch.py:46](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/batch.py:46)、[resolution :8](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/conflict_resolution.py:8)。

### Schema 之外还会执行的校验

- 每个 owner 只能走一个完整操作；不能同时 conflicts/resolutions/integrate_groups，也不能再对同一 owner node_update/retire。已有 owner 的 conflict 由保义编译器翻译为固定 before 的单 owner 整合；来源单元必须真实、无漏无重。
- normal/common 单元不能 discard；只允许有据处置旧 alternative。普通结果用 result，冲突结果用 common/指定 alternatives；目的分支必须存在。
- 新 conflict owner 没有历史 source unit，不能带 source_routes，每个新分支须自有已知 evidence_ids；已有分支还可由真实来源路由支持。common_evidence_ids 本身不等于对任意分支独有断言的支持。
- node/edge ID 必须存在或为合法创建；不能改物理 catalog、不能偷偷改变节点类型、不能写未知 properties。新边 properties.description 必须有效；relation_metadata 只属于 Concept，且引用同谓词、同相对方向、分支可共同成立的至少两条真实相邻边。
- 单边明确后继 retirement 会保留对应精确来源；多对多重构不机械广播来源。来源/lineage 还会检查消失对象的处置与结果知识覆盖。
- `properties.alternative_ids` 只限定**起点 owner** 的完整分支；不是终点版本选择器。目标由普通节点变成/改写 conflict 时，controller 生成 mandatory 协调，给出旧完整目标与新目标映射；不能把旧含义扩散到全部目标版本。
- 当前通用 batch 编译会做 GraphState 形状/语义属性校验；**普通关系局部编译不是 GraphManager 全部最终约束的替代品**。coupled helper 显式执行完整 GraphManager.validate_state；最终 publication 也执行完整校验（如 Metric 逐分支 refs 对应的精确直接依赖、Concept owner 竞争等）。独立 Assessment 负责保义判断，最终 graph 仍有发布 gate。

定位：[操作边界 patch.py:168](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/patch.py:168)、[来源单元 source_units.py:80](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/source_units.py:80)、[retirement relation_edits.py:200](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_edits.py:200)、[目标 conflict node_relations.py:170](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/node_relations.py:170)、[发布 gate incremental.py:588](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/incremental.py:588)。

## 6. 独立 Assessment 输出：完整 Review 契约

| 字段 | 允许值与校验 |
| --- | --- |
| verdict | accept / revise / needs_context / revisit_identity |
| issues | 默认 []；accept 当且仅当 issues 为空，其他结论至少一个 issue |
| issues[].refs | 至少一个 ReviewReference，指出当前问题对象 |
| issues[].problem | 非空问题描述 |
| issues[].basis | ReviewReference 列表；revise / revisit_identity 每个 issue 都必须非空，不能仅写偏好 |
| issues[].required_outcome | 非空保义修复目标，不要求唯一组织/措辞 |
| issues[].claim | 可选 AbsenceClaim；用于可被权威快照核验的关系不存在断言，不是任意自然语言声明 |
| ReviewReference | kind=node/edge/contract、id、view=input/before/candidate（默认candidate）、fields=[]。fields 是真实对象路径；不用展示 envelope 的 content.*，不加 ref.alternative_id，不用 alternatives[0] 之类数组路径；分支问题可引用 owner.alternatives，在文字定位分支 ID |
| AbsenceClaim | kind=relation_absent/concept_owner_absent；edge_type、to_node_id 必填；from_node_id?、alternative_id?、view=input/before/candidate。选 branch 必须同时给来源 owner；concept_owner_absent 只能 grounded_on，且不能指定 from_node_id/alternative_id |

`candidate_hash` 是代码填入的候选绑定字段，不在公开结果 schema 中，不让模型管理。判定不会执行修复。refs/basis 的 ID、视图和字段必须真实；全局缺边 claim 若被权威图反例推翻，代码拒绝该 Review 并返回 counterexamples，而不是命令 Writer 添加已存在关系。

定位：[Review review.py:12](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/review.py:12)、[validate_review :122](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/review.py:122)。

普通流程中，needs_context 的精确 refs 会由 controller 补读后复评；重复或不可获得的上下文会保留候选并停止。coupled repair 当前对任何非 accept 都保存 Review 后停止，没有同样的自动补读/修订外循环。两入口不可混写成完全相同的恢复策略。

## 7. 真实 Metric 小例子：关系退休不是身份 alias

以下来自真实 `toxicology/266 + 204` 节点整合测试的已接受关系 Patch，原样保留字段与解释。节点合并后的 COUNT refs 指向 case266 的 carcinogenic_molecule，仍继承了两条原 case 的直接依赖。关系 Writer 将不再对应公式操作数的 case204 边明确退休，保义后继指向 case266 既有边；独立 GraphAssessment 已 accept。

此例只证明关系编译、来源/lineage 和该独立关系 Review 的结果；节点内容是否通过节点 Assessment 是另一道门，不能仅凭关系 accept 推定。

```json
{
  "retirements": [
    {
      "before": [
        {
          "kind": "edge",
          "id": "case204::edge.carcinogenic_count_depends_on_carcinogenic_molecule"
        }
      ],
      "after": [
        {
          "kind": "edge",
          "id": "case266::edge.carcinogenic_count_depends"
        }
      ],
      "explanation": "The metric's expression.refs.carcinogenic_molecule resolves to case266::concept.carcinogenic_molecule, so depends_on must target that concept. The edge to case204::concept.carcinogenic_molecule references a concept that is not the formula operand and is stale; the existing case266::edge.carcinogenic_count_depends already expresses the correct direct-operand relationship with compatible meaning."
    }
  ],
  "explanation": "The metric case266::metric.carcinogenic_molecule_count has expression.refs.carcinogenic_molecule = case266::concept.carcinogenic_molecule, so its direct depends_on operand must point to that concept. The existing edge case266::edge.carcinogenic_count_depends correctly expresses this direct operand. The edge case204::edge.carcinogenic_count_depends_on_carcinogenic_molecule points to a different concept (case204::concept.carcinogenic_molecule) that is no longer the formula's operand, making it stale per the depends_on contract (must match expression.refs). It is retired with the correct edge as its semantic successor; no node content is changed."
}
```

原始输入回归文件：[node_local_metric_retirement.json](/Users/yichendezaizai/autoonto-nl2sql-release/tests/fixtures/node_local_metric_retirement.json)。这条记录曾暴露一个代码问题：被退休边仍携带 e→e identity alias，lineage 在处理显式 retirement 之前就拒绝“alias 目标已不存在”。当前实现把退休当作 Change 后继，不把它强称为 identity；读取定位仍能从原边找到存活后继。此解释描述既有实现，不在本次文档任务中修改代码。

## 8. 本次只读审查发现的 review 点

| review 点 | 当前代码事实与可能影响 | 建议讨论方向（本次未实施） |
| --- | --- | --- |
| 完整性文案不一致 | focus 的 resolved_content 实际是完整 NodeContent，邻居才是摘要；但 conflict_record_semantics 把它们统称 lightweight semantic summary。[relation_context.py:182](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:182) | 分别说明 focus 完整 / neighbor 摘要，避免额外读取已经完整的分支。 |
| 初始普通节点缺少来源句柄关联 | node_view 不附普通 evidence_ids；初始 evidence 虽有 ID/正文，但未总是直接绑回该节点。 | 决定是否提供紧凑 per-node evidence IDs；无需因此重复证据正文。 |
| 公共来源没有完整纳入初始证据选取 | refs 从节点 sources、alternative.sources、选中边 sources 收集，未显式加入 common_sources；common_evidence_ids 可能因此显示为空。工具从完整 context 仍能按字段补读。[relation_context.py:141](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:141) | 明确公共来源 ID 的默认呈现，不能将初始空列表误当作公共知识没有来源。 |
| 初始 source manifest 在入口间不同 | 普通 run_task 初始给 target owner 的 manifest；coupled helper 直接构造 packet，需必要时 read_source_units。 | Prompt 明确“有则使用，缺少时按 owner 补读”，不要暗示所有入口初轮都已给齐。 |
| Schema 比有效关系操作更宽 | integrate_groups 仍公开但关系任务通常无计划；retirement Element 公开 alternative_id 但编译拒绝；create_nodes 与完整 NodeContent 允许字段不同。 | 审查是否应把硬限制明确集中到关系输出说明，或未来收窄工具 schema；不要仅靠编译报错教模型。 |
| 关系事务与节点完整替换语义并存 | 完整操作集合替换、局部 node/edge 更新按字段继承、conflict content 完整替换，是三个不同层次。 | 明确层级，避免“省略字段继承”被误用于完整 alternatives，或“完整替换”被误用于局部 node_update。 |
| needs_context 恢复入口不同 | 普通关系事务会自动读取精确 refs 后复评；coupled helper 保存非 accept 后直接停。 | review 时分别理解，不把普通流程的自动补读承诺套到 coupled repair。 |
| 局部编译不是最终严格 gate | 通用 batch 使用 validate_graph_state；coupled repair 和最终发布另外运行完整 GraphManager.validate_state。 | 明确每层保证，独立 Assessment 仍需检查直接依赖与保义；不能把“编译通过”当作全部图约束已闭合。 |
| 关系上下文仍可因 hub/metadata 增长 | metadata 闭包、所有相关 grounding owner、mandatory 邻接会扩充任务材料；读取也可触达完整快照。 | 评估必要闭包、按需读取与 token 预算，而不是声称已具备节点阶段同样严格的局部权限/缓存布局。 |

这些是当前实现与文字呈现之间值得复阅的差异，不是本页提出的新运行契约。最终 graph 仍须清空所有关系待办并通过发布校验；本页不宣称已经执行新的关系模型测试。

# 拟议输入、工具、草稿与评估契约

本页是**设计方案**，不代表现有 runtime 已具备这些接口。`review_contracts.py` 只用于导出公开 schema 和离线核对示例，不实现读图授权、真实编译、事务或模型调用。新协议不把旧 MergePatch 全文反复交给模型。

## 1. 不可变任务与可演进视图

不可变任务包含 task_id、固定 before_id、触发来源、完整 Signal/义务、初始锚点、修改范围规则、图契约版本、预算和覆盖要求。Signal 中保留观察事实与字段角色，不能仅剩 kind/IDs。mode=suspect 是待核实模式；mode=required 是已核实结构错误或明确保义义务。

同一任务的多个 Signal 只有存在实际耦合决策时才合组；共享大 hub 不意味着无限扩图。子图选择先按各 guidance 收集必要关系集合，再用**完整输入预算**分组，不以根节点 JSON 代替整个请求大小。缺少必要前提不能通过截断伪装成 complete。

working_graph_view 保存 before 和 candidate 两个当前命名空间，以及按授权snapshot隔离的source归档命名空间及明确 snapshot。节点初始给 id/type/name/一两句含义、分支目录、可读字段目录；它是展示 DTO，不是可直接提交的 GraphNode。关系给完整 id/谓词/端点/properties/来源 IDs。涉及当前边的 relation_metadata 保留在对应 Concept.properties 内；不建 top-level relation_groups，不把组当节点/边实体。

coverage 按查询范围记录：锚点、谓词、方向、适用快照、complete/next_cursor。它表示“该范围已列全”，不是整图完整或写权限。原始 Question/Gold SQL/evidence 默认只有 IDs，在授权读取后放 evidence_view。节点摘要的字符串截短必须带 summary 标记，不作为完整值。

## 2. 公开工具集合

| 角色 | 正常公开工具 |
| --- | --- |
| Writer | read_nodes、read_evidence、request_context、edit_draft、submit_draft |
| Assessment | read_nodes、read_evidence、request_context、Review |

### read_nodes

| 参数 | 约束 |
| --- | --- |
| ids | 非空已授权节点 ID 列表，不接受扫描所有节点 |
| view | before / candidate / source，默认 before；source 是明确授权归档，不是当前活图 |
| fields | 默认 summary；支持真实精确路径及 summary/content/alternatives/source_units 保留选择器。禁止通配符、整个 graph、数组位置路径；完整 properties 仅通过明确 content 获取 |
| draft_version | candidate 必填，精确指定已编译版本；无该版本候选则 NO_COMPILED_CANDIDATE，不回退到旧候选 |
| source_snapshot_id | 仅source必填，精确指定controller发布的归档快照；before/candidate禁止携带。source仅只读，不允许source_units或draft_version |
| alternative_ids | 仅 fields 含 alternatives 时允许。局部读某分支不授权完整重写所有分支 |

返回节点 ID/view/snapshot、精确字段、字段完整性、来源 ID 与最小读回执，由 controller 合并到 working view。content 是完整 name/description/aliases/tags/properties；alternatives 是按 ID 解析的完整内容及证据，隔离内部 overrides；source_units 是固定 before 的真实 manifest。通过字段取值证明存在，不能把未请求字段当缺失。

### read_evidence

仅 ids 非空列表，严格匹配本任务 evidence 白名单。未知或越权 ID 返回具体错误；不返回整个 context 的所有来源。相同正文只保存一份，多个 evidence IDs 各自的归属映射保留，不能因正文相同混合分支支持。

### request_context

| kind | 参数与结果 |
| --- | --- |
| relations | anchor_ids 非空、reason 必填；direction=incoming/outgoing/both，edge_types 可选，cursor 可选；view/draft_version 同上。controller 只扩展与本任务必要耦合的关系，返回完整性/分页和批准范围 |
| catalog | reason、准确 table 定位必填，columns 可选；不执行 SQL、不做自由语义检索。目录也受本任务数据库/授权范围限制 |
| write_contract | node_type、operation_kind、reason 必填；已有节点更新/重写/退役还需 anchor_ids。返回准确字段、隐藏校验、操作前提及必要来源单元；node_rewrite 同时返回单 owner 的真实 source_unit_manifest |

三种请求不能混用另一 kind 的参数。reason 解释需求，但不自动授予访问权；锚点必须来自已展示对象、可信诊断/原始来源定位或本任务新对象，controller 执行必要耦合检查。新增已批准对象写入 working view 的动态 scope，初始不可变任务只记录范围规则。不能靠猜一个真实全局 ID 扩权。

字段未给但确实可读时精确补读；权限不允许或关键前提无法取得时返回 needs_context/deferred。没有隐藏的全图 inspect_graph、任意 shell 或文件系统权限。

**历史来源可达性**：source_selection_variants或节点移交的旧source_node_id可能已不在当前before。controller发布最小source快照目录，仅授权相关原始记录；read_nodes(view=source,source_snapshot_id=...,ids=...,fields=...)可读完整原内容。记录按snapshot绑定，不能当新活节点/写授权/全局缺边证明，真实source units仍只能从固定before取。Review可精确引用source basis；claim仍限before/candidate。

## 3. Canonical draft 是一份受控文档

```json
{
  "task_id": "task-example",
  "before_id": "before-example",
  "version": 0,
  "changes": [],
  "decisions": []
}
```

前三项为 controller 管理的头部，模型不能改 task/before/version。change_id 是当前修改项的稳定文档句柄，和真实 graph node/edge ID 分开；signal_id 来自任务。草稿存的是相对固定 before 的**当前修改计划**，不是累积图和编辑历史。

### edit_draft

参数为 expected_version（非负整数）与非空 operations 数组：

| op | 字段 | 语义 |
| --- | --- | --- |
| put_change | change={change_id,kind,value} | 创建或完整替换该修改项；省略该项旧 payload 的字段是撤回旧指令，不继承旧草稿片段 |
| remove_change | change_id | 撤回该修改计划，恢复基线行为；不等于删图节点/边 |
| put_decision | decision={signal_id,status,reason,change_ids} | 完整替换某触发项的判断 |

同一请求不能重复操作同一 change/decision。顺序不会被为了缓存而排序。controller 先检验版本、工具参数与授权；失败整批不执行，canonical version 不变，原始坏调用与具体反馈保留审计。成功一次原子事务只增一个版本，返回当前 canonical 状态；任何中途异常都不能半写入。

草稿允许在多项编辑之间暂不满足完整图语义或 Signal 覆盖；这与允许损坏工具 JSON、越权或半个 operation 进入 canonical 文档不同。提交时再检查跨项一致性、完整义务和实际候选。

### 八类 change 的准确 payload

| kind | value 字段 | 前提/含义 |
| --- | --- | --- |
| edge_update | id、explanation 必填；edge_type/from_node_id/to_node_id/properties/add_evidence 可选 | 只改现有边；properties 按键替换，不允许内部来源或新造 role/condition/qualifier 属性；必要条件写在合法 description 中 |
| edge_create | id、edge_type、from_node_id、to_node_id、完整 properties、evidence_ids、explanation | evidence 非空；完整 description 和必要 alternative_ids；端点/谓词须已知或本草稿明确创建/声明 |
| edge_merge | source_ids、survivor_id、explanation | source_ids 是要吸收的图边 IDs，非证据 IDs；唯一且不含 survivor；同事实/相容范围由 Agent 证明；survivor 属性另用 edge_update |
| retire | before=[{kind,id},...]、after=[{kind,id},...]、explanation；退役节点还须 node_unit_routes | actual before/语义后继；after=[] 只限有据错误边弃用，节点必须有后继。元素仅 node/edge，不含 alternative_id。替代分支处理走 node_rewrite |
| node_update | id、explanation；name/description/aliases/tags/properties/add_evidence 可选 | 有必要才改普通节点；修改某字段前读取该完整 before 值和依赖。省略继承基线，给定 property 整体替换；name/aliases/tags 不填 null，description 可显式 null |
| node_create | id、node_type、explanation；完整 content 或完整 alternatives 二选一 | content 五字段齐全且 evidence_ids 非空；冲突创建需 >=2完整分支、conflict_points、各分支直接证据，可另有 common_evidence_ids；不得给伪造 source_routes。新 schema 比旧 NewNode 更完整，需新adapter，不假称旧工具支持 tags/冲突创建全部字段 |
| node_rewrite | node_id、explanation、source_routes；完整 content 或 >=2 alternatives；普通结果可 evidence_ids 增补新证据；冲突时 conflict_points 非空，可 common_evidence_ids | 保持一个已有 ID/身份，无 member_ids/identity alias；普通→冲突、冲突→冲突、相容分支→新普通、选择旧分支普通化都用这一路径；按真实来源单元恰好路由一次 |
| relation_declare | id、name、description、from_node_types、to_node_types、direction_semantics、evidence_requirement；extends 可选 | 只登记确有必要的新具体业务谓词；已有谓词不重发改样式；不把 abstract has_attribute 作为实例边 |

**节点退役的 node_unit_routes**：每项含 source_node_id、source_unit_id、非空 successors、explanation；对每个被退役节点的固定before真实unit恰好一次。successors每项为kind/id，可对node后继附实际alternative_id；它们必须属于after声明的真实后继，不能把原知识广播给不支持的后继分支。normal/common及有依据的旧alternative均需保义映射；不允许借whole-node retirement清空知识。确需有据纠正旧分支先用node_rewrite。替代关系及来源ledger由编译/Assessment核验；这也是新adapter的一部分。

可选字段不用则省略；默认未提供不等于显式 null。完整 NodeContent 的 description 可为 null；properties 仍遵守具体类型。每个嵌套 schema 的真实必填项见 JSON 工具定义。

同一已有身份只能用 node_update 或 node_rewrite 中一种内容路径，不能串联重写/选择/退役以规避来源。新节点的全部初始内容在 node_create 一项中给齐，不再同轮 node_update/node_rewrite 同一新 ID。真正身份合并交回 Identity，不公开 integrate_groups。

单 owner 普通化是**新增编译能力**：现有 conflicts 只支持 alternatives，resolutions 只选旧分支；实现时需要保持来源路由/分支出边/消费者的 trusted 单 owner adapter。不能给模型一个假第二 member，不能让它私造内部 IdentityIntegration。

### 跨操作组合矩阵（提交前公开校验）

| 组合 | 是否允许/归一化方式 |
| --- | --- |
| 同一已有node的多个node_update，或node_update+node_rewrite | 不允许；合成一项当前完整修改，避免两个change互相覆盖 |
| node_create+同ID node_update/node_rewrite | 不允许；初始完整内容直接写在node_create |
| 同一edge的多个update，或edge_create+同ID update | 不允许；在单项value中给最终字段 |
| edge_update(survivor)+edge_merge | 允许；survivor必须原有且存活，更新其说明与合并来源分别表达 |
| edge_merge中被吸收edge另有update/merge/retire | 不允许；选择一个完整合并方案，不串联删除/循环吸收 |
| create新后继+retire旧断言 | 允许；before必须原已存在，after在最终候选真实存在；先解析声明/创建，再原子执行退役与来源门禁 |
| 修改真正被退役的同一node/edge | 不允许；不先改后删。若某before对象同时在after中保留，则仅其真正存活部分可有一项必要内容更新 |
| node_rewrite分支+相关边/Concept metadata更新 | 允许且可能必要；完整评估来源路由、起点/终点范围、内容消费者和组级含义 |
| 多条retire覆盖同一个before断言 | 不允许；合为一个明确后继计划，避免来源被分散或循环自证 |
| relation_declare新谓词+创建/更新使用该谓词的边 | 允许；先校验新契约，再在同一候选核验端点和属性，已有契约不重发 |

草稿operations是编辑文档的顺序，不是顺序修改已接受graph。提交编译时先解析完整声明/依赖，再生成一个候选；不能利用中间图状态绕过约束。某项临时编辑导致跨项组合不合法，可以保存并反馈，但submit必须拒绝；界面/Prompt不能把它伪装成已经接受的graph。

## 4. Signal decisions 与 submit_draft

每项 decision 含 signal_id、status、非空 reason、change_ids 列表：

| status | 提交意义 |
| --- | --- |
| pending | 仅编辑过程；正式提交拒绝并保留草稿 |
| keep | 已核实无需调整，change_ids=[]，说明为何当前图有效；suspect 可保留，真实结构错误不能借 keep 绕过 validator |
| repair | 至少引用一项当前 change；每个变更须有任务义务关联，包括必要的耦合更新 |
| needs_context | 缺少无法在当前授权内获得的前提；deferred，不接受 graph |
| needs_identity | 有具体身份反例/需独立 Identity 判断；deferred，不由关系 Writer 自行合并 |

submit_draft 仅含 expected_version，不包含整图、草稿全文或旧 MergePatch 包装。controller 原子固定待评估版本：检查所有任务触发项恰好覆盖、没有悬空/未归属 change、scope/read前提 → 从固定 before 重新编译候选 → 运行完整结构校验 → 独立 Assessment。

needs_* 不会因部分其他 Signal 已修复就整体通过；可以后续在证明任务独立的情况下由controller另建任务，但不能在同一不可分事务中暗自部分提交。合法 no-op 也有 decisions 和独立 Assessment，空 changes 不代表免审。

## 5. Assessment Review 完整格式

| 字段 | 内容 |
| --- | --- |
| expected_version | 当前实际编译候选对应的草稿版本；不重建 candidate hash |
| verdict | accept / revise / needs_context / revisit_identity |
| signal_reviews[] | signal_id、status=satisfied/needs_revision/needs_context/needs_identity、explanation、issue_ids；每个任务触发项恰好一次 |
| prior_issue_reviews[] | issue_id（输入旧问题）、status=resolved/still_open/needs_context/needs_identity、explanation、issue_ids（本次问题引用）；每个 pending_recheck 旧问题恰好一次 |
| issues[] | issue_id、kind=defect/missing_context/identity_counterexample、signal_ids、refs、basis、problem、required_outcome；可选 claim |
| refs / basis 项 | kind=node/edge/contract、id、view=before/candidate/source、fields；source另附source_snapshot_id。不得把归档当存活candidate；不加 alternative_id，不用 envelope/数组位置路径；分支 ID 放说明文字 |
| claim | 可选可核验不存在前提：kind=relation_absent/concept_owner_absent、edge_type、to_node_id、可选 from_node_id/alternative_id、view；由权威快照核验，不允许当作语义修复命令 |

verdict按全部当前issues的种类决定，含signal_ids=[]新回归：identity_counterexample优先revisit_identity，其次missing_context为needs_context，其次defect为revise，无问题才accept。Signal的issue链接必须双向一致，satisfied不能携阻塞issue；旧问题未解决状态也与其当前issue种类一致。

accept 只在所有 Signal satisfied、旧问题 resolved、issues=[] 时成立。其他判定须有具体 issues，revise/revisit_identity 每项有 basis。未解决项必须链接当前实际问题；旧问题被省略不能自动关闭。已 resolved 的问题可关闭，即使另一个问题仍导致 revise。

signal_ids=[] 仅用于本次修改引发的新回归，引用必须落在 controller 计算的 affected closure，不能引入无关审查。完整影响范围（入边、内容消费者、metadata、分支来源）由 controller 根据实际变更补齐；Agent 不凭初始摘要自行声明“无影响”。

Assessment 的实际输入包括 candidate 的受影响边/必要 unchanged context、before 中被替换/删除记录、按需节点详情、全部 Signals、当前草稿与 pending issues。不给出整个世界图，也不能只给 draft 意图。候选中删掉的对象要引用 before，候选当前关系须真实存在。

claim端点也必须先通过本地view授权，不能通过猜全局ID触发权威图探测。claim 若被完整权威图推翻，返回精确反例并并入当前工作视图；不丢弃其他有效问题，不命令 Writer 重复创建已有边。没有可靠 coverage 不作全局“不存在”判断。

## 6. 版本、读取与错误的前置门禁

| 门禁 | 结果 |
| --- | --- |
| expected_version 过期 | VERSION_MISMATCH，整批未执行；返回当前版本和必要状态，不重放旧编辑 |
| 读未知/越权对象 | UNKNOWN_OBJECT / OUT_OF_SCOPE，无新事实或写授权 |
| 未读取欲替换字段完整值 | MISSING_REQUIRED_READ，列 node_id/view/fields；不能靠摘要覆盖未读内容 |
| 未加载必要写契约 | MISSING_WRITE_CONTRACT，指明 node_type/operation_kind |
| 来源句柄覆盖不符 | SOURCE_ROUTES_MISMATCH，给 owner/path/missing/extra/duplicates 与真实所需句柄 |
| 结构编译不合法 | code/change_id/object/path/branch/expected/actual；候选不提交，当前草稿保留 |
| 旧候选或反馈绑定错位 | STALE_CANDIDATE / STALE_FEEDBACK，不将旧事实标为当前 |
| 能力无法表达终点版本或关系条件 | NEEDS_CONTEXT 或显式表示能力阻塞；原始知识和草稿保留，不伪造属性、不默默广播 |

错误格式为拟议统一 envelope：status、draft_version、candidate_for_version（若有）、facts_revision、errors。每条 error 保留 code/path/change_id/object IDs/必要 branch/期望与实际及具体修复目标；不存在的字段可省略，不能用一段模糊文字代替结构错误。

这些门禁都应在实现前与 Prompt、工具 schema、编译器同表核对；本方案没有宣称仅靠 docs-only schema 已实现全部权限或语义验证。

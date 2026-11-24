# 拟议上下文、草稿编辑与恢复协议

本方案仅供 review，尚未替换任何 src、Prompt loader、工具、后台 runtime 或 checkpoint；没有新增 API 调用。公开工具和结果字段以[输入输出契约](contracts.md)为唯一格式来源，本页解释数据生命周期、每轮实际上下文和恢复规则。

## 1. 一份局部工作视图，一份唯一草稿

关系协调不再依靠累加 AI/Tool 对话记住操作。controller 管理以下对象，模型每次只看其当前有效投影：

| 对象 | 含义与边界 |
| --- | --- |
| 固定 before | 本事务的保留基线；只读。每个草稿版本都从它编译，不在上一被拒候选上增量执行。 |
| Signals / 明确义务 | 本轮需独立判断的有限目标。疑点允许 keep；明确保义义务必须闭合，不能被 no-op 自动豁免。 |
| working graph view | 初始粗节点、详关系；按需补读的精确字段并入同一对象记录，不另存一份完整工具返回。 |
| canonical draft | 稳定 change_id 索引的变更和 signal 决策；由 controller 管理版本，具有文件式修改语义。 |
| candidate | 代码从固定 before 和草稿实际编译的不可变候选；Assessment 不能把 Writer 的解释当作这个结果。 |
| feedback | 当前绑定的机械诊断、语义问题、缺失前提、读状态及协议恢复状态；旧问题保留其原绑定。 |

本页 before 是 controller 保存的快照绑定，不是让模型接收整张 before graph；模型只看 working view 中已选择的关系和必要节点字段。

“局部”只描述权限与选择范围，不保证体积恒小。高连接度节点、完整关系组或必要来源可能仍超过预算。届时需收窄独立任务、补充分批计划或明确停止，不以删除知识换取通过。

## 2. 固定六条 messages

| 位置 | 内容 | 什么时候改变 |
| --- | --- | --- |
| 1 System | 共用图契约 → 当前角色 → 该角色工具/输出 → 当前 Signal 的共用依据与角色指导 | 同 task 内固定。不给所有类型/Signal 全套指导。 |
| 2 Human：immutable_task | task / before 绑定、作用域、全部 Signals、明确义务、必要 schema/契约引用及完整性边界；不重复图正文 | 同 task 内固定；保存初始锚点及不变的 scope 规则。新增独立义务由 controller 建立新任务或显式重规划。 |
| 3 Human：working_graph_view | before/candidate 的局部粗节点、详边、必要 metadata，以及已按需读取的精确节点字段；source 命名空间另存已授权归档 snapshot 目录与精确节点记录；Assessment 加真实 affected diff | 新详情、受控 scope 扩展或新的实际 candidate 出现时更新。 |
| 4 Human：evidence_view | 已读来源正文及 ID 映射；同内容只一份，保留所有来源身份 | 首次有效读取新内容时更新；重复读取不再增加一份正文。 |
| 5 Human：canonical_draft | 唯一当前 version、change_id 索引的变更与全部 signal 决策 | 成功 edit 后更新；失败的原子编辑不改变它。 |
| 6 Human：feedback | 当前机械错误、语义 issue 状态与绑定、仍缺前提、最近工具执行回执、控制性恢复模式 | 精确反馈或实际复核结果变化时更新。 |

不在六条之外追加历史 AI/Tool、上一轮完整请求、整份旧 Patch、重复 schema 或过期候选图。原始模型回复、工具参数/返回、编辑日志与编译记录继续完整写入审计记录；“不再发送历史”不等于删除历史文件。

工具执行后的下一次请求仍是这六条消息。controller 消化读取/编辑结果后重新构造工作视图和草稿；两端都不保留旧 AI tool_call 或未配对的 ToolMessage。最近回执仅给成功/失败、实际版本、缺失字段和对象引用；正文放在其唯一记录位置。

第 2 条中的 scope 是不变的任务边界/授权规则与初始锚点，不是每次补读后的可见 ID 清单。已批准的 context 扩展、当前可见对象和局部完整性状态放在第 3 条；这样正常补读不会为了改 scope 而破坏固定任务前缀。与本次修改耦合的新回归 issue 在 feedback 中登记并进入后续 prior_issue_reviews，不靠偷偷改写最初 Signal 列表追认；真正独立的其他问题另建任务。

六条是本拟议渲染布局，不表示每条都可无限大，也不是供应商的 token 计数方式。

## 3. 初始图与按需补读

### 初始粗节点、详关系

- 粗节点提供真实 ID、类型、名称、必要简短含义、分支身份和 evidence IDs，不假装是完整 NodeContent。
- 详关系提供真实 ID、谓词、起终点、完整有效 properties、分支范围和 evidence IDs；关系完整性按明确局部 scope 声明，不能把局部缺边当作全局不存在。
- 涉及的关系组直接附在 Concept.properties.relation_metadata 中，保留完整组及成员关系；不引入独立 RelationGroup 节点。
- conflict 的摘要与完整 alternatives 明确区分。需要判断定义、公式、角色或分支对应时，补读真实完整内容，不能从存储占位值推断缺失知识。
- 默认给来源 ID；原始 Question/Gold SQL/evidence 在确有需要时读取。已读来源不会因 Writer → Assessment 切换而凭空失去可验证映射。

### read_nodes 的合并规则

读取对象以 **view + snapshot/version（source 使用 source_snapshot_id）+ node_id + field path** 标识。相同快照的同一字段只有一个当前值：

1. 初始视图已完整提供的字段不再次出正文；返回“已提供”的记录引用及完整性信息。
2. 新字段并入该对象已有记录；不在输入末尾追加另一份“Tool 返回的节点”。粗摘要与精确字段不并列伪装成两个版本。
3. 多次重叠读取只补未提供的字段；字段值相同但 view/snapshot 不同，保留各自绑定。可以共享相同正文，不能共享“这是哪个版本”的断言。
4. 同一不可变快照的同一字段出现不同返回值，作为 controller 一致性错误停止；不能采用最后一个值覆盖前一个值。
5. 不存在、未提供、读取失败、分页未完成和字段不适用分别报告。缺失值不写成业务 null；不把部分页标为完整。
6. `before`、`candidate`、`source` 的同 ID 不自动代表同内容；candidate 更新后，旧 candidate 字段不继续作为当前值。source 按 source_snapshot_id 分开存放，同 ID 的不同归档快照不能互相覆盖。

`read_nodes` 接受 ids、view、fields；默认 before/summary，候选读取须提供实际已编译的 draft_version。source 读取须提供明确的 source_snapshot_id，只能访问 controller 已授权的原 case 或旧接受快照节点；before/candidate 禁止携带 source_snapshot_id，source 不使用 draft_version。alternative_ids 仅在 fields 包含 alternatives 时可用；请求 ID 来自已展示记录、可信诊断引用或本轮明确的新对象，最终仍受 controller 的授权校验。它不成为新的全图检索工具。需要未提供的关系、消费者、物理端点、谓词约束或额外节点时，通过 `request_context` 提出缺失前提；request_context 仅有 relations/catalog/write_contract 三类，带 anchor_ids 和具体 reason；controller 进行 scope/耦合检查并返回可验证材料或明确缺失原因。relations 按 direction/edge_types/cursor 获取必要关系；catalog 按 table/columns 定位物理端点；write_contract 按 node_type/operation_kind 获取必要写入契约。candidate 请求同样绑定精确已编译版本。提出 reason 不授权扫描整图，也不自动获得新写权限。

详细字段优先沿真实路径读取。需要改某个结构化字段时，先取得该字段的完整原值，而非以一个摘要或局部列表作整字段替换。

来源路由句柄也必须可读取：`read_nodes(ids=[owner], view="before", fields=["source_units"])` 返回该 owner 相对固定 before 的真实 source unit manifest；或通过 `request_context(kind="write_contract", anchor_ids=[owner], node_type=当前类型, operation_kind="node_rewrite")` 同时获取所需写契约和真实来源单元。它们是 controller 元数据，不是新增 NodeContent 字段，也不默认对整个局部图全量注入。source 归档视图不能读取 source_units；历史来源单元不能冒充本事务固定 before 的路由句柄。

### 已退役来源节点的精确归档访问

source_selection_variants 等 Signal 可能指向已在 before 中退役的 source_node_id。此时 controller 不应把 before/candidate 的同 ID 或后继假装成旧记录；应提供授权 source snapshot 的定位和最小目录，再由 read_nodes 精确补读。

例如 Signal 已给出 source_node_id=M-retired、source_snapshot_id=case-snapshot-204，可按下列参数读取其完整原节点：

```json
{"ids":["M-retired"],"view":"source","source_snapshot_id":"case-snapshot-204","fields":["content"]}
```

未授权或未知 snapshot 明确拒绝，不扫描其他归档，也不悄悄回退到当前图。归档读是只读事实，不授予写归档或写其当前后继的权限。source 中的旧 graph IDs 不能未经已验证的 live 映射就直接复制进当前变更；当前对象定位仍须经 before/candidate scope 核验。

Assessment 可以用带 source_snapshot_id 的 source 节点引用作历史 basis，同时用 before/candidate 定位实际待判断对象；不能把 source 当 live candidate，也不能凭旧来源缺边声称当前关系不存在。absence claim 仍只允许 before/candidate。request_context 不增加第四种归档入口，它仍是原三类当前事务上下文申请；归档只通过 read_nodes 的明确 source selector 读取。

### evidence 正文和来源身份

来源身份与正文存储分开：两个 evidence IDs 即使正文相同，也继续分别指向其来源；正文按内容 hash 共享。重复读取同 ID 不再追加正文。正文内容变化应创建明确的新来源版本并使相关决策待复核，不能悄悄覆写旧 ID 的已接受证据。

Writer 读取的纯来源事实可供 Assessment 复用，但必须仍绑定真实来源/版本。Writer 的推理、keep/repair 结论和草稿解释不作为已验证事实复用。

## 4. 草稿编辑与提交生命周期

### edit_draft 是原子文件修改

一次 edit_draft 携带 expected_version 和操作列表；controller 对操作格式、目标、授权和本轮可检验前提完成检查后，才整体接受并分配新版本。允许中间草稿尚未满足完整图语义或跨 change 引用；提交前必须补齐。任一操作失败则整次不生效，成功项也不会悄悄落入草稿。

- put_change：按 change_id 添加或完整替换一项公开变更。
- remove_change：明确删除该项；不存在的 ID 给精确错误，不假装删除成功。
- put_decision：维护某个 signal 的当前判断及关联 change_ids。

公开变更种类为 edge_update、edge_create、edge_merge、retire、node_update、node_create、node_rewrite、relation_declare。`node_rewrite` 是单 owner 的完整保义改写，可从旧 conflict 形成新的普通 content 或完整 alternatives；真实 source_routes 说明每个旧单元去向，不拼凑旧 conflict/resolve API。实施时需要新增受信的单 owner 编译适配，不能假装现有旧 API 已能直接执行这个新操作。

没有 whole-Patch 的“遗漏即撤回”。新调用没提到的变更继续存在；修改某一项不重发其他项。撤回变更时优先在同一原子编辑中调整受影响 decision；若中间草稿仍有悬空 change_id，它可以继续编辑，但不能 submit。

controller 分配 draft version。模型不能通过更改版本号、重建账本或调整排列制造新草稿。匹配当前 expected_version 的重复等价编辑可作为 no-op 回执，不增加有意义的修订计数。若 expected_version 已过期，返回 VERSION_MISMATCH、实际版本与可核验回执；不能自动把旧编辑套到新版本，模型应先基于当前草稿重建操作。

### CAS 与提交阶段锁

expected_version 的检查与草稿版本/内容写入必须处于同一持久化 CAS 事务，不能先读版本、退出锁后再写入。两个同时指向 version=1 的编辑，至多一个推进到下一版本；另一个得到 VERSION_MISMATCH，不能两者都“成功保存 version=2”。

submit_draft 原子锁定当前 head，并把任务转为 validating/assessing。正常提交已经结束 Writer，此时迟到 edit 不再被接受，应返回阶段不匹配而不是悄悄改变 head。只有机械失败或独立语义拒绝已持久化后，controller 才恢复 editing 并提供相应反馈。deferred 也保持未闭合状态，不趁机部分提交。

最终 commit 同时核对：已接受 workspace 的 before hash、canonical head 与 submitted_version 一致、真实 candidate 对应该版本、Review/facts/policy 绑定仍有效。即使某个旧 Review 对它当时的候选完全正确，也不能在 head 已变化后提交旧候选。本方案选择串行阶段门禁；不提出评估期间并发编辑而缺少失效控制的行为。

这里只规定拟议持久化语义。docs-only shape simulation 可以验证阶段参数和版本拒绝，不等于已经实现数据库 CAS、分布式锁或真实提交事务。

### submit_draft 只提交版本引用

submit_draft(expected_version) 引用已经存储的 canonical draft，不携带全文。它正常结束当前 Writer episode，随后 controller 执行：

```text
检查提交版本与全部 Signal 决策
→ 从固定 before 编译 canonical draft
→ 校验来源、引用、关系结构、完整关系组和必要耦合影响
→ 建立实际 candidate / affected diff
→ 独立 Assessment
→ 全部义务闭合后原子 commit
```

pending 仅用于编辑过程，不能作为正式提交决定。每个 Signal 在 submit 时应为 keep、repair、needs_context 或 needs_identity：repair 引用真实 change_ids；keep 没有 change_ids 且说明为何已满足。出现 needs_context/needs_identity 时 submit 返回 deferred，保留未闭合任务并交 controller 路由，不把它当作编译或最终接受。不存在另一个 defer 枚举。submit 不是最终 graph 通过。编译失败时不生成伪候选、不进入针对错误候选的语义评估，controller 重新给 Writer 当前草稿及精确机械反馈；已接受 graph 始终保持不变。

## 5. 每轮具体示例

以下使用教学 ID，说明上下文位置和版本行为，不是一次新的真实模型调用。完整工具参数结构见输入输出契约；业务变更 value 使用其对应的实际类型。

### 场景

局部图有 Metric M、Concept C-old 与 C-input，两条详边：

```text
E-old:   M --depends_on--> C-old
E-input: M --depends_on--> C-input
S1: 检查 M 的直接输入是否一致；这只是疑点，不预先要求删哪条边。
```

初始 M 只有“某类对象计数”的摘要，没有完整 expression。模型不能据此认定两条 depends_on 都应保留，也不能凭名称认定两端等价。

假设后续精确来源确认：M.expression 的直接操作数是 C-input，E-old 的旧直接依赖归属错误，E-input 是保留原有效计算含义与来源的合法后继。只有在这一前提成立时，退役 E-old 才是本例的可选修复；如果旧边还有独立业务含义，必须另行保留，不能机械删除。

### R0：初始输入

| 消息 | 具体内容 |
| --- | --- |
| System | Writer + 当前 direct-input guidance |
| immutable_task | task=T1；before=B1；S1；授权 IDs 与明确局部完整性 |
| working_graph_view | M/C-old/C-input 粗节点；E-old/E-input 完整边；暂无 expression 详情 |
| evidence_view | 两条边及成员的 evidence IDs；无正文 |
| canonical_draft | version=0；changes 为空；S1=pending |
| feedback | 无错误；facts=F0 |

Writer 发起以下 read_nodes 参数，精确补读 M.expression：

```json
{"ids":["M"],"view":"before","fields":["properties.expression"]}
```

若取回 refs 指向 C-input，下一轮只在 M 记录中新增该字段；不增加第七、第八条 AI/Tool 历史。

### R1：补读后仍不猜节点含义

六个位置不变。working_graph_view 中 M.expression 完整，facts=F1；E-old/E-input 不重新输出第二份。Writer 如仍不能判断 C-old 与 C-input 的角色或 E-old 的来源含义，继续读取所需节点字段和 evidence；这些值合入第三、第四条。

读取已完整提供的 M.expression 时，仅回执已提供且 facts 不变；只有第一次注入新事实才可更新相关 facts 版本。

### R2：编辑被拒，版本不推进

准备放入 C1 这项合法关系变更，并将 S1 标为 repair。若原子操作里的 decision 错把 signal_id 写成任务不存在的 S-typo，controller 返回：

```text
当前草稿仍为 version=0，changes 仍为空，S1 仍 pending。
机械反馈绑定 submitted expected_version=0：
  path = operations[决策项].decision.signal_id
  unknown signal_ids = [S-typo]
  本次没有执行任何操作。
```

下轮六条消息仍保留相同 working view 和 evidence。feedback 明确给出失败路径、实际版本和未知 ID；不把半个 C1 写入草稿，也不要求模型重新输出图。若只是中间 decision 引用了尚未加入的 change_id，则与本例不同：可以保存为未完成草稿，但 submit 的跨项门禁必须拒绝，不能误写成每次 edit 都已完成所有图检查。

### R3：修正编辑并提交

Writer 在同一次原子 edit 中放入 C1，并让 S1.repair.change_ids=[C1]：

```json
{
  "expected_version":0,
  "operations":[
    {"op":"put_change","change":{"change_id":"C1","kind":"retire","value":{
      "before":[{"kind":"edge","id":"E-old"}],
      "after":[{"kind":"edge","id":"E-input"}],
      "explanation":"本例精确来源已确认旧直接依赖归属错误，保留现存实际计算后继及其来源。"
    }}},
    {"op":"put_decision","decision":{"signal_id":"S1","status":"repair","reason":"已核对完整公式与原始来源；退役的知识由明确后继保留。","change_ids":["C1"]}}
  ]
}
```

成功后 controller 给 canonical_draft version=1；未改变 before 或事实 F2。接着 Writer 调用：

```json
{"expected_version":1}
```

这是 submit_draft 的参数，不是完整草稿，也没有额外包装。controller 从 B1 + version 1 读取完整草稿并编译；相同 submit 请求在网络重试时指向同一个提交意图，不重新执行一次改变。

### R4：独立 Assessment

| 消息 | 具体内容 |
| --- | --- |
| System | Assessment + 当前 direct-input guidance |
| immutable_task | 同 T1/B1/S1 与 scope；不能删掉 S1 以便接受 |
| working_graph_view | controller 实际 candidate=C1-hash、affected diff；详边显示 E-old 是否真实退役、E-input 是否真实保留及来源变化 |
| evidence_view | 可复用的原始来源正文与来源身份；不把 Writer 的理由标成已核实证据 |
| canonical_draft | version=1；C1 与 S1.repair，仅供追踪变更，不代替实际 candidate |
| feedback | 当前候选绑定；旧语义问题若有则 pending_recheck；没有 Writer 的整段对话 |

Assessment 必须独立核查 M 的真实计算和前后关系；需要原节点或 candidate 详情时补读。例如读取本次实际候选的 M：

```json
{"ids":["M"],"view":"candidate","fields":["properties.expression"],"draft_version":1}
```

candidate 必须指定已实际编译的 draft_version，不能把“当前”隐式替换为其他版本。即使 Writer 原来选 keep/no-op，也有 S1 的实际关系材料和独立 signal_review，不能仅因没有 diff 就免评。

如果已满足目标，Review 用 expected_version=1，对 S1 给 satisfied，且 issues 为空。若出现新的实际回退，issue 可以没有原 signal_id，但 refs 必须落在 affected closure，并被 controller 作为后续必须追踪的回归事项登记。每项 issue 用 kind 区分 defect、missing_context、identity_counterexample；因此全新回归即使 signal_ids=[]，也能给出正确的上下文/身份结论，不伪装成已有 Signal 失败。

### R5：语义拒绝后的重评

假设 Assessment 给 I1：当前改法没有保留某项具体来源范围，要求恢复该含义。I1 绑定 candidate=C1-hash、draft version=1、facts=F2、明确 before/candidate basis。

Writer 修订得到 version=2 后，I1 不能因 version 增加而自动删除，也不能直接被当成“当前候选仍然错误”。controller 将 I1 标为 pending_recheck，保留原 basis 与 required_outcome；重新编译得到 C2-hash 后，Assessment 必须在 prior_issue_reviews 中对 I1 恰好给一条 resolved/still_open/needs_context/needs_identity。没有提到 I1 的 Review 会被拒绝，不从新版本或解释中的措辞推断它消失。resolved 不引用当前 issue；其他状态须关联本次明确问题。

若复核确认 I1 已解决且 S1 已满足，可返回如下 Review 参数：

```json
{
  "expected_version":2,
  "verdict":"accept",
  "signal_reviews":[{"signal_id":"S1","status":"satisfied","explanation":"已核对真实候选的直接输入与来源范围。","issue_ids":[]}],
  "prior_issue_reviews":[{"issue_id":"I1","status":"resolved","explanation":"按旧问题依据与本次候选逐项比较，要求保留的范围已恢复。","issue_ids":[]}],
  "issues":[]
}
```

如果 I1 resolved 但新 I2 仍然成立，可以明确关闭 I1 并保留 I2，整体 verdict 仍非 accept。旧回归 issue 即使没有原 signal_id，也必须出现在 prior_issue_reviews；不能因为它不属于某个 Signal 就漏掉。

如果代码反馈指出当前 version=2 的结构错误，而 I1 仍绑定旧 C1，两者分别展示。模型应先使当前草稿结构有效，再对新 candidate 复核 I1，不能把旧候选的错误路径硬贴到新图上。

### R6：任意阶段中断恢复

- 停在 R1：恢复 B1、当前授权 scope、精确已读详情与来源映射；六条消息重建后不重复读 M.expression。
- 停在 R2：草稿仍 version=0，原子失败反馈保留；不能把被拒的 C1 当作存在。
- 停在 R3 提交后、编译前：用持久化提交意图继续同一版本；不要求 Writer 重发全文。
- 停在 R4：保存的合法草稿确定性重编译且 candidate hash 一致后，进入未完成的独立 Assessment，不重跑 Writer。
- 停在 R4 已接受后、commit 前：仅当候选、事实、规则和 Review 绑定均兼容时复用接受结果；否则明确复评。
- commit 已完成、队列尚未移除：以提交事务/history 证明完成，处理队列回执，不再运行 Writer 或 Assessment。

## 6. 机械诊断、语义问题与无进展

### 两种反馈分别管理

| 类别 | 必须绑定 | 新版本出现时 |
| --- | --- | --- |
| 机械编辑/编译诊断 | attempted/submitted draft version、change_id、字段路径、实际 scope/before | 只对该草稿成立；重编译后确认已修或更新诊断，不混到另一个版本 |
| 独立语义 issue | candidate hash、对应 draft version、所用 facts、精确 basis 和 Signal/回归关联 | 保留原结论为历史判断并标 pending_recheck；不得自动清空或自动沿用为新候选结论 |
| 缺失前提 | 具体对象/字段/view 与当前判断为何需要它 | 获得精确材料后复核；请求本身不等于读到了新事实 |
| 协议错误 | 哪个回复、哪些调用未执行、保留的实际草稿版本 | 恢复同一公开 schema，不能重放被拒的操作集 |

每条输入 pending_recheck 必须有独立 prior_issue_review，只有明确 resolved 才闭合。非 accept 的整体 verdict 依据当前所有 issues 的种类，按 identity_counterexample → missing_context → defect 的优先级派生；不只看 signal_reviews。accept 还要求全部 Signal satisfied、全部旧 issue resolved、本次 issues 为空；其余结论必须有当前问题，revise/identity 的每个问题都有明确 basis。

结构错误优先给 code/path/owner/missing/extra/duplicates 和简短修复目标；解释保留原具体含义，不只返回“Patch invalid”。大段重复图正文用当前工作记录引用表达；必要历史 basis 必须保留正确快照，不能把引用指向已变化的当前节点。

### absence claim 的核验与反例

Review 的可选 claim 只声明可机械核验的 relation_absent / concept_owner_absent 前提，不是另一个修改指令。controller 先验证谓词、端点、view/分支和本地授权，再在权威 before/candidate 快照中核验；猜出的全局 ID 不能通过 claim 绕过读取权限。

若前提被现存关系反驳，返回精确反例，按相同对象/view/snapshot/字段规则合入 working view，并绑定原 issue 与本次事实版本。原 candidate 不因此被改写，其他有效问题不能一起被清除。只有实际新增了此前未给的相关事实，才可能改变无进展判断；同一个反例重复返回不构成又一次新知识。

### 什么才允许重新考虑同一决定

无进展签名基于规范化后的实质变更/Signal 决策、具体拒绝、与该判断相关的真实事实和 policy；不以 draft_version 递增判定进展。

- 新节点字段、有效新 evidence、明确扩展的关系范围或真实候选改变可改变相关事实版本。
- 重复读、同正文换查询参数、工具 call_id、无关格式空白、对象键排列和单纯 version 增加不算新事实；不得为归一化而改写 SQL 字符串字面量或有实际语义的源文本空白。
- 只有对相关判断新增事实才重开其机会；无关的另一节点补读不能无限重置被拒变更的计数。
- 整轮多个 submit/读写混用等协议错误按一个模型回复计数，整轮不执行；不把同一回复中的两个坏调用当成两次独立无进展。
- 网络重试重用同一提交意图与 expected_version，不记为新的模型决策；传输重试预算另计。

详情被驱逐后再补读同一内容，不是第一次出现的新知识；恢复曾经失败过的同一相关事实集，也不清空其失败计数。

无进展停止必须保留当前草稿、待复核问题与 checkpoint，不通过清空问题、换一个 change_id 或重启进程恢复无限尝试。

### 损坏 JSON 的结果恢复

controller 明确给出 target_tool 与该工具原参数 schema，并暂停全部读取工具：

| 损坏调用 | 允许恢复的唯一对象 | 不允许的替代 |
| --- | --- | --- |
| edit_draft | 同一原子编辑调用的 expected_version + operations 参数 JSON | 丢掉未执行的编辑，改成 submit_draft |
| submit_draft | 仅 expected_version 参数 JSON | 重发整个草稿或旧 MergePatch |
| Review | 同一 Review 参数 JSON，包括全部 Signal 与旧 issue 复核 | 省略难判断的问题以取得 accept |

这只是传输格式修复，不改变字段、来源或版本约束。损坏原回复的全部调用都没有执行；之前有效草稿、机械反馈、pending_recheck 与来源依据继续可见。若恢复时版本已过期，返回 VERSION_MISMATCH，不按旧 expected_version 重放编辑。schema 合法但内容错误仍走明确机械/语义反馈，不反复切换输出格式。

## 7. checkpoint 的恢复边界

checkpoint 至少保留固定 before 与当前 workspace 绑定、任务/Signal/权限、原子编辑日志与当前草稿版本、提交意图、编译候选、issue 状态与 basis、事实索引、纯读取详情、授权 source snapshot 目录及绑定、来源正文映射，以及模型策略/输出契约版本。

**能重播的是可验证的纯详情事实，不能重放旧工具副作用。** read_nodes/read_evidence 的缓存条目须验证对象、view、snapshot、字段完整性与内容 hash；source 缓存还必须恢复并验证原有 source_snapshot_id 授权，不能换成当前 before 或其他归档；request_context 产生的授权扩展必须恢复其已批准结果，不能重新用旧请求静默扩大权限。edit_draft/submit_draft 以持久化事务与幂等回执恢复，不再次执行旧 AI/Tool 历史。

当前已接受 workspace graph hash 与 checkpoint 预期不同，停止并报告冲突；不能为了继续而用旧 before 覆盖新 graph。若外部提交已经完成，先用事务记录证明该任务的归属和结果再收尾，不仅凭“版本更大”推断已完成。

源码/Prompt/工具策略变化后的迁移保留原记录。已接受 graph 和已完成任务不重跑；尚未提交候选需明确判断：机械重编译是否仍等价、哪些 read facts 仍可信、旧 Review 能否复用。不能仅换新 policy 标签就宣布旧语义接受仍有效。

## 8. 输入预算与缓存布局

### 预算是整个实际请求

计入 System、所有工具 schema、immutable task、working graph view、source evidence、canonical draft、反馈与必要历史 basis，以及序列化开销。分别显示字符/token 估算及供应商实际 usage，不能把某个节点列表预算当作整次请求上限。

为工具读取、单次返回与整个 episode 设置预算；连续没有新增相关事实的读取也必须有上限。超限时可驱逐纯可重取的、当前判断不需要的节点详情，保留“省略且可补读”的标记；不得驱逐未解决 issue 的必要 basis、来源版本、必须同时成立的关系组或 Signal 决策。

canonical draft 也必须在当前请求中完整可见；当前工具没有 read_draft，不允许静默隐藏部分仍有效的变更后继续编辑。若草稿本身无法纳入预算，必须先按依赖计划拆分事务或另行 review 草稿分页读取契约。

若不可分割的关系/分支/来源闭包本身超限，分批需有显式的覆盖和依赖计划；不能把同一不相容组合拆开各自 accept 后直接宣称整体通过。无法安全分批时明确中止，保留草稿与检查点，不静默截断。

### 稳定前缀与实际可复用边界

- System 按共用图契约、当前角色、该角色工具/输出、当前 Signal 共用依据与角色指导排列；同 task 内固定。
- 工具名称、顺序、schema 固定，不把 task IDs、图 IDs 或实时枚举塞进工具定义。Writer 与 Assessment 的结果/编辑工具不同，不能保证供应商跨角色工具前缀复用。
- task_id、草稿版本、候选 hash 与错误不进入 System；固定任务在动态工作区前。
- 工作记录与字段采用稳定顺序；对象/schema 键规范化不能改写业务字符串、SQL、列表语义或来源顺序。

合并式 working view 会在补字段或换 candidate 时改变第三条消息，因此后续 evidence/draft/feedback 的完整字节前缀未必可复用。这是控制重复内容与缓存命中之间的真实取舍；本方案保证 System 与 immutable_task 稳定，不声称每次读取都能保留整个旧上下文前缀。

正常工具集合保持固定；损坏 JSON 的无工具恢复属于显式异常模式，停用工具可能改变前置 tool schema 并失去原缓存前缀，应单独计量，不能为了“命中缓存”继续授权读取。

缓存效果要用真实请求 usage 验证。减少重复 graph、schema 与读历史首先降低必须处理的输入；没有测量就不承诺具体命中率或速度提升。

# 节点整合两个 Agent：局部输入与中文 Prompt 复阅稿

状态：**接口与 Prompt 草案，尚未接入源码或后台 runtime**。本次只整理节点整合 Writer、独立节点 Assessment；不调整 Identity、Graph Reconciliation、checkpoint 或已完成任务。

输入输出约束见[类型 guidance 详解](format-and-guidance-review.md)，最新组装与缓存方案见[第三版说明](cache-and-review.md)。下文保留局部任务设计；完整 System 统一按共用语义、角色、角色输出契约、唯一类型 guidance 组装，类型模块严格置于末尾。

## 1. 是否需要完整 graph？

**不需要。** Identity 已完成分组后，Writer 的工作单位应是一个 group，不是整个 graph。

最开始只提供 ID 的应当是 **evidence**，不是待合并节点。节点正文和相关 relations 是必需输入，应该直接提供；Question、Gold SQL 和原始 evidence 正文则按 ID 读取。

原先的 “before 图” 混合了两个概念：代码编译 Patch 所依赖的固定已接受状态，以及 Agent 需要阅读的业务材料。代码可以保留全图状态用于确定性编译，模型只需要本 group 的合并前局部输入。新 Prompt 不再要求模型理解一个可任意浏览的 “before graph”。保留的 `Review.refs.view=before` 只是现有输出协议中的快照标签。

## 2. 边需要同时处理，但职责必须划清

| 内容 | Writer 是否看到 | 谁决定、谁执行 |
| --- | --- | --- |
| group 全部成员、完整 alternatives | 完整看到 | Writer 决定整合内容，代码保存结果 |
| 从成员出发的全部出边 | 完整看到，含来源、条件和分支范围 | Writer 决定必要的语义调整；无变化边继承 |
| 指向成员的全部入边 | 只读，含来源、条件和分支范围 | 代码重定向；Writer 不改变外部节点含义 |
| relation 端点 | 只提供解释关系所需的 ID、类型、名称、有关字段 | 只读；不是端点所在整图 |
| 消费者约束 | 提供引用 group 的具体字段和必要角色/条件 | Writer 检查兼容性；实质修改交回 controller |
| 成员、refs、边端点的 ID 重定向 | 提供映射规则 | 代码执行，不让模型重新生成 |
| 严格重复边 | 比较所需关系在局部输入中 | 代码按严格规则去重；相似关系不自动合并 |
| Question / Gold SQL / evidence 原文 | 初始仅 ID | 两个 Agent 均可按需 `read_evidence` |
| 其他 group、其他邻居的完整图 | 不提供 | 不开放整图查询或修改 |

出边与入边不能只给端点：例如两条边都指向同一列，但一条只适用于 alternative A，另一条只适用于 B，作用域是知识的一部分。只传“连接了哪个节点”会使合并丢失含义。

入边也不能完全省略。例如外部 Rule 把某个成员当作日期角色，合并后却把它压成通用文本属性，ID 重定向虽然成功，消费者含义仍然被破坏。因此提供**与本 group 有关的具体消费者字段**，不提供该消费者的整张邻域图。包含多少字段应由字段依赖规则确定，不由模型继续扩图寻找。

如果保义必须修改外部消费者，当前局部任务不能假装已经完成。Writer 报告缺少前提/依赖，或 Assessment 返回 `needs_context`；controller 扩充有明确边界的局部任务、处理耦合依赖，必要时交给 Reconciliation。保义缺陷未处理前不接受候选。

## 3. 两个角色的 System 组装

- [节点整合 Writer：角色与局部输入模块](writer.system.zh.md)
- [独立节点 Assessment：角色与局部比较模块](assessment.system.zh.md)
- [通用字段与整合决策](common-fields.zh.md)
- [Writer 输出契约](writer-output-contract.zh.md) / [Assessment 输出契约](assessment-output-contract.zh.md)
- 类型 guidance：[Concept](types/concept.zh.md)、[GenerationRule](types/generation_rule.zh.md)、[Metric](types/metric.zh.md)、[Event](types/event.zh.md)

上面分别是有单一职责的模块，**完整拼接后的每类型 System 在 HTML 中逐份展示**。只针对本 group 的唯一类型和实际关系发送 schema；不拼接整份全图工具说明、所有关系治理指南或其他阶段契约。

| Agent | 唯一读取工具 | 结果工具 | 不提供的能力 |
| --- | --- | --- | --- |
| 节点 Writer | `read_evidence(ids)` | `submit_patch`；缺少前提使用其现有 `failure` 字段 | `inspect_graph`、`lookup_catalog`、`read_guidance`、`read_source_units`、`inspect_draft` |
| 节点 Assessment | `read_evidence(ids)` | `Review` | 所有 graph 查询、Patch 与修改工具 |

不需要 `read_source_units`，因为本 group 所有单元已经随输入给全；不需要 `inspect_draft`，因为修订时会直接给当前唯一草稿。节点类型约束已在 System 中，不再提供重复读取说明全文的工具。

## 4. Writer 的 User Prompt

固定任务句：

> 请整合下面唯一的 identity group。合并前节点和相关 relations 是本任务的比较基线；只提交完整节点结果与必要出边操作。原始 evidence 按需读取。

以下是一份逻辑 JSON 任务包的字段结构；实际发送时按[第三版缓存布局](cache-and-review.md)拆为静态类型、固定任务、已读 evidence 与末尾动态状态。不会在每条消息中重复该完整 packet。以下是**拟议输入字段**，不是声称当前代码已生成这些字段：

```json
{
  "task_id": "node-merge-example-1",
  "group": {
    "group_id": "identity-group-1",
    "node_type": "concept",
    "owner_id": "concept-A",
    "member_ids": ["concept-A", "concept-B"],
    "members": ["各成员的完整业务记录；实际发送对象，不发送这段说明"]
  },
  "node_schema": {"node_type": "concept", "fields": "实际 schema 声明的 properties 字段、类型和必填约束"},
  "source_unit_manifest": ["本 group 的真实 unit_id、成员、版本种类及旧 alternative ID"],
  "relations": {
    "outgoing": ["每条完整出边；含 edge ID、两端 ID、谓词、业务属性、分支范围、evidence ID"],
    "incoming": ["每条完整入边；同样保留含义、分支范围和 evidence ID"]
  },
  "endpoint_directory": ["上述关系和内容引用涉及的必要端点信息，物理列包含表名/列名"],
  "consumer_constraints": ["消费者 ID、引用成员/旧分支的具体字段路径与值、必要条件和角色"],
  "relation_contracts": ["本局部输入实际用到的谓词契约"],
  "completeness": {
    "member_content": true,
    "outgoing_relations": true,
    "incoming_relations": true,
    "required_dependencies": true
  }
}
```

这里的数组说明是模板占位符；实际成员、边和契约必须使用结构化对象。`evidence_ids` 附在节点、alternative、边及需要归因的约束上，不另发送一份所有节点/边重复组成的 evidence 清单。原文只通过工具返回。

`group.members` 每个成员提供一次完整业务内容。若成员为 conflict，提供完整 resolved alternatives 与真实公共来源归属，不重复发送存储层 `base + overrides`。`source_unit_manifest` 只给句柄及定位，不再复述内容。

端点目录对 group 成员只引用 ID；外部端点按 ID 去重一次。消费者约束引用已提供的入边 ID，不再次抄写完整入边。

## 5. Writer 的输出：完整节点 + 必要出边变更

这里保留现有 `MergePatch` 的公开字段，避免仅改 prompt 又与编译契约不匹配。

- 一项 `integrate_groups`：完整 `owner_id`、`member_ids`、完整 `content` 或完整 `alternatives`、`source_routes` 与解释。
- 出边：仅必要的 `edge_updates`、`edge_merges`、`create_edges`、边级 `retirements`。
- 不输出其他节点操作、不修改入边、不新建谓词。
- 无法安全决定时，使用现有 `failure` 字段说明缺少什么；它是任务阻塞结果，不是错误的合并候选。后续 controller 要把此类结果区分为缺少上下文或身份反例，不反复当作格式错误重试。

这在语义上就是用户希望的“合并后的节点信息及其出去的边”。模型不必每次重新生成完整出边集合；代码继承未变化边并应用必要操作，然后把**完整的实际结果出边**提供给 Assessment。这样保留了已有编译支持，也减少了漏边与重复生成。

### 来源单元与 evidence ID 为什么分别保留？

- `evidence_id`：原始 Question / Gold SQL / evidence 的读取键。同一来源可以支持多个节点或多个分支。
- `source_unit_id`：这次合并前的一份普通节点内容、旧 alternative 或真实公共来源单元。它回答“这份已接受知识最终保留到哪里”。

因此，仅列 evidence ID 无法证明两个不同的旧 alternative 都被保留。`source_routes` 是简短的保留路径，不是要求模型重建内部来源对象、哈希或版本账本；Assessment 仍须比较真实语义。

## 6. Assessment 的 User Prompt

固定任务句：

> 请独立评估这个 group 的实际编译结果。比较合并前后节点内容、relations 与来源归属；只报告可核实的保义缺陷。原始 evidence 按需读取。

逻辑输入结构如下；实际按第三版分区，其中变化的 candidate、change_map、source_routes 放到末尾当前状态，不提前打断固定输入和证据前缀：

```json
{
  "task_id": "node-review-example-1",
  "group": {"group_id": "identity-group-1", "node_type": "concept", "owner_id": "concept-A", "member_ids": ["concept-A", "concept-B"], "members": ["完整合并前节点"]},
  "node_schema": {"node_type": "concept", "fields": "与 Writer 相同的实际字段约束"},
  "before_relations": {"outgoing": ["合并前完整出边"], "incoming": ["合并前完整入边"]},
  "candidate": {"node": "实际编译后的完整 owner 节点", "relations": {"outgoing": ["实际结果完整出边"], "incoming": ["实际结果完整入边"]}},
  "change_map": {"node_aliases": {"concept-B": "concept-A"}, "edge_aliases": {}, "branch_routes": [], "removed": []},
  "source_unit_manifest": ["与 Writer 相同的真实来源单元"],
  "source_routes": ["本候选实际采用的来源路由"],
  "endpoint_directory": ["必要端点；已提供的完整节点不重复发送"],
  "consumer_constraints": ["必要消费者字段与语义约束"],
  "relation_contracts": ["本任务实际用到的谓词契约"],
  "completeness": {"member_content": true, "before_relations": true, "candidate_content": true, "candidate_relations": true, "required_dependencies": true}
}
```

其中的说明占位符同样须替换为实际对象。候选来自编译器，不把 Writer 整份对话、读取过程或完整 Patch 再抄进 Assessment 输入。独立性来自它对原始局部内容和实际结果进行比较，而非复述 Writer 的理由。

当前只提供局部片段，不能输出“整个 graph 没有此关系/owner”。若这个全局前提确实是判断所必需，返回 `needs_context` 让 controller 核实；不要开放整图读取来解决局部接口遗漏。

## 7. 一次合并的上下文示例

以下均为**教学示例，不是本次 thrombosis / toxicology 的真实数据或模型验证结果**。

### 示例 A：不必读取 evidence

- 成员 A：“登记日期”，targets 为 `[[登记表.登记日期]]`，出边 `grounded_on → 登记表.登记日期`。
- 成员 B：“登记日期”，同一身份，但记录另一种相容匹配方式 `[[明细表.登记日]]`，出边指向相应列。
- Writer 保留两个独立 targets group，继承两条出边；来源单元均路由到 `result`。无需把两个列变成一个联合匹配组，也不必读 Question / Gold。
- Assessment 比较两组匹配方法和两条出边仍保留，即可接受；如果候选只剩其中一组，应指出具体丢失。

### 示例 B：需要原始 evidence

- 同一 Metric identity 的两个成员，一份定义 COUNT(*)，另一份定义 COUNT(DISTINCT patient_id)，局部说明没有交代适用条件。
- Writer 如需确认是否存在原始条件，调用这些成员的 `read_evidence`，只读对应来源。
- 若原始材料没有区分条件，不能编造“住院/门诊”来强行统一；保留两个完整 alternatives，包括各自公式、粒度与来源。
- 如果某条出边只属于第一种定义，其输出作用域只能包含第一分支。Assessment 同时比较节点定义与关系范围，不因为两个分支出自同一 case 就认定可合并。

### 示例 C：外部消费者不能被静默改变

- 外部 Rule 引用成员 B 并要求其保持特定日期角色；该字段随 `consumer_constraints` 提供。
- 合并 proposal 若保留等价日期角色，代码重定向即可。
- 若 proposal 把它改为含义更广、无法满足 Rule 的对象，应修改当前节点候选；若保义确实需要改写 Rule，则报告依赖并阻塞当前候选的接受。
- 不让 Writer 浏览 Rule 所在整图，也不让 Assessment 把这个问题当成缺失边而随意补边。

## 8. 多轮上下文：保留什么，不累计什么

本表描述每轮所需的逻辑材料，不表示把全部 User 材料塞入单条消息；实际消息顺序以第三版缓存布局为准。

| 请求 | System | User 中的固定局部材料 | 本轮动态内容 |
| --- | --- | --- | --- |
| Writer 首轮 | 仅 Writer 完整 System + 实际工具 schema | group、relations、必要依赖、来源单元；不含 evidence 正文 | 无历史草稿 |
| Writer 读 evidence 后 | 同上 | 同一份局部任务，不追加第二份 | 必要 assistant 读取调用及对应结果；相同 ID 原文不重复注入 |
| Writer 草稿编译失败 | 同上 | 仍是同一份合并前局部输入 | 唯一当前草稿、与该草稿绑定的完整有效错误、已读 evidence |
| Assessment 首轮 | 仅 Assessment 完整 System + 实际工具 schema | 合并前局部内容、实际候选、局部 relations、映射及必要依赖 | 不带 Writer 对话 |
| Assessment 读 evidence 后 | 同上 | 同一份比较材料 | 本 Assessment 已读取的相关 evidence |
| Writer 收到 revise | Writer 的 System | 合并前局部输入保持固定 | 当前唯一草稿、候选 ID、该候选的具体问题及必要候选片段、相关已读 evidence |
| Assessment 复评 | Assessment 的 System | 合并前输入 + 最新实际候选 | 仅前次相关问题，用于判断是否解决；不叠加旧候选全文 |

以上是建议的上下文管理，不是给模型的消息压缩指令。由 controller 进行协议正确的重建：保留合法的 tool call/result 配对或把已读 evidence 放入单一受控区，不能把工具返回伪装成 system 指令。

旧草稿、旧候选、历史错误全文与重复工具正文不随轮次线性叠加。**仍有效的错误不能只保留“最新一条”而丢失**：每次重建保留当前候选尚未解决的完整问题集合、绑定 ID 和必要事实。恢复 checkpoint 时也须恢复这些绑定与已读 evidence。

若出现候选与草稿不同步，提供明确的 `draft_id`、`issues_draft_id`、`candidate_draft_id`，不能把旧候选的问题冒充为当前草稿事实。普通轮次无需暴露一整套未使用的历史状态。

## 9. 当前代码差距：仅改 Prompt 不足以生效

| 当前实现 | 接入该方案需要的代码调整 |
| --- | --- |
| `task_agent.py` 为非 Identity Agent 统一注册整套读取工具 | 按 phase 注册工具：节点阶段仅 `read_evidence` + 对应结果工具；关系阶段单独保留它实际需要的工具 |
| `node_context.py` 装载 evidence 正文，并允许完整图补读 | 构造一个 group 的局部 packet；初始移除正文；提供完整成员、relations、必要端点与消费者约束 |
| System 继续拼接 content/conflict/protocol 通用文件 | 按共用语义 + 角色 + 角色输出契约 + 唯一类型 guidance 组装；工具 schema 与边界一致，不再拼接旧全图说明 |
| `MergePatch` 公开了大量通用字段 | 给节点 Writer 派生一个窄 schema，只保留 integrate_groups、必要出边操作、failure 和 explanation；代码校验禁止越权操作 |
| `read_evidence` 可解析 context 中的 evidence | 节点任务只允许读取当前 packet 列出的 evidence IDs；检查未知或越界 ID，不静默返回空材料 |
| 编译器在全局状态上执行 ID 映射 | 全局状态仍由代码持有；无需发送模型。校验不得只因端点相同而丢失边的作用域/内容 |
| 一个 task 可能包含多个 identity obligations | controller 按 group 调度、编译时仍准确检查当前任务义务；不能只在 Prompt 宣称“一组”而输入仍给多组 |
| Assessment 校验可引用全图中存在的对象 | 增加局部可见/可评估范围校验；禁止凭已知外部 ID 越权提出无输入依据的问题 |
| `failure` 可表达无法完成，但未提供专门的缺少局部材料结果结构 | 复用当前字段作明确终止反馈；controller 分类并补齐，不反复催交同一 Patch；将来如增专用状态应单独更新 schema |
| 对话与 checkpoint 保留多轮状态 | 根据上述固定/动态材料重建上下文，保留未解决错误与版本绑定；旧 checkpoint 保留原件并显式迁移 |

对于成员或 relations 特别多的 group，局部输入仍可能很大。应在调用前核算实际模型 token 与输出余量；超限则停止并报告局部任务体量，不能悄悄截断必需 relations，也不能自动恢复整图工具权限。是否分片评估需另定覆盖和汇总规则，不在这次 Prompt 草案中假装已经解决。

## 10. 本次交付与验证边界

本次创建两份完整中文 System Prompt、Writer/Assessment 的 User 输入模板、职责和上下文说明，以及可浏览的 HTML。保留现有输出字段；文件中明确标出新输入 packet、窄工具注册与范围校验仍需代码接入。

不修改当前生产 Prompt 组装代码，不切换后台 runtime，不迁移或重跑 checkpoint。未进行真实模型调用，因此不把本次文案整理表述为“真实 merge 已通过”。

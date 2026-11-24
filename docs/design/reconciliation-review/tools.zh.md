# 工具说明与 schema 中的自然语言：完整中文对照

这些说明不在 System Markdown 中，而是作为公开工具的 description/parameters 发送。Writer 与 Assessment 当前拥有相同的六个读取工具；只有最终结果工具不同。以下为译文，不替换运行时描述。参数名、枚举和 JSON 键保持原样；完整原始 schemas 在 HTML 下载区。

## 1. 六个读取工具的 description 译文

### inspect_graph

在一个明确视图中读取准确对象或相邻关系。

items 接受节点或边 ID；fields 使用真实路径，如 properties.targets、properties.expression 或 properties.relation_metadata。content 读取完整节点业务内容；alternatives 读取完整解析的分支。裸 targets 和整个 properties 不是合法 selector。relations 先按方向和谓词筛选，再分页。不完整分页不能证明不存在。重复读取返回所选对象的当前值。读取不授权图修改。

参数：mode 必填，items/relations/consumers/contracts；ids、fields 可选；view=input/before/candidate，默认 candidate；offset 默认 0；limit 默认 30，代码要求 1–100；direction=incoming/outgoing/both，默认 both；edge_types 可选。consumers 表示内容中的引用者，和所有邻接边不同。contracts 的 IDs 是关系类型 ID。

### lookup_catalog

按准确的表名/表 ID 和可选列名定位物理 ID。

返回表/列名称及已有 Concept grounding owners。这是只读目录定位，不执行 SQL，也不是语义搜索。

参数：table 必填；columns 可选；view 默认 candidate；offset=0，limit=30，范围同上。

### read_evidence

按展示的 IDs 读取原始 Question、Gold SQL 和 evidence。

参数：ids 字符串列表，必填。注意：这个 description 说“展示的 IDs”；实现使用整个 context 的 EvidencePresentation，并未像新节点工具那样额外强制当前局部白名单。未知 ID 返回 missing_ids，不是 SQL 执行失败。此差别为当前实现事实，未在本报告中修改。

### read_guidance

读取当前决策所需的类型/操作指导。

参数：topic=concept/metric/generation_rule/event/operations/conflicts/relations。conflicts 返回已经在 System 出现的 conflict_contract；四类节点返回英文资产契约和当前类型 fields；operations 返回 operation_registry；relations 返回关系规则。完整按需指导译文另见 guidance 模块，不能把这些额外内容遗漏为“只发了一份短 System”。

### read_source_units

从固定 before 读取准确的、由 controller 管理的来源单元。

计划身份组的 owner/member 返回其完整身份组；其他 owner 返回独立 scopes。结构性公共字段不会创造来源单元。读取从不授权无关写入。

参数：owner_ids 必填且非空，必须是 before 中存在的 owner。它返回 manifest，不返回 Question/SQL 正文。

### inspect_draft

读取最新未应用草稿/错误，可只选部分 owners/sections。

sections 可以指定顶层操作，也可以指定 integrate_groups/conflicts 内的 source_routes/content/alternatives/conflict_points。过滤结果是投影，不是替代草稿。不改变任何已接受 graph。

参数：owner_ids、sections 均可选。返回 draft_id、issues、issues_draft_id；无草稿时返回 no_draft。完整草稿与当前 canonical draft 相同则不另回注全文；投影仍可能作为读取消息保留。

## 2. 两个结果工具的 description

### Writer：submit_patch

相对于固定 before graph 提交完整局部草稿。

替换上一份草稿。省略的操作视为撤回。NodeContent 按角色契约完整替换。有效候选结束本次 Writer，等待独立 Assessment，不直接提交 graph。

参数为 MergePatch 公共字段；不是 {patch:{...}}。具体完整字段表及代码门禁见 contracts-review.md。

### Assessment：Review

运行时直接把 Review Pydantic 类注册为结果工具，其工具 description 当前为空；职责说明来自 Assessment System。参数为 verdict、issues。candidate_hash 为 controller 字段，不在公开 schema 中。

## 3. parameters 内 21 项自然语言描述的完整译文

同一描述重复出现在多个 nested schema 时只列一次；每一原文及全部 JSON 路径保存在 field_descriptions.actual.json。

| 编号 / 位置 | 原文描述的中文译文 |
| --- | --- |
| 0 failure | 成功时省略或使用 JSON null，绝不能用字符串 'null'；失败时，给出无法完成安全局部决策的具体原因。 |
| 1 node_updates.add_evidence | 仅使用确实支持该保留节点的已知 evidence IDs，包括明确合并/替换节点带来的相关证据；已有准确来源会保留。 |
| 2 node_updates.properties | 仅列变化的属性键；每个已提供属性值整体替换该键，其他键保留。 |
| 3 edge_updates.add_evidence | 仅使用支持该边的已展示 evidence IDs；准确来源由代码解析。 |
| 4 edge_updates.properties | 变化的真实属性，包括 description 和 alternative_ids。每个已提供属性整体替换，省略键保持不变。 |
| 5 create_edges.id | 为本次创建提供新的边 ID；被拒提案不会在图中创建它。 |
| 6 create_edges.properties | 完整边属性，包括 description 和任何必要的 alternative_ids scope。 |
| 7 create_edges.evidence_ids | 支持该新边的已展示证据；已有边改用 edge_updates.add_evidence。 |
| 8 retirements.before | 要被替换的当前断言。before 中没有出现在 after 的对象由代码删除。 |
| 9 retirements.after | 实际后继断言/路径。仅在解释明确的错误断言弃用时允许为空。 |
| 10 edge_merges | Agent 声明这些是同一事实；代码保留准确来源引用。 |
| 11 edge_merges.source_ids | 要吸收到 survivor 的图 EDGE IDs，既不是 evidence IDs，也不是 SourceReference 的 source_ids。代码保留其准确来源。 |
| 12 edge_merges.survivor_id | 要保留的已有边 ID。其 description/properties 另在 edge_updates 中按 id 修改；merge 对象没有 properties 字段。 |
| 13 edge_merges.explanation | 解释这些边为何在相容范围内表达同一事实，以及如何保留实质限制。 |
| 14 alternatives.evidence_ids | 结果的已知 evidence IDs；已有输入知识也可通过 source_routes 保留归属，包括没有来源的历史分支。 |
| 15 conflict_points | 在完整 alternatives 之间仍未解决的一项决策。 |
| 16 conflict_points.choices | 针对某个具体未解决差异的一项一致立场。 |
| 17 conflicts.source_routes | 对已有 owner，将每个代码提供的来源单元恰好路由一次。改写耦合 owner 前读取其来源单元。新 owner 没有原始来源单元。 |
| 18 source_routes.source_unit_id | 精确复制 controller 提供的 source_units.unit_id，不编造 node/null scopes。 |
| 19 resolutions.discard_reasons | 列出其他每个已有 alternative ID 及其有证据的纠正/冗余理由。有依据的竞争选择必须继续保留为冲突。 |
| 20 issues.claim | 可以机械核验的“不存在”前提，绝不是创建关系的命令。 |

该表翻译的是 schema 的描述，不代表这些描述穷尽了全部校验。source_routes 覆盖、分支支持资格、metadata 相邻性、身份操作边界、ProposalScope 耦合权限以及来源保留仍有额外代码门禁，详见 contracts-review.md。

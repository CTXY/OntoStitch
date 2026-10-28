# NL2SQL Ontology：锚点输入与正式关系

## 1 权威来源

节点中的 references、表达式 refs 和 Concept target.references 是写入时的锚点，也是保留在节点上的内容属性。GraphManager 在写入阶段将连接落实为正式 relations；现有代码继续使用 GraphSpace.edges 存储，不增加另一套图容器。

后续检索、整合依赖上下文、在线投影和 Agent 读取均通过已保存的 relations 获取连接。读取阶段不编译 refs，不用 refs 补救缺失关系。保存验证可以检查锚点是否已经落实为关系，但不会修复或生成边。

六种节点为 Table、Column、DimensionValue、Concept、Metric、GenerationRule。Example 是外部证据，不是本图节点。

## 2 写入规则

一个锚点连接由起点、终点以及 Concept 的 target_id 确定。

- 没有显式边：按默认规则生成关系。
- 有显式边：保留显式关系，抑制该连接的默认关系。只影响这个连接，不影响其他锚点。
- 显式边也可以连接锚点之外的节点，表达独立事实。
- 同一关系身份不能通过不同 ID 重复创建；修改时更新已有 ID。不同关系名称可以表达不同事实。
- 删除显式覆盖而保留锚点，会恢复默认关系。彻底断开该连接需要同时移除锚点和相关显式边。
- 修改节点定义、移除锚点时，会清除原来绑定在该锚点上的默认边或显式覆盖。独立关系保留；删除节点仍需处理这些关系。

默认关系：

| 输入 | 默认关系 |
| --- | --- |
| Column.table_ref | belongs_to → Table |
| DimensionValue.column_ref | value_of → Column |
| Concept.targets[].references | grounded_on → Column/DimensionValue，附 target_id |
| Metric 定义引用 Metric | depends_on → Metric |
| Metric 定义引用物理节点 | grounded_on → Table/Column/DimensionValue |
| GenerationRule 定义引用 Concept/Metric | applies_to |
| GenerationRule 定义引用物理节点 | grounded_on |

触发条件、SQL 文本和 catalog 元数据不是另一套隐藏的出边。它们仍接受内容校验，不能通过普通属性字符串遍历图。

## 3 最少的关系元数据

GraphEdge 保留 id、edge_type、from_node_id、to_node_id、properties、sources。新增一个由代码维护的 reference_origin：

- default：由锚点生成的默认关系。
- explicit：明确指定的锚点关系，覆盖默认关系。
- null：锚点之外的独立关系。

这个标记既用于后续更新，也让消费者知道自定义名称的边仍然属于定义依赖，而不必回查 refs。已有 depends_on 等关系也保留其已知依赖语义。Agent 不填写或控制该标记。

Concept 继续使用 properties.target_id 区分不同 target 到同一节点的连接。只有一个匹配 target 时可由代码补齐；多个匹配 target 时返回歧义 feedback，要求指定 target_id。

冲突节点的关系在 properties.alternative_ids 保存所属分支 ID。分支选择直接过滤已存关系，不从分支 refs 重建。该元数据同样由代码维护。

Concept target 仍然只有 id、sql_expression、references。Metric 内容仍为 references、pattern、expression、sql_generation_plans；SQLGenerationPlan 是列表元素的类型，不另存一份内容。

## 4 自定义关系

自定义 edge_type 无需修改 schema 或注册表。非空名称、合法端点、JSON 属性和调用权限通过即可；描述可放在 properties.description。

已定义的关系名称继续受其端点、必填属性、物理归属及循环等约束限制。例如 joins_with 必须提供属于两侧 Table 的完整 Column 对，不能用自定义机制绕过其验证。未定义的名称不会自动获得 JOIN 等执行行为。

joins_with 身份使用无向的完整列对，反向和重排列对不会产生新事实。整合复用同一身份函数并保留来源；完全重复事实合并。不同属性证据版本仍按既有 Merge 协议保留，不能混为两条相同事实。

## 5 统一接口和生命周期

GraphManager.apply 同批支持 create/update/delete_node 和 create/update/delete_edge，处理最终状态后原子提交。拒绝返回 code、path、hint 和可定位到操作的反馈。

Learning 使用 edit_graph.upserts 写语义节点，edge_operations 写显式关系；revert_ids 撤销暂存变更。物理 catalog 的修改权限仍属于确定性代码。

GraphSnapshot 保存完整 graph，以及编写输入 assets、catalog_edges、explicit_edges；不提供缺少完整图时从 refs 重新生成的读取兼容入口。显式边与节点一起进入指纹、diff、分组、评估、接受和撤销。

Agent 可以通过 values 提议 column_id/value；代码核对数据库中的实际类型和值，生成规范 ID、归属及可信来源，然后经 GraphManager 写入。原始 DimensionValue 节点不能直接作为 Agent upsert。尚未接受的 Value 和其他暂存内容一样可以丢弃。

Agent 节点读取和下游 handoff 同时包含相连 relations。类型 guide 说明默认关系；relations guide 展示内置约束以及自定义关系规则。Assessment 读取关系前后版本，而不把 refs 当作实际连接。

## 6 消费和测试

定义依赖沿正式关系展开，包括覆盖默认关系的自定义边。独立自定义边在端点被保留时随图提供，不会仅凭未知名称自动扩张依赖。目标和冲突分支选择仅过滤持久化边。

回归覆盖默认生成、局部覆盖、默认恢复、锚点更新清旧边、target 歧义、无 schema 扩展、自定义依赖检索、在线投影不编译 refs、分支选择、整合去重、快照恢复、Agent Value 验证及非法操作回滚。

历史快照若缺少当前图结构或关系元数据，应重新生成；不增加旧映射关系转换或 adapter 兼容入口。


## 7 根 schema 与旧格式清理

内置 cortex_root.json 只定义 6 类 NL2SQL 节点和 13 个内置关系。已删除 Example、KnowledgeUnit、Entity、Event、Process、通用 Rule 的正式图定义，以及仅适用于这些类型的 uses、constrains_metric。related_to 的内置端点收敛为 Concept。图外 QA/SQL 示例证据仍由既有训练流程保存。

Metric 正式字段与当前编写模型一致，仅为 references、pattern、expression、sql_generation_plans；expression 必须为对象，不接受旧字符串表达式。GenerationRule 删除旧 sql_trigger、nl_triggers 字段，只使用当前 triggers 等字段。Column 正式归属使用 table_ref，不再提供重复的 references 字段；Concept 不保留未使用的 scope 字段。

删除了旧 Metric physical_dependencies 丢弃、过滤条件折叠、旧维度/时间字段清洗、旧 Rule relations 转 refs，以及冲突 field_paths 丢弃、impact 改名等转换函数。旧字段通过当前 schema/模型验证明确报错。plans 省略时采用空列表，显式 null 不再静默转为空列表。

删除已停用的 JoinPath 节点输入模型、枚举和工作区读写入口，以及未使用的 GraphFragment 编译封装。当前 joins_with 关系、JOIN 路径检索和 SQL 生成仍保留。自定义 relation 依然无需修改根 schema。

这是 schema 指纹发生变化的不兼容更新：旧正式图版本、训练快照和 checkpoint 应重新生成，不提供运行时迁移入口。

## 8 跨入口校验与读取完整性

根 schema 的物理 owner、Rule 必填字段与写入要求一致。Metric 表达式及依赖闭包通过共享内容校验；正式图也检查 plan ID/长度、Concept 重复 target 和完整 Rule 内容。检索仅接受当前嵌套 triggers，不转换旧字符串或数组。

快照读取同时检查指纹、GraphManager 图约束，以及保存节点与 assets、非默认 relations 与 catalog_edges/explicit_edges 的逐项一致性。读取只转换节点编写属性作比较，不编译关系或补边。保存 explicit_edges 时保留规范后的锚点标记和 target_id，删除 anchor 时一并删除过期 override。

已保存的锚点关系必须有实际存在的 anchor，冲突分支范围必须有效且覆盖对应定义；default 标记不能用于自定义关系。某分支的显式 override 只替换该分支的默认关系。在线投影对所有带 target_id/alternative_ids 的关系过滤范围，未限定范围的独立关系保持独立。物理图检查和最终审计也识别合法的自定义归属覆盖。

Agent 只提交关系事实；reference_origin、sources、alternative_ids 由代码维护，越权提交明确拒绝。代码生成的来源、分支范围和锚点元数据随 relations 持久化，不增加新的图内注册表或 adapter。

训练服务在调用抽取引擎之前也通过 GraphManager 校验仓库返回的完整状态，不能仅依赖具体仓库或后续 merge 拒绝非法图。删除未使用的 CaseRetrievalGraphBuilder：该旧入口仅凭 assets 重建检索图，会遗漏已保存的显式关系；训练检索继续使用 GraphStore 中的正式图。

## 9 候选定义压缩与目录整理

候选定义现为公共节点内容 + alternatives[].overrides；不再持久化完整 alternative.content 或合并审计 consolidation。分支关系仍以 graph.edges 为准。详细结构、读取方式和格式变化见 [候选定义规范](compact-conflicts.md)。schema 目录已移出检索算法、Agent 编写规范和文档加载 DTO。

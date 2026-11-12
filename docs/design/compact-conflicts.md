# NL2SQL 图结构与候选定义

当前可执行结构见 `src/cortex/schema/graph.py`，六类节点及十三个内置关系见 `src/cortex/schema/roots/cortex_root.json`。机器可读的图状态 JSON Schema 见 [graph-state.schema.json](examples/graph-state.schema.json)，包含物理端点和分支关系的完整示例见 [compact-conflict.json](examples/compact-conflict.json)。JSON Schema 检查字段形状；端点类型、完整候选、依赖闭包、分支及其他跨对象约束由 GraphManager 检查。

## 存储结构

```text
GraphState
  graph.nodes[]                 完整节点，唯一冲突存储位置
    id / node_type / 公共内容
    merge_status                normal 或 conflict
    consolidation_status        仅conflict：semantic_conflict 或 unresolved
    preservation_reasons[]
    alternatives[]
      alternative_id            稳定候选ID
      overrides                 按完整字段替换的差异
      sources[]                 候选自身的证据来源
    conflict_points[]
      point_id / aspect / description
      choices[]
        alternative_ids[] / statement
  graph.edges[]                 分支边带 properties.alternative_ids
```

`semantic_conflict` 表示已确认的不同业务口径；`unresolved` 表示目前无法可靠统一，不能自动解释成多个业务政策。conflict point 解释已知业务分歧，不是生成候选内容的补丁，也不是当前问题的在线 ChoicePoint。后者仍由在线流程按当前问题生成。

不再保存 `alternative.content`、conflict 内的 `consolidation` 审计过程和 choice 的重复 `source_case_ids`；来源从候选 sources 获取。审计中的 proposal、review、调用次数和停止过程仍在 merge 工作流/trace/checkpoint 中，不进入正式图。`merge_status` 保留为 GraphSpace 子图消费者识别候选节点的标记，与节点内 alternatives/status 一致性校验；它不表示已经选择某个候选。

## 公共内容与覆盖

例如销售额的名称、描述、pattern 相同，公式及引用金额列不同：公共节点仅保存名称、描述和 pattern。两个候选分别覆盖 `properties.expression` 和 `properties.references`，不复制其他字段。

规则固定如下：

1. 同一记录的所有候选覆盖相同的一组字段；至少有两个不同候选。每个覆盖字段至少有两个不同值，否则应回到公共内容。
2. 允许 `name`、`description`、`aliases`、`tags` 和 `properties.<字段名>`；不允许修改 ID、类型、来源、深层对象路径或列表下标。
3. 对象与列表整体替换。属性覆盖为 null 表示该候选不包含此可选属性；必填属性不能因此缺失。
4. 差异属性从公共 properties 移除。顶层字段发生差异时使用结构占位：name 为节点 ID，description 为 null，aliases/tags 为空列表；具体内容全部在候选 overrides 中。共同名称等字段不变。
5. GraphManager 还原每份完整 NodeContent，并校验 schema、嵌套内容、引用、关系、循环及身份。公共节点可以缺少各候选才有的字段，不能作为默认执行定义。
6. 修改公共内容会影响所有候选，因此要重新校验整个状态；普通 Agent 不能直接覆盖一个仍有候选的节点。
7. 去重候选会合并来源，并优先保留已有 alternative_id；再次整合不能无故更换稳定 ID。

空覆盖不会被当成“默认方案”。公共内容只保留共同部分，所以每个分支都明确给出全部差异字段。代码不在读取时重新生成 relations。

## GraphManager 接口

```python
# 确定性生成/整合：输入完整候选，立即压缩为公共内容和差异。
inline_node, proposal = GraphManager.compact_conflict(
    node,
    [(candidate_id, node_content, sources), ...],
    consolidation_status="semantic_conflict",
    conflict_points=points,
)
state, dropped_edges = manager.prepare_state(candidate_state)

# 默认查看：公共内容 + 紧凑差异。
manager.get_node(node_id)
manager.get_conflict(node_id)  # 同一个内联GraphNode的读取视图，普通节点返回None

# 明确选择后查看完整候选；ID 和类型仍属于同一个 ontology 节点。
manager.get_node(node_id, alternative_id="gross")

# 内部算法需要完整内容时统一还原，完整状态在边界校验。
GraphManager.resolve_alternative(inline_node, alternative)
manager.validate_state(state)
```

有候选的 existing graph 参与 fragment 整合时传入完整 GraphState，保留候选上下文；纯物理 GraphSpace 仍可直接作为端点目录。

## 使用方式

- 合并 Agent 的默认输入是 `group.common` 与 `members[].overrides`，来源与输入成员 ID 保留。完整生成结果仍必须作为 NodeContent 返回，由代码压缩保存。
- 依赖读取、在线发现、下游 guidance 和 HTML 默认展示公共内容与差异。合并只读工具可以按明确请求读取完整输入成员。
- 检索内部使用还原后的完整定义建立可搜索文本；选择 Agent 接收紧凑 evidence，避免重复发送公共描述和 plans。
- 在线选择后投影还原选中的完整候选，并筛选已保存的分支 relations；未选择时不建立默认解释。
- 分支 relations 保存在 `graph.edges`，例如 gross 对应 `grounded_on -> gross_amount`，net 对应 `grounded_on -> net_amount`，各边带自己的 alternative_ids。

## 目录边界

`schema/` 只保留图结构、类型定义、字段/引用/关系规则与校验：graph、models、roots、references、relationships、physical_joins、semantic_validation、validation。

- JOIN 路径算法：`capabilities/retrieve_db/join_paths.py`。
- Rule 检索信号：`capabilities/retrieve_db/rule_signals.py`。
- Concept grounding 查询：`capabilities/retrieve_db/concept_grounding.py`。
- Agent 编写规范：`capabilities/sql_training/llm/prompts/asset_contract.py`。
- 文档加载 DTO：`application/sources.py`。

已删除未使用的 schema/retrieval.py 和 graph_merge/references.py 兼容转发入口。旧模块路径不保留 re-export。

## 格式变更

GraphState bundle 新写入版本为 `graph-state-v3-inline`，仅保存 nodes.jsonl 和 edges.jsonl，候选及冲突点已在节点中。旧 v2 bundle 和 `{graph, node_conflicts}` 仅在导入边界解析一次，下一次保存写新格式；不回写sidecar。旧 content 格式仍需要显式审阅适配，不能猜测转换。merge、训练和在线工作流版本均升级，旧checkpoint明确拒绝恢复。

`GraphState.model_fields` 只有 graph；`state.conflict_nodes` 返回已有节点对象，不生成另一份存储。`GraphNode.with_conflict()`、`without_conflict()` 执行完整状态切换。`NodeMergeConflict` 仅作历史导入和临时操作提案；只读历史属性可从节点派生，不进入序列化或JSON Schema。`graph_io.py` 现在负责历史导入与严格导出检查，导出直接序列化同一组节点，没有内部外部冲突格式转换。

## 对外事务写入

`GraphManager.apply` 现支持两种 conflict 操作，复用同一 expected_revision、request_id、权限检查、校验和 commit 回调：

```python
manager.apply([
    {"operation": "set_conflict", "node": inline_node.model_dump(mode="json")}
], expected_revision=revision, request_id="set-sales-v1", commit=repository.commit)

manager.apply([
    {"operation": "resolve_conflict", "node_id": "metric.sales", "alternative_id": "net"}
], expected_revision=manager.revision, request_id="resolve-sales-net", commit=repository.commit)
```

set_conflict 接受完整内联节点；旧node+conflict操作载荷仅作为命令输入兼容。它可以创建一个新候选节点，或者替换现有节点/现有候选记录；旧载荷的 node.id 与 conflict.node_id 必须一致，既有类型和物理精确身份不能改变。resolve_conflict 必须选择现有候选，不接受隐含默认或任意替代内容。一个批次中每个节点只能有一项操作，其他节点/边可以同时写入，支持前向引用；验证任一部分失败或 commit 失败都不更新 manager 状态。

旧分支事实随分支删除；改过引用的显式覆盖不会转移给其他候选。选择后保留该候选和共享事实，清理 alternative_ids；Concept target 限定的独立事实也按选中定义清理。默认关系只在写入阶段生成。选择同一引用的某个分支时，默认关系来源收敛到该分支。

Agent 使用现有 `edit_graph(conflict_operations=[...])`，与 upserts、edge_operations、values、proposed_groups 共用一次原子发布。操作结构同上，但不提供 node.sources、node.merge_status 或 alternatives[].sources，由受信服务补充。每个还原候选也要通过现有编写约束，包括 Direct Rule 的 Question trigger 来源检查。

Working/Accepted 快照的冲突只保存在 graph.nodes，普通authoring assets不会另外保存冲突候选。冲突参与内容指纹、diff、分组、接受、丢弃和重开；resolve 后恢复普通 authoring asset。inspect_graph 的 node/diff 模式默认显示公共节点和 overrides。对冲突节点使用 set_conflict/resolve_conflict，不通过普通 upserts 或逐节点 revert 改写候选；控制器 retain 仍可丢弃整个暂存版本。

训练 case 的 ontology.json 仍是单定义 authoring export；尚有 accepted conflict 时明确拒绝封存，要求先 resolve，不会把候选静默漏掉。这不限制 GraphManager 或 GraphState repository 保存未解决的正式图。

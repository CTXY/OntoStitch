# NL2SQL GraphManager 设计

本地图统一通过一个 GraphManager 读写。确定性代码与 Agent 工具调用相同入口；manager 检查请求、维护节点与边的一致性、提交合法修改，或拒绝并返回可供外部调整请求的 feedback。本文替代此前分散的 graph operations、adapter、handler registry 等架构建议。关系含义见 [正式关系规范](nl2sql-ontology-relations-v1.md)。基础实现已接入当前运行时，以下仍区分已实现接口和后续迁移。

## 1 职责

```text
Agent 工具 / 数据目录导入 / Merge / 评估脚本
                     ↓
                 GraphManager
          读图 / 检查 / 修改 / feedback
                     ↓
              现有本地快照存储
```

GraphManager 对外只有一个修改入口，内部使用普通 Python 构造和校验函数。它不调用 LLM，不替 Agent 猜测业务含义，不建设工作流 DSL。manager 是服务对象，不是图里的一种节点。

图保存节点、边、必要业务属性、来源和现有冲突信息。schema 定义类型与字段；输入解析、构造、校验、权限和 Agent 指南属于代码，不保存成节点或边的 authoring_adapter、content_model、contract、checks、operations 字段，也不需要行为注册框架。

合法性不等于业务正确性。结构、引用、依赖环和已实现约束可以确定性拒绝；来源是否证明某个业务口径、SQL 是否正确仍由外部学习和评估流程判断。accepted/working 与训练验收机制保留，不因 manager 校验通过就自动进入永久知识。

## 2 最小接口

```python
manager.get_schema(node_type=None, edge_type=None)
manager.get_snapshot()  # 当前 GraphState 的隔离副本；manager.revision 是版本
manager.get_node(node_id, alternative_id=None)
manager.get_conflict(node_id)
manager.list_nodes(node_type=None, offset=0, limit=20)
manager.get_edges(node_id=None, edge_type=None, direction="out")
manager.get_dependencies(node_id)

result = manager.apply(
    operations=[...],
    expected_revision="r17",
    request_id="edit-42",
)
```

精确参数以实现时的类型模型为准。读取返回快照或不可变视图，不能通过返回值原地修改内部状态。缺少节点、截断和未决冲突应明确返回，不能伪装成完整可执行知识。

普通 operations 为 create_node、update_node、delete_node、create_edge、update_edge、delete_edge。update 第一版接收完整内容替换，不设计任意 JSON Patch。create/update 的 ID 与类型规则明确检查，不自动把错误操作变成 upsert。

派生边不可直接 create/update/delete；应修改权威节点内容，由 manager 在同一事务维护。显式 JOIN 等关系可以直接编辑，但仍检查端点、完整列对和来源。工具只暴露该角色允许的操作；actor、来源与权限上下文由受信工具/服务注入，不接受 Agent 在请求里自报权限。

Merge 在外部完成身份判断与内容合并，其最终节点、引用 remap 和冲突变化通过同一个 apply 事务提交。Learning Agent 可以通过 edit_graph.conflict_operations 提交 set_conflict 或 resolve_conflict；GraphManager 在同一事务内维护 conflicts、merge_status 与分支关系，来源由受信服务补充。没有第二套 conflict 写入服务。具体 payload 和约束见 [候选定义规范](compact-conflicts.md)。

## 3 修改流程

1. 检查 request_id 回执、expected_revision、操作权限和输入格式。
2. 在独立候选状态中应用整批节点和显式关系修改，批次内新节点共同可见。
3. 为新增/修改节点同时构造派生边；移除它们不再需要的旧派生边，保留显式关系。
4. 校验最终状态的精确属性、实际引用类型、悬空引用、Metric 环、完整 binding、JOIN 列对、删除依赖和现有冲突约定。
5. 全部通过才按 revision 原子提交；任一规则失败，整批拒绝，原图不变。

例如公式由引用 Metric 改为引用 Column，同一事务更新公式、删除旧 depends_on、增加 grounded_on。调用方不需要再触发 adapter 或第二次关系同步。构造逻辑用共享模型与普通函数实现，不需要 handler registry；正常读取不重建图。

第一版做最终状态的深层校验，避免只检查 changed owners 而遗漏 Column 归属、Table 键变化对既有 JOIN 的影响。删除检查还覆盖 formula/binding/Rule 引用、JOIN 内部 column_pairs 和冲突 alternatives。默认不级联删除业务知识，允许同一批次明确更新依赖后删除目标。

## 4 Feedback 协议

结果分 applied、rejected、error：applied 表示提交成功；rejected 表示请求违反规则且整批未提交；error 表示系统执行失败，不能伪装成业务规则违反。

每个问题提供稳定 code、operation_index、字段 path、准确 message、必要 details 和可执行 hint。Agent 据此修改请求或重新查询，不只接收一个 ValueError 字符串或堆栈。

以下为引用不存在节点的返回示意，ID 仅为示例：

```json
{
  "status": "rejected",
  "revision": "r17",
  "errors": [
    {
      "code": "REFERENCE_NOT_FOUND",
      "operation_index": 0,
      "path": "node.properties.expression.refs.base",
      "message": "引用 metric.missing 未在当前图或本批次创建节点中找到。",
      "details": {"reference": "metric.missing"},
      "hint": "查询已有 Metric 并使用其实际 ID；若确实缺少该指标，可在同一批次创建完整依赖节点。"
    }
  ]
}
```

hint 解释如何满足规则，不替 Agent 编造正确 ID、创作节点或静默修正公式。候选 ID 必须来自真实可见图。字段错误用 Pydantic 的结构化 loc/type 生成反馈；图规则直接生成稳定问题码，不从异常英文文本反向猜 code。

| code 示例 | 应说明的内容 |
| --- | --- |
| INVALID_FIELD / UNKNOWN_NODE_TYPE / UNKNOWN_EDGE_TYPE | 错误位置、预期字段/可用类型；新关系不能临时自创 |
| REFERENCE_NOT_FOUND / INVALID_ENDPOINT_TYPE | 哪个引用不存在，或实际类型为何不兼容 |
| METRIC_DEPENDENCY_CYCLE | 造成环的实际节点路径 |
| INCOMPLETE_BINDING / INVALID_JOIN_COLUMNS | 缺失内容或不属于 JOIN 表端点的列 |
| NODE_HAS_DEPENDENTS | 阻止删除的实际 owner/关系 ID；截断必须标记 |
| DERIVED_EDGE_NOT_EDITABLE | 应修改哪个 owner 的哪项内容 |
| OPERATION_NOT_ALLOWED | 当前角色不允许的操作及允许入口 |
| REVISION_MISMATCH | 当前 revision，提示重新读取后提交 |

格式损坏时不继续执行图规则。可安全检查的独立错误可以聚合并限制数量；不报告由前一个错误引发的虚假连锁错误。

系统错误记录日志并返回安全 error_id。若提交结果暂时无法确认，不声称原图一定未变；调用方使用相同 request_id 查询或重试，以回执确认结果。rejected 才保证此请求没有部分写入。

## 5 当前代码接入

| 当前位置 | 修改方向 |
| --- | --- |
| learning/tools.py::edit_graph、graph/mutations.py::apply_edit | 调 manager.apply，将 rejected.errors 交给 Agent；保留目标分组和 edit_id 语义 |
| graph/snapshots.py::GraphStore | 工作区读写经 manager；accepted/working 仍由训练 controller 决定 |
| graph/nodes.py、schema/relationships.py | 生成时构造节点，GraphManager 统一生成和校验关系；adapter.py 已删除 |
| graph_merge/operator.py、edges.py | 外部完成语义整合后通过同一 manager 提交；退出第二套关系同步 |
| schema/validation.py 与训练 contracts | 复用已有规则，补深层模型和统一引用检查；返回结构化 issues |
| builders/catalog.py、pipeline/physical.py、值物化 | 可信生产者也走 apply；证据预先收集，不绕过校验写图 |
| case 输出、检索、评估与消融脚本 | 通过 manager 读快照和子图，不在导出时重新推导关系 |
| artifacts/graph_state.py | 延续 repository，由 manager 控制提交；锁内检查 revision，验证成功再切 CURRENT |

GraphManager 位于 src/cortex/application/ontology_graph.py。精确模型与结构函数放在 cortex.schema，磁盘代码继续放 infrastructure。仅新增必要类和结果模型，不按操作拆成大量 service、adapter 或 plugin。

先把现有校验、写入和 feedback 收到同一入口，保持字段、来源和冲突协议；再统一 NodeAsset/GraphNode 内容与引用校验，调用方迁移和 adapter 删除现已完成。当前训练模型从 ID 前缀判断类型，而 Merge 可使用其他 canonical ID，统一时必须改为真实节点索引校验。

Metric 重复 refs、merge_status、mapping 属性精简及新增关系另行迁移，不作为建立 manager 的前置条件。旧数据确需复用时写一次性转换脚本，不建设常驻 importer 框架；已经丢失的引用不能猜测恢复。

## 6 最小验收

- 合法批次同时写入节点及相应边，查询立即可见。
- 非法字段、悬空引用、错误端点、依赖环和非法删除返回有定位与修正提示的 rejected，整批不变。
- 更新/回退不留下过期派生边，不误删显式关系。
- 训练、Merge 和确定性生产者走同一入口与校验；Agent 不直接写 repository。
- revision 冲突和相同 request_id 重试行为明确，失败不发布半成品。
- 校验通过不等于 SQL replay 成功或训练验收通过，外部流程保留自己的判断。

## 7 当前实现

GraphManager、GraphIssue/GraphResult 已在 application/ontology_graph.py 实现。普通 CRUD 只允许六类 NL2SQL 节点；通用 root 中的 Example 等声明不自动成为可写节点，旧状态的受信兼容处理暂保留。apply 为隔离批次修改，commit 回调可对接现有 repository.commit；manager 的 request_id 回执在实例内保存，训练仍保留现有 controller/episode 回执与恢复机制，并未新增持久化回执系统。

确定性生成、训练快照和 Merge 共用 schema/relationships.py 的同一实现。训练快照新增已生成 graph 与 graph_fingerprint，正常回读不再投影；旧快照读取允许兼容重建。Agent edit_graph 返回拒绝反馈，支持临时节点删除，inspect_graph 增加 edges 模式；episode 将 rejected 标记为错误工具消息，提示 Agent 修正。

NodeGraphAdapter 及 adapter.py 已删除，不提供兼容入口。物理初始化、case 检索、训练输出、实验脚本统一使用普通构造函数和 GraphManager。build_incoming_graph 在生成时分配 case 独立 ID，重写明确引用；prepare_fragment 利用已有节点校验开放片段并生成关系，再交给 Merge。已有物理节点的引用由表名、列名、owner 和 value 等结构化属性解析，不再根据语义名称、sources 或第一个 case 猜测。缺失或歧义引用直接拒绝。NodeAsset 暂作为训练输入 envelope，不写入 adapter 注册信息，也不产生 identity_map / graph_projection 存储。

本地 repository 支持 load_snapshot、带文件锁的 commit CAS，并在切换 CURRENT 前验证候选 generation。manager 不替代外部训练验收与业务证据审核，root 中的兼容字段和现有冲突协议继续保留。


## 8 实现复查与入口约束

本轮复查发现此前“已统一”的表述覆盖不足：旧 case compiler 仍会用 ID 前缀构造占位端点，Merge 输入与 repository 直接提交也只有基础 schema 检查。现已删除 graph/relationships.py，不保留这个入口；关系推导仅保留 schema/relationships.py 的一份实现，由 GraphManager 调用。

| 路径 | 当前入口与职责 |
| --- | --- |
| 物理初始化、case 快照生成 | build_graph → GraphManager.prepare_state，构造时同步派生边 |
| case 导出与评估 | project_case_graph → prepare_fragment，传入实际物理图，不伪造端点 |
| 训练输出 | build_incoming_graph → prepare_fragment，隔离 case ID 并重写明确引用 |
| Merge | existing 经 validate_state，incoming 经 prepare_fragment；整合结果经 prepare_state，返回前再次 validate_state（含 checkpoint 恢复） |
| Agent 编辑 | GraphEdit 做输入与工作流权限处理，GraphManager.apply 做原子图操作；GraphStore.save 再验证保存内容 |
| Agent 查询 | GraphStore.read 验证缓存图；dependencies / referenced_by / edges 使用 manager 查询。NodeAsset 展示格式仍由工具层处理 |
| 本地持久化 | repository.commit/save 与 load 都经 validate_state；发布前再次读取验证 staged generation，再切换 CURRENT |

prepare_state/prepare_fragment 用于生成，可以替换过期派生关系并合并重复的派生边来源。validate_state 用于读取、持久化和返回结果，只检查、不修复：缺失、多余、过期或重复派生边均拒绝。构造暂存 GraphNode/GraphSpace 本身不代表图已通过检查或已经发布。

本轮补齐的约束包括：规则 SQL trigger 列、表键和默认/重要/时间列等结构化引用的存在性及字段归属；Column/Value 必填 owner；精确身份不可通过 CRUD 改写且正式图不可重复；节点删除覆盖辅助引用及 JOIN 列对；边更新同时检查原 owner 和新 owner 的权限；严格 CRUD 拒绝不完整规则；当前对象公式、target、SQL plan 与 trigger 的嵌套字段验证；Concept target 的完整引用与 source-local 限制；普通物理 JOIN 不接受自连接；指标依赖与概念层级环；单个冲突 alternative 对稳定依赖图的环检查；CRUD 拒绝覆盖未解决 alternatives。冲突内容重写只修改 properties 中的明确引用，不修改 SQL 或说明文本。

Agent 启动 contract 的关系端点、字段、方向来自实际 schema，managed_from/editable_from 按起点类型来自同一派生关系集合和调用权限；没有新增行为 registry。GraphManager 负责结构合法性，SQL replay、证据完整性、业务语义与训练验收仍由既有流程负责。多个冲突节点之间的具体分支组合仍由现有冲突选择协议处理，不把 alternative 并集当作一个已确认的执行图。

Concept target 已精简为 id、sql_expression、references。全部声明引用生成带 target_id 的 grounded_on，旧映射边、组合算子、target 自动转换及在线重复编译器已删除。retrieve_db 沿完整的已存边读取依赖；在线普通投影不编译，变更定义后通过 GraphManager 重建临时图。端点/辅助引用拆分不再是迁移任务。具体含义与不兼容范围见 [当前关系规范](nl2sql-ontology-relations-v1.md)。

历史复核遗漏的 online_context/output/projection.py::_compiled_edges 已移除。关系推导只保留 schema/relationships.py，由 GraphManager 调用；消费者不再自行根据节点类型创建边。Schema 变化后旧图、快照和 checkpoint 需要重新生成，不提供旧字段或旧边的自动转换入口。

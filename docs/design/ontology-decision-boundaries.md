# 语义匹配、使用决策与输出形式

日期：2026-11-10。本文补充 ontology graph 与 merge 设计；实现测试结果另行记录。

## 1. 所有者与准入

| 问题 | 所有者 | 边界 |
| --- | --- | --- |
| 业务含义由什么数据表达？ | Concept 定义、targets、关系 | 静态术语到字段/值的匹配不重复建 Rule |
| 请求使用哪些匹配、如何使用？ | 必要时 GenerationRule | 必须是匹配定义尚未解决、有证据且改变回答的独立选择 |
| 已选内容如何呈现？ | 有明确依据的呈现 Rule 或通用任务约定 | 列顺序、分列/拼接与字段/对象范围不同；NLQ 是否显式提及不决定类别 |

`targets` 是支持的物理表示集合，不是默认 SELECT 列表。外层多个组不自动表示任选、全部返回、合并或补空；内层联合匹配不规定拼接、列顺序或输出行数。角色、必要条件由定义和关系说明。不得从字段后缀推断主次角色。

Rule 准入同时要求：有证据支持的候选行为、语义或任务结果上的后果、正确 Concept/Metric 定义仍不足以确定该选择、一个最小完整 action。不存在额外选择时完善 Concept，不因多 targets 自动创建 Rule。静态绑定竞争留在 Concept，生成行为竞争留在负责该行为的 Rule；不为制造冲突而增加规则。

`column_binding_choice` 可以选择一个字段、联合字段组或多个表示，决定参与回答的内容；不负责字段顺序和格式，不重复已确定的静态映射。相同 intent 是检索信号，不是 identity。

## 2. Action 最小化

检查行为能否独立改变：选择哪些内容、怎样呈现、是否过滤行。若其中一个可以改变，而另一个仍完整、可执行且可独立评估，则是不同决策。必要实现步骤可以共存；SQL 操作数或标签数不决定原子性。

拆清决策不等于为每一项创建节点：静态匹配归 Concept，常规行为可不抽取，只有通过准入的独立行为成为 Rule。不能把字段选择藏在排序、筛选或格式化 action 中；必要的业务操作数仍可保留。常规 NULL 传递、Gold 中未出现某个过滤器，不自动产生额外 Rule。

## 3. 同一身份与局部决策重叠

Identity 比较业务指称、数量或独立生成义务，不按标题、主导 SQL 操作或标签判等。相同决策选了不同字段集合，可能是实现差异而非身份不同。

复合 Rule 与另一条 Rule 仅部分重叠时，不强行整体合并。Identity 在 distinction 理由中指出重叠义务及差异，由 Integration 使用现有编辑、重写对应和 alternatives 处理。整体 identity 不同不证明其内部行为可以无冲突并存。

代码通过显式 applies_to 及其操作数的声明引用召回候选，并将共享 intent 作为弱排序信号。只读声明引用，不解析 SQL/字段名推断身份，不扩大为依赖推理闭包。候选检索仍分页；Agent 可继续读取完整源图与证据。

## 4. 相容性与冲突

令 d 为一个独立业务决策，C_i(q) 为规则适用条件，A_i(q) 为要求的行为。需要审查竞争实现的条件为：

\[
d_i=d_j \land \exists q\,[C_i(q)\land C_j(q)\land
\neg\operatorname{Compatible}(A_i(q),A_j(q))].
\]

这是 Agent 的语义审查标准，不是代码可仅凭 SQL 文本、相同 targets 或 intent 标签判定的定理。说明重叠请求范围和具体不相容行为；不能虚构请求或业务策略来制造冲突。

- 同义/兼容/互补：整合或保留角色差异。
- 有证据支持且运行时可识别的条件分界：条件化保存，不编造 else 分支。
- 同一决策、条件重叠、行为竞争且没有可靠分界：保留完整 alternatives、来源和分支关系。

case ID、偶然地点/日期过滤、字段后缀、臆造的 primary 角色不能消除竞争。不同 SQL 写法或任务允许的多种等价结果不自动构成冲突。不同表示可以互补，但合并 targets 不等于已决定请求该选哪些表示。

## 5. Assessment 的职责

内容质量只分两层：节点自身（类型、匹配、决策价值、原子性、条件/行为、证据），以及相关知识之间（相同决策、条件重叠、相容性、重复与保存）。SQL 应用和回归仍沿用现有独立检查。

每条规则符合自己的 Gold，只证明各有来源支持，不能证明可同时成立。检查完整 action，而非沿用作者的标题或 Identity 的结论。对复合行为分离后仍需保留的选择差异，明确指出相关节点及义务。使用现有 graph_consistency、decision_check 和 issues/issue_refs，不新增审查对象或存储类型。

合并成功需同时满足机械检查与语义检查。完整来源映射、正确序列化或模型 accept 不单独证明冲突处理正确。真实实验应保存运行前预期、源图、模型轨迹、最终图及 repository round-trip；零冲突不能计为成功验证冲突分支存储。

## 6. 实施范围

共享节点契约和 operation registry 供 Direct、Repair、Assessment 复用；Merge 的 Identity、Integration、Assessment 对齐以上边界。保持既有节点/边 schema、图编辑工具、alternatives 格式及 source correspondence；不新增 case-specific 分支、确定性语义裁决或全图两两比较流程。

## 7. 实验中发现的恢复边界

外层 case checkpoint 是图版本与执行位置的恢复依据。Learning episode 不继承外层的子图 checkpointer：基础设施中断后从外层保存的图版本重新执行该 episode，不能续用旧对话中的成功写入回执。否则对话与已回滚的 GraphStore 不一致，可能错误地交付空图。成功完成的外层步骤仍复用检查点；这不改变语义验收门槛。

编辑工具的错误反馈必须指向具体操作及字段，并提供对应操作的最小合法形状。例如删除边只接收 operation 和 edge_id；不能笼统地要求 Agent 再读一遍完整规则。格式错误不应改变图，也不能被当作语义评估失败。

## 8. 冲突发布与持久化

若 alternatives 覆盖 name，公共节点的 name 是其 ID 占位符。内部短 ID 转为公开 ID 时，占位符必须同步变更；普通名称、分支名称和业务文本不做字符串替换。真实冲突的验证应覆盖发布、保存、重载和分支还原，而不只检查 Agent 返回的 JSON。

模型服务中断前的候选可以单独导出供审查，但必须保留 Assessment verdict 和中断原因；机械保存成功不将 revise 候选提升为已接受结果。


## 9. 业务指代、直接属性归属与合并重检

Concept 的业务指代与物理表示分开判断。School 可以由编码或名称表示；School administrator 可以由同一槽位 first/last name 联合表示。名称、定义、tags 与关联边的业务角色须一致，不从表示字段强制创建名称属性节点，也不以姓名相同推断人物身份唯一。若场景独立讨论姓名本身，则可保留独立属性 Concept。

属性关系应连接属性直接描述的对象或有证据支持的适用子类。管理员邮箱归属于管理员；charter number 应按其业务含义组织到 chartered school，而不是仅因字段存于 schools 表就归属于所有学校。此处是语义示例，不是代码判定规则，不自动断言每个实例有值、属性只属于一个对象，或源数据不存在异常。

令 D(c) 为节点业务指代。保留边 (u,r,v) 必须满足契约 Valid_r(D(u),D(v),scope)。R(A,B) 与 attribute(B,P) 不推出 attribute(A,P)。没有禁止跨层边、环或共享属性的通用规则；可推导冗余仅按已接受契约与相同作用域判断。

has_attribute 只作为抽象基类。实际边必须复用/声明具体业务谓词，可选 extends=has_attribute；不再使用泛化属性边兜底。description 解释本次业务联系及必要限制，不能只说明字段存储位置。旧图保持可读取；新编辑与合并影响范围内的结果禁止直接 has_attribute。

节点合并或指代调整后，Integration 复查其全部入边/出边及对端含义，决定保留、改谓词、重接或有据删除，通过既有 changes 保存来源处理。Assessment 仍采用节点自身与关系/全图两层，不增加独立检查类别。代码只检查抽象谓词、结构和来源，业务归属由 Agent 判断。

## 10. 多轮上下文

各 merge 阶段使用新的对话；只传递当前候选、明确的来源视图、必要反馈和对应关系。read_ids 表示跨阶段已读取授权，不等于当前模型看过该节点。候选工具仅对当前阶段初始载荷/工具响应已包含的源节点省略重复定义；新阶段必须重新提供未载入的源定义。输入与候选版本保持独立，不能因 ID 相同省略不同版本。

初始关系页与关系读取工具采用相同的稳定排序：业务节点间关系优先，物理绑定随后。保留总数和分页标记，不以首屏缺边推断关系不存在。Assessment 对所有实际改边保持完整展示，未改关联边按同样优先级分页。

## 11. Two-stage merge implementation

2026-11-12: production merge uses matched-node consolidation with independent Node Assessment, followed by Graph Reconciliation that inspects and repairs structure. See [two-stage contract](ontology-merge-two-stage.md) for role inputs/tools/outputs, buffered repair, intermediate versus final validation and logging. A graph rewrite that changes core node semantics or declared dependencies invalidates the prior node review for those objects and triggers targeted reassessment. No additional mandatory Graph Assessment role is introduced.

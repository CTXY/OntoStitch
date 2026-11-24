# Ontology 结构与保义要求（拟议语义契约）

本页定义 Reconciliation 需要遵守的图语义，供 review；不定义新工具字段，不改变现有代码或存储格式。下面明确区分**当前结构硬约束**、**业务语义保义要求**与**仅针对新建/实质重构的质量要求**。新 canonical draft 的操作包装由主设计另行规定。

已接受节点是整合基线。关系协调不得对它们重新执行 extraction 准入审计；不能因为 Gold SQL 未投影某字段、没有逐字说明某个理由，或当前查询没有使用某知识，就删除其已接受含义。若本轮修改确实暴露了具体矛盾、丢失或契约问题，必须定位该问题，不用风格偏好要求重写整图。

## 1. 三层要求各约束什么

| 层次 | 需要保证 | 不应扩张为 |
| --- | --- | --- |
| 当前结构硬约束 | ID/类型/字段/引用合法；使用真实物理端点；公式与依赖符合契约；完整分支可解析；metadata 引用有效；满足当前 Concept grounding 项目政策 | 这些格式检查不证明业务含义、来源或泛化正确 |
| 业务语义保义 | 原知识、适用条件、角色、计算、关系、配对及来源在实际候选中得到保留；修改有明确依据 | 不要求保留重复措辞、临时 ID、逐 case 摘抄或唯一写法 |
| 条件性新建/重构要求 | 对新增含义或实质改变的部分，说明新身份、范围、可匹配性、计算/生成行为和关联依据 | 不重新审计未变的 accepted 内容，不强迫每个旧节点补充来源从未承诺的单位、NULL、唯一性或时间粒度 |

结构检查通过只是必要条件。独立 Assessment 必须比较**实际完整候选**与输入知识，不能仅检查提交者的 explanation、引用数量或 route 是否填满。

## 2. 节点与关系的基本结构

| 对象 | 语义与结构要求 |
| --- | --- |
| 物理节点 | Table、Column、DimensionValue 是真实 catalog 对象；匹配引用使用其规范 ID。关系 Agent 不创建虚构列、改 catalog 类型或把 SQL 别名当物理节点。 |
| Concept | 一个清楚的业务对象、类别、属性或术语；同义词放 aliases，表示角色写进定义/说明；至少一套完整物理匹配方法。 |
| Metric | 一个业务计算口径；公式、直接操作数、范围与已承诺的粒度/单位/NULL 行为一致。不同表达式是否等价需要依据。 |
| GenerationRule | 一个可复用的 SQL 生成决策，保留适用条件、完整 action、reason 与检索信号。一个必要实现可以有多个步骤。 |
| Event | 一个业务发生类型，保留发生含义及参与者、时间/地点、作用对象之间的关联。不能仅因共用时间列或参与者认定是同一事件。 |
| 普通边 | 显式保存 id、edge_type、from/to 和 properties；每条边有非空 description，说明业务联系及必要范围。谓词两端类型必须符合 schema。 |
| 带 alternatives 的节点 | 仍是一个身份；alternatives 是该身份的完整竞争版本，不是自动建立的独立 GraphNodes。细则见 [alternatives.md](alternatives.md)。 |

完整 `NodeContent` 包含 name、description（可 null）、aliases、tags、properties 五项；物理 ID、来源对象和版本信息不混入 content。业务别名不证明不同角色等价，物理唯一键也不自动证明现实对象唯一。

### 同一已有身份的完整内容修订

拟议 canonical 契约用 `node_rewrite` 统一普通→冲突、冲突→冲突、相容分支→新综合普通内容，以及选已有分支普通化。它固定已有 node_id / 身份，输出完整 content 或完整 alternatives，并给出真实 source routes；不创建 alias，不把不同身份塞进同一 owner。

相容互补分支合成新普通内容时，所有仍有效旧单元都保留到普通结果；选择已有分支时也需明确处置其他旧分支，不能按当前偏好丢掉仍有依据的竞争知识。normal/common 单元不得 discard。

这是一条**拟议接口路径，尚未由当前 compiler 实现**。需要受信任的单 owner adapter，不让模型伪造单成员身份组或通过退役重建 owner 绕过保义。普通化后，旧分支限定关系不能机械去 scope 广播；必须验证条件、消费者和 metadata 在实际候选中仍成立。详见 [alternatives.md](alternatives.md) 的统一重写路径。

## 3. Concept：定义、匹配与绑定分别处理

### 3.1 `targets` 的明确含义

`properties.targets` 是二维列表：

- **每个外层项是一套完整、可能适用的匹配方法**。
- **每个内层列表是该方法所需的联合物理端点**，不能把它拆成相互独立的方法。
- “完整匹配”说明这套方法的信息组成，并不保证其结果现实唯一；例如名称可以匹配多个同名实体。
- 多个外层项不表示必须选择全部列、不表示 SQL SELECT 投影，也不自动表示 UNION、JOIN 或任意互换的计数操作数。
- 不同相容表示可共存；真实冲突的完整解释则保留 alternatives，不能把冲突扁平成无条件匹配组并集。

以下是当前已有 schema 能表达的完整 `NodeContent`。**示例前提**：来源已经确认同一学校实体可通过代码、名称或“省份＋本地编号”三种方法匹配；不从字段名自行推断这个事实。

```json
{
  "name": "学校",
  "description": "学校业务实体；代码、名称和省份内编号是不同匹配方法，不据此假定名称唯一。",
  "aliases": ["School"],
  "tags": [],
  "properties": {
    "definition": "保留代码标识、名称表示与省份内编号的不同角色。",
    "targets": [
      ["column.schools.code"],
      ["column.schools.name"],
      ["column.schools.province", "column.schools.local_number"]
    ]
  }
}
```

这里第三项是一套联合匹配，不能拆为单独省份与单独编号。也不能把三项全部拼成一个内层列表，要求同时选择四个字段。

### 3.2 匹配表达式与去重

`target_expressions` 以当前外层下标的规范字符串为键，例如 `"0"`、`"1"`。组排序、合并或去重后必须同步重编号，不能让旧表达式错绑另一组。

结构去重至少比较**组内端点集合＋该组完整表达式**。相同端点但表达式不同，不是简单重复；表达式文字不同是否等价，属于需证明的语义判断。相同完整组及相同表达式不重复保存。

以下示例前提是来源明确说 completed 与 closed 都表示该业务的完成状态；两项是相容匹配。它们共用 status 列，但条件不同，不能只按端点删除一项。若来源实际上对两个代码的业务含义存在冲突，不能照抄此合并结果。

```json
{
  "name": "已完成订单",
  "description": "来源确认 completed 和 closed 两种状态代码均表示业务完成，分别保留其匹配条件。",
  "aliases": [],
  "tags": [],
  "properties": {
    "definition": "保留来源确认的两种完成状态表示，不将其他状态纳入完成订单。",
    "targets": [
      ["column.orders.status"],
      ["column.orders.status"]
    ],
    "target_expressions": {
      "0": "orders.status = 'completed'",
      "1": "orders.status = 'closed'"
    }
  }
}
```

表达式使用实际目录 SQL 表列名，或表达式中真实声明的别名；graph ID 是引用句柄，不是 SQL 标识符。不得引用组外物理输入、删掉关键条件再宣称等价，或把匹配条件只留在 explanation。

### 3.3 匹配不自动生成 grounding

`targets` 记录“如何匹配”，`Concept --grounded_on--> physical` 是显式的直接物理表示断言；两者有关联，但不是一一对应的生成规则：

- 有效匹配组必须保留，即使没有对应直接边。
- 不要求每个 Concept、每个 target group 或每个物理端点都有 grounding 边。
- 已写出的 Concept grounding 端点必须出现在其适用完整版本的匹配方法中。
- 修改直接边不授权删除其他有效 targets；targets 重合也不证明两个 Concept 是同一身份。

**现有端点格式与新建质量要求分开**：当前存储/graph schema 可接受 Table、Column、DimensionValue 物理引用。若新建/重构的具体匹配要求更精细的 Column / DimensionValue，应为该新含义提供充分匹配，不将某阶段的细化要求倒用于重新审计所有已接受 Table 引用。

### 3.4 “direct owner”只是旧称：当前单直接绑定项目政策

本项目旧文档的“direct owner / 规范 owner”仅指：**一个 Concept 通过 grounded_on 直接连到某个物理节点**。它不是新节点类型，不表示业务上的拥有、主体归属或唯一现实所有者，也不规定所有业务属性只能属于一个实体。

**当前代码政策必须如实说明**：同一个 GraphState 中，按 `physical_node_id` 全局记录 Concept 来源 ID；一个物理端点最多接受一个**不同 Concept ID** 的直接 grounded_on 来源。GraphManager 当前不按业务条件、适用范围或 alternative 分支判断两个 Concept 的边是否可同时成立。

因此，两个不同 Concept 即使声明了互斥条件/分支，直接连同一物理 ID 仍可能触发 `DUPLICATE_CONCEPT_GROUNDING`。来自同一 Concept 的不同分支边不会因为 Concept ID 相同而触发这条“不同 Concept”限制；Metric 等其他语义类型的物理输入也不计入该 Concept owner 表。

这是**现存项目政策，不是普适 ontology 原则**。当前文档/实现不悄悄把它放宽成“仅同范围相容分支最多一个 owner”。代码定位：[ontology_graph.py:782](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/application/ontology_graph.py:782)。

| review 选项 | 必须考虑的影响 |
| --- | --- |
| 保留当前政策 | Prompt 明确全局按物理 ID 判重。不同业务 Concept 可保留重合 targets；直接表示、业务角色和业务属性联系分清。若修复需调整 grounding，说明真实语义后继及来源，不仅为过校验而删边，不按共享 targets 强行合并身份。无法同时保义与满足政策时保留未发布状态并报告政策冲突。 |
| 未来按范围/分支放宽（**尚未实施**） | 先定义 scope/分支跨 owner 的可比较性和同时成立规则，不能只比较各 owner 的本地 alternative ID 字符串。再统一 GraphManager、预检/repair、signals、owner lookup、检索与 SQL grounding 消费者、缓存/候选绑定、checkpoint 契约及回归。不能只改 Prompt 或删除一条 if。 |

放宽政策也不意味着所有重复直接边自动正确：真实含义、范围、targets、来源及消费者处理仍要成立。保留政策也不能当作抹掉不同业务角色的理由。

### 3.5 `relation_metadata` 嵌在 Concept 的 properties 内

它表达**多条已经存在的相邻边的组级含义**，例如共同成立、角色组合、材料性顺序或有据的重叠/覆盖。它不是独立节点、不是另发一份的输入对象、不是没有真实边时的替代关系，也不会因一段 description 自动成为可执行公理。

结构硬约束：每组 id 在该 Concept 内唯一；edges 至少两个不重复的真实相邻边；同谓词、相对 owner 同方向；作用域可共同成立。不能把互斥分支的边凑成组，也不能把入边与出边混成同一方向的组。

示例前提：来源已经确认“随访研究患者”同时属于患者及随访参与者两类，下面只展示其合法 NodeContent。组说明嵌入 properties。

```json
{
  "name": "随访研究患者",
  "description": "研究中接受随访的患者；保留患者身份与随访参与者身份的共同成立。",
  "aliases": [],
  "tags": [],
  "properties": {
    "targets": [["column.Patient.ID"]],
    "relation_metadata": [
      {
        "id": "joint_membership",
        "edges": ["edge.study_patient_is_patient", "edge.study_patient_is_participant"],
        "description": "该研究对象同时属于患者与随访参与者，两项分类须共同保留；不声明两父类等价、互斥或穷尽。"
      }
    ]
  }
}
```

对应两条边的现有 `GraphEdge` 内容如下；实际完整图中必须存在这三个 Concept 和物理引用。

```json
[
  {
    "id": "edge.study_patient_is_patient",
    "edge_type": "is_a",
    "from_node_id": "concept.study_patient",
    "to_node_id": "concept.patient",
    "properties": {"description": "该随访研究患者属于患者类别。"}
  },
  {
    "id": "edge.study_patient_is_participant",
    "edge_type": "is_a",
    "from_node_id": "concept.study_patient",
    "to_node_id": "concept.followup_participant",
    "properties": {"description": "该随访研究患者属于随访参与者类别。"}
  }
]
```

边重定向、合并或删除后，metadata 也必须重新核对实际候选。精确边别名可由代码映射；如果两条边去重后只剩一条，不能静默删掉组级知识，也不能留两个相同 ID 冒充有效组。必须保留其实际语义并由独立 Assessment 判断修改是否成立。

## 4. 其他类型的结构与保义重点

| 类型 | 当前结构硬约束 | 保义要求 | 仅对新增/实质重构部分追加的质量判断 |
| --- | --- | --- | --- |
| Metric | expression 对象为非空 expr 与 refs；`${key}` 占位符集合与 refs 键一致，引用真实节点；不以裸列/恒等包装伪装计算。完整图中每分支的直接依赖精确对应 refs：Concept/Metric 用 depends_on，物理输入用 grounded_on。 | 保留实际运算、操作数角色、范围、计算顺序及已承诺的单位/粒度/NULL 行为；SUM(a/b) 不能自动改成 SUM(a)/SUM(b)。未知操作数等价性不能靠邻居将来可能合并而假定。 | 新计算应有完整业务口径和有据实现；必要时按业务含义分解，不按公式长度/SQL 子句数强拆。不能为通过类型校验随意添加 SUM/COUNT。 |
| GenerationRule | condition/action/reason 非空；triggers 只能使用 question.phrases 与 sql.columns/operations，至少一个有效信号；可选 operation_intents。 | 条件与完整行为配对；理由、操作次序、计数对象、去重、并列策略等已知承诺保留。检索信号命中不等于执行所有分支。 | 新/重构规则表达一个可独立变化的生成决策；一个必要实现可含多步，不把其依赖暗推给未定义“上游”。参数化要有来源支持及可还原的绑定。 |
| Event | definition 非空；targets 可选，有则遵守完整匹配/索引约束。 | 保留发生含义和角色间关联，不把原有发生判据改成孤立日期/ID；同日期/参与者不证明相同发生类型或同一实例。不跨分支拼接参与者与发生条件。 | 新建/重构事件需要发生含义及参与者、时间/地点等必要角色的实际关联依据；不要仅凭动词/日期列/JOIN 建立事件。未变旧事件不因本轮未投影所有角色而被重审。 |

这些是结构/保义说明，不要求每个输入都额外填写所有业务维度。来源未指定的单位或 NULL 策略不能由 Agent 补造。

## 5. 关系与整体组织要求

| 关系/行为 | 语义边界 |
| --- | --- |
| is_a | 同范围内类别集合包含；不等于业务 identity、对象归属或物理映射。同范围、分支相容时可传递，不复制 targets，不假设兄弟互斥/穷尽。 |
| depends_on / grounded_on | 区分 Metric 直接语义输入与物理输入；间接路径不能替代公式的直接边。不因存在有效 targets 就自动造 grounding。 |
| applies_to | Rule 实际作用对象与条件关联，不是无条件触发，不沿层级广播全部 action。 |
| 具体业务关系 | 谓词、方向、两端角色、条件与必要限制有据。使用当前有效的具体谓词；抽象 has_attribute 不是可直接输出的业务边。新谓词的定义/登记由主接口契约规定。 |
| belongs_to / value_of / joins_with | 物理 catalog/已确认连接事实；不证明业务 identity、唯一性、必须匹配或 SQL JOIN 类型。 |
| 去重 | 代码只机械去除规范端点、谓词与完整属性都相同的重复边并保留来源；其他语义合并需要明确比较范围、角色与保留图。 |
| 删除/重构 | 每条实质知识都在候选中的保留断言、明确后继或有据纠正中得到交代；不能靠相同端点、来源数量、当前偏好或未投影内容判删除。 |
| 一般图结构 | 普通业务图不要求普遍无环；类别层级、非递归计算等按各自契约处理。不能把所有三角形/平行边/环当错误。 |

边上的业务角色/条件写在具体谓词与 description 的现有表达能力内，不私造 `role` / `condition` / `qualifier` 等不受支持字段。边 description 是语义说明，不自动成为 SQL 执行条件或可计算的跨 owner scope。

## 6. 当前代码保证与仍需 review 的边界

1. `validate_graph_state`、完整 `GraphManager.validate_state` 和业务 Assessment 是不同层。完整发布层还检查严格 owner 政策、公式直接依赖、类型/引用与循环；局部结构校验不是保义证明。
2. 当前 Concept grounding 的分支 targets 检查使用适用版本的 targets 并集；它不能充分证明某条无 scope 边在**每个**分支都合法。业务契约仍要求逐适用分支成立，详见 alternatives 文档；若未来补强 validator，应作为显式代码/迁移变更。
3. metadata 校验只证明基本引用、谓词、方向和已有分支 scope 相容；不会证明 description 中的“共同成立/顺序/覆盖”业务断言。
4. 物理 owner 政策没有计算自然语言条件互斥、跨 owner 分支相关性或数据库上下文。将其改写为普适“Ontology 唯一所有者”会误导 Agent。
5. 新抽象/参数化不得以压缩为由删除固定含义；现有 schema 无法表达所需内容时报告具体契约缺口，不能悄悄扩展 JSON 字段。

代码依据：[targets 校验 semantic_validation.py:152](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/schema/semantic_validation.py:152)、[metadata relation_metadata.py:12](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/schema/relation_metadata.py:12)、[Metric 依赖 ontology_graph.py:499](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/application/ontology_graph.py:499)、[Concept grounding 与 owner 政策 :763](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/application/ontology_graph.py:763)。

节点退役必须有真实保义后继及node_unit_routes逐单元路由；空after仅用于有据错误边弃用，不用于整个已接受节点。

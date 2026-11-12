# Ontology graph 结构与约束设计草案

> 历史提案，未采纳其中独立 Constraint 类型的设计。本轮决定不新增 Constraint，以关系 description 说明业务联系与限制，结构要求由 schema/validator 执行。参见 [当前设计规范](ontology-graph-design.md) 与 [实施方案](ontology-graph-implementation-plan.md)。下文保留用于追溯，不作为当前实施依据。

本文提出在当前实现上统一管理节点、关系、物理绑定和 Constraint 的设计。Constraint 用于表达业务语义中必须成立的限制，可以涉及节点、关系、属性以及多个元素组成的模式。本文是设计草案，尚未改动运行时实现。

## Constraint 的职责

Constraint 回答“在什么范围和条件下，哪些状态必须成立、哪些组合不允许成立”。它独立于某次 SQL 的生成步骤，也不必归属于单个节点或单条边。

例如：订单在当前业务快照下必须且只能关联一个客户；同一客户在同一有效期内不能同时属于两个互斥等级；某种关系成立时必须同时具备另一种关系。

Constraint 保留领域限制的语义，不自动退化为对当前图中可见边数的检查。图中的业务概念通常描述类别，实际客户和订单记录仍在数据库中。检查定义、检查具体业务数据和指导 SQL 消费需要分别绑定相应能力。

Generation Rule 描述怎样构造查询；Constraint 描述哪些状态可以接受。Constraint 不应在 requirement 中塞入一串查询生成步骤。

## Constraint 的统一结构

建议作为与 nodes、relations、bindings 并列的可引用对象保存。每个对象具有自己的 ID、来源、版本和生命周期。画布上可呈现为节点，但不要求它成为普通业务 NodeType。

| 字段 | 含义 |
| --- | --- |
| id、name、description | 稳定身份、可读名称、准确说明 |
| kind | 已注册的约束种类，决定 requirement 的精确结构 |
| scope | 领域模块、数据源或业务上下文，以及检查对象和粒度 |
| target | 目标选择器；支持具体元素、类型或带角色变量的关系模式 |
| applies_when | 适用条件；不适用时不施加该限制，不能与必须满足的内容混写 |
| requirement | 结构化限制，引用 target 中已绑定的角色；含义由 kind 定义 |
| explanation | 为什么有这个限制、违反时意味着什么 |
| sources | 支持这项声明的文档、数据库约束或样例证据 |
| version、lifecycle | 版本及 proposed、active、deprecated 等维护状态 |

初期支持 cardinality、mutual_exclusion、requires 等少量明确种类。每种 kind 注册参数 schema、语义说明、抽取和合并 guidance、正反例，以及可用的 checker。扩展通过注册新种类完成，不允许随意在 requirement 中写无法解释的对象。

下面是建议格式，表达一项业务数据层面的限制；不是现有工具已支持的输入。

```json
{
  "id": "constraint.order_customer",
  "name": "订单客户唯一性",
  "description": "当前业务快照中，每个订单必须且只能关联一个客户。",
  "kind": "cardinality",
  "scope": {
    "module": "sales",
    "subject": "business_instances",
    "grain": "per_order_per_snapshot"
  },
  "target": {
    "anchor_type_ref": "concept.order",
    "relation_type_ref": "relation.has_customer",
    "direction": "outgoing"
  },
  "applies_when": {"kind": "always"},
  "requirement": {
    "count": "distinct_targets",
    "min": 1,
    "max": 1
  }
}
```

concept.order 和 relation.has_customer 必须是正式声明的语义类型与谓词，不能从名称猜测其含义。若有多条物理实现，应选择适用的 binding 后检查；不能直接数 Concept 旁边画了几条关系定义线。

对于多元素约束，target 用一个有角色名的模式声明参与者、连接和共享变量。例如，互斥等级约束需要共享 customer 和 valid_time 角色。只存一组无角色的 refs 无法表达“同一个客户、同一个时间”的要求。涉及元素索引应由 target、条件和 requirement 生成，不再人工维护另一份权威 refs。

## Constraint 的呈现

| 视图 | 呈现内容 |
| --- | --- |
| 全局约束目录 | 名称、自然语言限制、适用范围、类型、来源、生命周期 |
| 节点和关系详情 | 展示相关约束，并标明当前元素承担的角色 |
| 图画布 | 约束卡片通过带角色标签的虚线连接多个参与者；点击高亮完整模式 |
| 检查详情 | 当前检查对象、适用上下文、结果、证据和无法检查的原因 |

默认展示自然语言、作用对象和条件，精确结构按需展开。视图辅助连线表示参与角色，不冒充业务关系。约束卡片中的来源结论与当前检查结果分开展示：active 表示正式知识，不表示任何一次检查已通过。

## Constraint 的作用与检查结果

Constraint 同时服务于知识抽取、合并和消费，但每一步只执行其明确支持的能力。

- 抽取时，帮助 Agent 判断关系和定义是否符合已知业务限制；新限制必须有与其声明强度相匹配的证据。
- 合并时，检查适用范围是否一致、限制是否冲突；不能把“最多一个”与“至少一个”自动合并成“恰好一个”。
- SQL 消费时，帮助解释语义和选择合法实现；只有经过有效数据源检查的唯一性等信息才可用于相应优化，不能因描述存在就删 JOIN、去 DISTINCT 或改变聚合。
- 检查时，按明确的对象执行：ontology 元数据、绑定后的业务数据或查询表示。checker 的支持范围单独登记。

结果使用 satisfied、violated、unknown、not_applicable，并附 constraint ID、检查上下文和证据。未知来自缺少 binding、信息不完整或不支持的检查能力，不能视为通过。检查器执行失败作为单独的执行错误记录，不伪装成领域违反。

SQL replay 成功不能单独证明一个全局业务限制。来自一条 NL/SQL 的候选约束应保持 proposed，直到有足够来源支持。业务假设与数据库实际数据不一致时记录差异，不能静默删除限制或修改数据。

## 当前实现边界

| 内容 | 当前实现 | 问题或边界 |
| --- | --- | --- |
| 节点与边类型 | `src/cortex/schema/roots/cortex_root.json` | 已显式列出类型，但嵌套结构与行为定义不集中 |
| 资产结构 | `capabilities/sql_training/models/` | Pydantic 比 root 中 object 等声明更精确 |
| 关系生成 | `capabilities/sql_training/graph/relationships.py` | 部分 refs 按目标类型生成边，无法表达同类型节点间不同关系意图 |
| 语义契约 | `schema/ontology_asset_definitions.py` 和各角色 prompts | 分散维护 |
| 图校验 | `schema/validation.py` | 已有字段、端点、冲突及专用校验，不等于完全没有 constraints |
| 规则相关定义 | root 中 `rule`、`constrains_metric`、`computed_from.constraint` | 不构成统一约束声明、发现、检查与报告流程 |
| 图存储 | `infrastructure/artifacts/graph_state.py` | schema hash 与快照完整性检查已存在，跨 schema 迁移另需设计 |

本项目 Column 节点通常描述数据库字段，不代表字段中的每个数据值。检查 ontology 元数据、数据库行和生成的 SQL 是三种不同的校验任务。

## 统一管理的建议结构

建议以一个版本化 OntologyDefinition 作为统一入口，允许内部按类型拆文件。下列是目标设计，不是已存在的接口。

```text
OntologyDefinition
  metadata                 ID、版本、依赖、内容哈希
  node_types               类型、精确 properties schema、identity、语义说明
  relation_types           显式语义、端点、方向、属性、身份、来源规则
  constraint_kinds         约束种类及参数、语义、操作指南和检查能力
  constraints              具体限制、作用域、条件、来源与版本
  bindings                 语义对象到数据源的实现
  capabilities             各类型已实现的读取、编辑、合并和检查能力
```

统一入口不要求所有内容写在同一个文件。精确结构可以沿用 Python 和 JSON Schema，语义、guidance 和 examples 通过 registry 关联。未知的约束种类拒绝写入；有效的约束可以先有语义说明，但必须标明尚不支持自动检查。

## 节点类型的管理

现有 root 的全部类型需要显式登记用途，避免把“schema 中存在”等同于“Learning Agent 可以生产”。

| 分组 | 当前类型 | 建议管理方式 |
| --- | --- | --- |
| 数据目录 | table、column、dimension_value | 数据库事实导入，记录声明或验证证据 |
| NL2SQL 语义 | concept、metric、generation_rule | 明确编辑、校验、合并及消费契约 |
| 样例 | example | 验证过的任务与 SQL，作为证据或示例 |
| 通用知识 | knowledge_unit、entity、event、process、rule | 独立可选模块，逐类标记运行时支持能力 |

Constraint 统一保存为可引用对象。类型上的简单 required 等结构要求可由 schema 维护；独立领域限制由 Constraint 维护。重复展示的约束必须标明原始定义来源。

## 关系定义与实例

RelationType 是谓词定义；Relation 是一次具体断言；GraphEdge 是二元关系实例的存储表示。同一对节点可有多个不同谓词，同一谓词也可有多个带不同限定条件的实例。

每种 RelationType 至少登记：

- `id`、准确语义、正反例及所属模块。
- source 和 target 类型；明确这是校验限制，还是逻辑上的 domain 和 range。
- 方向；inverse、symmetric、transitive 等逻辑特性仅在有实现时启用。
- 精确的 properties schema 和参与身份的限定字段。
- 证据要求；允许人工、数据库导入或哪一种编译器生成。
- 可支持的检索、合并、删除及 Agent 操作。

最大最小连接数等要求由 schema 或独立 Constraint 明确声明，不能把 Table 关系上的数据基数 `1:N` 与图中每个节点允许多少条边混为一谈。

### 现有关系的完整处置目录

| 现有谓词 | 语义与后续处置 |
| --- | --- |
| belongs_to | Column 所属 Table；唯一归属由相应结构约束检查 |
| value_of | DimensionValue 所属 Column；值与字段的对应需要校验 |
| joins_with | 有证据支持的完整物理等值连接列对；不隐含业务查询必须选它 |
| aligns_with | 按声明粒度对齐聚合结果；与明细连接区分 |
| depends_on | Metric 对 Metric 的计算依赖；与一般关联区分 |
| computed_from | 先审计与 grounded_on 的重叠及现有消费者，再决定保留或迁移 |
| analyzable_by | 可分析维度的正向声明；未声明是否禁止必须另行规定 |
| filters_on | 对某字段的过滤绑定；操作符及值由精确结构定义 |
| maps_to_column | Concept target 绑定 Column，保留 target 和组合语义 |
| maps_to_value | Concept target 绑定维值，保留 target 和组合语义 |
| grounded_on | 物理落地依赖；不能表示“必须使用”或替代约束 |
| applies_to | Generation Rule 的适用对象；运行时 condition 仍需判断 |
| expands_to | 审计与当前 Concept 物理 target 模型的关系，再明确支持范围 |
| uses | Example 使用的资产，属于证据与示例关系 |
| broader_than | 概念层次；是否允许多父和环必须显式规定 |
| related_to | 弱语义关联；不能承担未注册的任意可执行谓词 |
| constrains_metric | 旧 Rule 到 Metric 的限定关系；待明确 constraint 模型后迁移，不能直接当成完整实现 |

### 声明来源与唯一权威表示

对人工或 Agent 创作的业务关系，写入显式 `relation_type/source/target/properties`。源和目标类型只验证兼容性，不决定谓词。

公式等精确结构可产生派生关系。例如 MetricExpression 的 typed operand 可以编译出计算依赖，但必须登记编译规则、版本和输入路径。派生边不可直接编辑；修改其权威输入后重新生成。

每种事实只有一个权威来源。显式关系、公式内引用和便于检索的依赖边之间必须规定归属，不能保留两份可独立写入的 refs 与 edges。平面 refs 可作为计算结果返回，不再承担任意关系的创作协议。

多元关系或带组合条件的关系不能拆成几条边后丢失共同作用域。Concept target、多列 JOIN 和多目标约束均需要稳定的组合标识或独立关系对象；是否提升为节点按身份和生命周期需要决定。

## 扩展与 Agent 接入

新增关系先注册语义、端点、属性、证据和执行能力，再允许进入正式图。扩展包使用稳定命名空间与版本依赖；Agent 可提交候选谓词，但不能在正式图中静默创建未经注册的新关系。

从统一定义生成三类视图：创作工具的精确 schema，Merge 的身份和合并契约，下游查询的相关资产与适用约束。角色提示词保留操作流程，避免重新手写一份类型定义。

检索需要同时考虑依赖闭包与约束目标，不能只沿旧 refs 找节点。投影必须保留 target、JOIN 或约束的完整组合结构，并携带 ontology definition 的版本与 graph revision。

## 实施顺序与验收

1. 建立现有类型、关系、约束种类和能力的统一目录；确定各字段和关系的唯一权威来源。
2. 引入显式关系的创作协议与受控派生规则，提供旧 refs 的兼容读取和迁移报告。
3. 加入 Constraint 模型及 registry，优先支持基数、互斥、依赖三种限制；每种带精确 schema、description、抽取和合并 guidance、正反例。
4. 选取有真实业务依据的约束，贯通声明、相关元素检索、Agent 注入、展示和至少一种明确检查能力。
5. 扩展多参与者模式及更多约束种类；为旧快照提供明确的版本迁移路径。

验收包括：约束可独立存在并涉及多个元素；参与角色和共同作用域不丢失；同类型节点可显式表达不同谓词；未知谓词和约束种类被拒绝；派生边无法独立篡改；正式图与 fragment 使用不同完整性边界；约束检查可定位对象和来源；未知和执行失败不会误报通过。

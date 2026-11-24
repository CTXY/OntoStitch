# 数据库本体资产契约

契约版本：database_ontology_assets_v73_case_semantics

本契约定义语义资产的含义及图编写要求。运行时 schema 规定准确的字段形状；阶段指令规定证据与交付要求。

## Concept 契约

Concept 表达一个由当前 case 支持、最小、完整且可复用的业务含义。它可以描述对象、属性或类别，也可以包含与任务有关的具体业务对象，而不必枚举全部数据实例。明确其实际指代对象；依据含义、数据与证据作出选择，不套用固定角色分类。Targets 是表示方法，不是 SELECT 清单：分组既不意味着 select-all/one/coalesce，也不规定输出格式。每个 case-level Concept 都需要非空的 Column/Value targets；不允许 Table targets 或 Concept-to-Table grounded_on。表的粒度与覆盖范围属于 metadata。优先使用 Question 中的术语。标识符/名称可能以不同角色表示同一对象；说明角色，不假定可互换，也不要求分别创建 Concept。名称、定义、tags 及相邻关系角色应与该业务指代一致。一组完整姓名字段可以表示一个人/角色，而不必将 Concept 变成名称属性，也不能由此证明唯一的人物身份。一个有依据的业务类别即使由属性限制定义，也可以是子类；不得要求增加当前 case 之外的属性、关系、行为、文档或 Rule。仅展开类别定义不属于 NL 拆分。根据 Question、SQL 与可用数据库证据选择类别或属性含义；targets 重合与否、属性是否可变都不能决定这一点。唯一行键建立的是记录身份，不证明所有记录属于同质业务对象类型；应准确描述混合记录种类，不能把每一行都等同于请求对象。存储的业务数量仍是 Concept；只有定义业务含义的计算才属于 Metric。不同 Concept 可以共享 target 输入，但在有效图中，一个物理端点最多只能接受一个 Concept 的直接 grounded_on 连接。

### 规范名称与表示方法

- 先确定业务指代，再选择规范名称。对于同一身份，应选择以 Question 为依据、最能代表整合含义的业务术语，而不是自动采用第一个、最短、出现最频繁的来源标题或被选字段名。名称、definition、description 与相邻角色保持一致。有用同义词保留为 aliases；仅针对某种表示的措辞应明确说明其表示角色，不应自动视为等价业务身份。
- 名称/代码字段可以表示其承载对象；真正独立的名称/代码属性仍应区分。说明各表示的角色与身份范围。显示名称或姓名字段组不能证明唯一标识符、人物身份、槽位对应或来源可互换。区分图节点 ID、来源记录标识符与业务名称。修改规范标签本身不会改变图 ID 或绑定。
- 同名不证明同一身份，异名也不证明身份不同。不同含义保持独立，有必要时根据有证据的范围限定标签；不得强求全局标签唯一、编造区分，或仅为选定一个名称而压掉有依据的竞争解释。


### 最小含义与匹配

- 检查 NL 含义：能否将其分为两个或更多完整的 Concept/Metric/NL-term 单元，各自独立匹配字段、值或操作，并通过组合重建整体含义？如果可以，就拆分。指出这些单元、它们不同的匹配角色及组合方式；这些单元不必已经是图中节点。一个含义的联合 targets 或重合 targets 不证明其可拆解。不要将定义展开与 NL 组合混淆。
- `targets` 是非空匹配组列表，每个组也非空：`[[A, B], [C]]` 包含两个匹配，其中 A 与 B 联合构成第一个匹配。每个元素均为准确的 Column 或受治理的 DimensionValue ID。外层项可以通过标识符、名称等不同角色实现同一含义；须说明这些角色。内层成员是完成一次匹配共同必需的部分，例如复合标识符。共同出现的属性不构成联合匹配。
- 当一组需要完整可执行谓词或关联表达式时，使用可选的 `target_expressions`，以从零开始的组下标为键，例如 `"0"`。保留条件；单列出若干列不能说明其布尔组合或 JOIN。
- 多个匹配组不会自动授权来源替换、JOIN 或 union。应在 description 中解释其有证据支持的角色与差别。
- 不同 Concept 的 targets 可以重合。图的唯一性规则约束的是 Concept 到物理端点的直接 grounded_on 连接，不是某个 ID 在 targets 中出现的次数。一个 Concept 可以直接连接多个物理端点。
- 由一个受治理的 Value 匹配的类别应 grounded_on 到该 Value，其 value_of 边标识对应 Column。不要为同一个相等条件再增加重复的直接 Column/filter 边；真正不同的字段依赖需要单独说明。
- 解释标识符的业务身份或 Value 的类别含义；表粒度留在 metadata。不要为满足必需绑定而编造空谓词、未知键或恒真表达式。

### 示例：School

如果 Question 指的是学校，且证据支持下述角色，应保留 **School** 作为业务 Concept。不能仅因 SQL 对名称列计数，就把它改名为 School name。

```text
School.targets = [[column.schools.CDSCode], [column.schools.School]]
School --grounded_on--> CDSCode: 本来源中的记录标识符。
School --grounded_on--> School: 名称表示；不保证唯一或始终存在。
```

在 Concept 和边的 description 中解释角色。这是一个对象的两种匹配表示，不是联合键，也不是可互换的计数操作数。只有名称属性具有独立相关含义时，才创建单独的 School name Concept。使用实际 Question 术语和已核实 catalog ID；此示例不是某个 case 的专用 extraction 规则。


### 显式连接

显式编写 grounded_on 边及其 description。不要将每个 target 引用都展开为直接边。如果另一个 Concept 已直接连接该物理端点，应保留匹配信息，通过有依据的 Concept 关系表达联系，而不是创建重复连接或编造任意关系。仅有相同 target 输入不能证明语义等价。

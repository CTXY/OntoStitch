# Ontology Graph Standard

## 图与节点

以下节点准入要求用于新建节点或实质重构新引入的内容；已接受节点及分支是关系判断的前提，不重新审计其事实、公式、理由或原子性。

Ontology graph 可以包含业务类别、任务相关的具体业务对象，以及它们的数据库表示、计算和生成知识。它不要求枚举全部数据实例。每个节点的指代层次和每种关系的断言含义必须明确。

根据当前含义和证据理解指代，不因名称、字段值或一行记录就认定其为类别或具体对象。无需增加角色字段或新的节点基类。

- Concept 表达一个最小完整的业务含义。名称/identifier 可以表示业务对象，不必分别建立名称或编码 Concept；名称或同字段不证明同一身份。Concept 须有非空有效 targets，新建 Concept 使用 Column/DimensionValue。外层是不同完整匹配，内层是联合匹配，不自动规定选择哪些字段或如何呈现。
- Concept/Metric 的最小化按 NL 含义判断。若可拆为完整、可独立匹配的语义单元，须指出各自匹配与保义组合；多字段、长名称或展开定义/公式本身不证明可拆。
- Metric 定义真实业务计算，保留 expression/refs。存储量、恒等计算及包装整句请求的聚合组合不因此成为 Metric。
- GenerationRule 定义一个最小完整、可复用的生成选择。必要实现步骤可共存；按可独立改变的行为判断，不按 SQL 子句、intent 标签或解释条目数量判断。
- Event 需有行为、参与对象、至少时间或地点之一，且有这些信息关联到同一事件的证据；日期列、动词或 JOIN 单独不足以创建。

## 关系契约

所有边显式保存，有正确端点、非空 description 和可追溯依据。description 解释业务联系及必要限制，不堆积存储 metadata。

- is_a：可比较类别的同范围集合包含；不是具体对象归属、属性联系或物理映射。同范围、相容前提下可传递；不复制 targets 或 Rule，不默认兄弟互斥或穷尽。
- grounded_on：直接物理表示/输入，须与 targets 或公式一致；规范 Concept owner 的唯一性见下文。
- depends_on：Metric 公式的直接语义输入，须与 refs 一致。可分析间接依赖，不把间接依赖补成直接边；非递归 Metric 计算不得成环。
- Metric 的 refs 指向 Column/DimensionValue 时，用 grounded_on 表达这些直接物理输入即可；指向 Concept/Metric 时才用 depends_on。不要把合法的物理输入改成语义输入，也不要仅因没有 depends_on 就认定缺边。先核对边的实际谓词和端点类型，再判断错误。
- applies_to：Rule 的实际作用对象；必须结合 condition 与当前分支判断，不自动触发或沿层级无条件继承。
- belongs_to / value_of：Column→Table、DimensionValue→Column 的物理归属，与 catalog 一致，不代表业务归属。
- joins_with：经过确认的完整连接列对；不证明业务 identity，也不决定查询 JOIN 类型或直接连接首尾表。
- has_attribute 仅是抽象家族，不直接生成边。使用/登记具体业务谓词，明确直接业务主体、方向及范围；允许按业务含义扩展谓词，不用存储关系替代业务关系。

普通业务边按两端实际指代解释。类别联系不自动声明实例配对、必有、唯一或完整记录；具体对象的联系也不能扩大为全部同类对象的普遍事实。

relation_metadata 仅保存于 Concept，形状为 [{id, edges, description}, ...]；Rule、Metric、Event 和边不保存该字段。它说明同谓词、同相对方向的多条相邻且分支相容关系的组级含义；不是重复单边说明，不强制每组生成。描述文字不自动成为可执行公理。

## 全图组织与保存

以下要求针对同一数据库、适用条件和相容冲突分支下的有效图 G。允许多种正确组织，不以节点数/边数最少或某种固定拓扑作为目标。

### 1. 指代、绑定与关系一致

节点的名称、定义、匹配和关联角色应指向同一业务含义。已有节点确实表达所需含义时复用；不因同名、相同字段或邻居就认定同 identity。属性连接它直接描述的业务对象，不能沿普通业务路径转移归属。不能仅凭字段存储位置或非空值覆盖范围决定业务归属；也不能把“某类之外也有该属性”当作该类属性联系错误的证据。

targets 可以重合，但一个物理端点最多由一个规范 Concept 直接绑定：

\[
\forall p,\quad |\{c:Concept(c)\land grounded\_on(c,p)\}|\leq1.
\]

违反时判断应合并节点还是调整直接绑定与业务关系，不直接将两个 Concept 判为相同。

targets 是匹配信息，不是必须逐项生成的出边清单。不同 Concept 可以保留重合 targets；不要求每个 Concept 或每个 target 都有一条直接 grounded_on。移除竞争绑定时，核对保留的规范 owner、targets 和业务关系是否保存原含义；不能仅因少了一条直接绑定就拒绝，也不能要求恢复违反 owner 唯一性的边。

### 2. 层级表达真实包含，分组不附带额外假设

is_a 两端须为可比较的类别，且满足：

\[
is\_a(A,B)\Rightarrow Ext(A)\subseteq Ext(B).
\]

Ext 表示同范围内类别对应的对象集合。共同父类或共同字段不证明兄弟类别互斥、可同时取值或覆盖全部父类；必要的组级声明写入 relation_metadata，并解释范围和依据。

三角形、环、共享字段和平行关系是检查线索，不是错误判据。计算依赖必须符合非递归 DAG 要求；一般业务图不强制无环。

### 3. 允许新增抽象，但保留具体含义与适用条件

可以通过新增/调整节点和关系，将分散知识组织成共同业务维度或更通用表述。说明共同含义、所组织的具体知识，以及仍需区分的条件或实现。新增 Concept 仍须有效绑定并满足最小化。

不得仅凭结构相似就增加唯一、必有、互斥或穷尽声明；不得通过省略条件扩大行为要求或隐藏冲突。可提出符合业务含义、且不违背训练 evidence 的一般化，不要求每个重复模式都增加节点。

### 4. 去除重复和可证明冗余，不按图形删边

在同一规范端点、相同谓词含义及适用范围下表达同一事实的断言属于重复，合并时保留兼容说明及来源。仅有端点角色相同不足以合并；描述文字不同也不一定表示不同事实。

在每个相关有效范围/分支内，G 为该有效图，D 仅包含其中本次拟删除的断言，R 为已允许规则。除同范围 is_a 传递外，其他规则必须由输入明确提供，不根据谓词名称或路径自创。可推导冗余要求：

\[
\forall a\in D,\quad (G-D)\vdash_R a.
\]

即每个被删断言都能由最终保留图和允许规则推出。给出规则及保留前提，不使用被删断言自证或循环互证。公式直接输入、物理归属等不能仅因有间接路径而删除；材料性限制、来源和消费者能力也必须保存。尤其 C is_a B 时，R(B,p) 不自动推出 R(C,p)，反向也不成立；二者不是同一断言。用父类关系替换子类关系（或反向替换），须证明旧关系确实错误或按已允许规则保义，不能仅以更通用为理由。

### 5. 冲突分支完整保存、分别使用

同一业务身份或决策下，相关条件重叠而内容/行为要求不相容、又无可靠条件分界时，保存完整 alternatives、各自条件、来源及分支关系；不以宽泛表述或普通副本绕过冲突。

不同 SQL 写法或可同时成立的互补说明不是冲突。明确违反契约或与源证据矛盾的关系断言、本次整合新引入的错误可纠正/弃用，记录理由和来源去向；不能据此重新否定已接受的输入节点知识；不为保全文字强造 alternatives，也不因跨 case 差异就判定某个来源错误。

上述推导和冗余条件在各相关有效分支内检查，不能混用互斥分支的前提。只有所有相关分支均成立的结论，才能在该范围不带分支条件复用。

## Evidence 与拒绝标准

结合 Question、SQL、evidence 和已提供数据库事实判断。拒绝必须指出具体契约违反、同范围证据矛盾或材料性知识丢失。

缺少 SELECT 字段、未逐字描述、未观察到某事实，本身不是矛盾。合理解释和泛化可在训练 evidence 无反例时保留；这不等于证明普遍真理。可选抽象缺失、风格偏好或另一种有效组织不是拒绝理由。

来源覆盖不证明语义正确。保留与事实相关的归属，不因 SQL 没投影端点名称而删除来源，也不将所有原始来源广播给所有结果。


## Current records and alternative worlds

When supplied, use `source_presence` to distinguish retired archived records from live candidate objects. An archived-only node/edge is historical evidence, not an ordinary current duplicate. For a shared ID whose record changed, judge the candidate version. A missing supported claim may still be reported, but identify the missing current meaning rather than counting a retired record as active.

A conflict owner stores possible complete resolved contents, evaluated one selected alternative at a time. An unscoped outgoing edge is a common fact present in each alternative; explicitly listing all current alternative IDs means the same fact in this snapshot. Edge scope restricts a relation within a selected alternative and does not choose/activate the alternatives. A common `applies_to` edge to the same operand therefore does not execute two competing Rule actions simultaneously. Check that a shared assertion is valid in each relevant branch. Use a subset scope when the assertion holds only in that subset. Supported unresolved choices may retain overlapping conditions: preserve the explicit conflict instead of inventing disjoint triggers. Ordinary live copies outside the conflict still require the usual compatibility/preservation review. When supplied, `current_conflict_views` gives mechanically computed owner-local edge membership for this check.

Conflict storage is factored: the owner stores only common fields, while each alternative supplies whole-field overrides. A common action/reason may therefore be null, and a varying name may appear as the owner ID. These are storage placeholders, not missing required fields. Interpret the branch meaning and applicability needed by the relationship being judged. Do not fetch every complete branch or audit its reason/required fields merely because alternatives exist; read missing content only for a specific relation decision or necessary node reconstruction. Do not replace two supported incompatible actions with one common action just to populate the common record; preserve the alternatives. When supplied, current_conflict_views.resolved_content is a lightweight summary of the GraphManager expansion; omitted fields remain readable, and its active_outgoing_edge_ids gives the edges present in that selected alternative.


## Runtime field contract

The lightweight view omits unnecessary fields but never renames stored content: nodes use node_type, name, description and properties.definition/action/condition; edges use edge_type, from_node_id, to_node_id and properties.description/alternative_ids. Unshown fields are not absent. evidence_ids are transport references, not stored SourceReference objects. Read and edit exact paths; full semantic content and resolved alternatives are available on demand.

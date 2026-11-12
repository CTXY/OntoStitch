# Ontology graph 设计与抽取规范

> 2026-11-12：全局合并的当前实现见 [两阶段 merge](ontology-merge-two-stage.md)。该实现区分节点内容整合与图结构协调，复用本文的节点和关系契约；实施结果与设计要求分别记录。

> 2026-11-10 补充：[匹配、使用决策与输出形式的边界](ontology-decision-boundaries.md)。本文中的 Concept/Rule 准入、复合规则整合与相容性判断以该补充为准；实验通过与否单独记录。

日期：2026-11-04。状态：本轮关系契约与全图约束的设计规范；实施及验证状态单独记录，不以规范文字冒充已通过验证。

本规范定义 case-level ontology graph 的业务节点、数据库绑定、关系、抽取规则与验收要求。目标是提供可扩展的框架，让 Agent 在明确限制下，综合业务知识、流程、NLQ、SQL、字段说明及数据库探索，找到合理的图结构，不预设每个 case 的唯一标准图。

配套实施方案见 [extraction training 修改方案](ontology-graph-implementation-plan.md)。若历史文档与本文冲突，本轮设计以本文为准。历史实验的通过记录不构成本规范的验收结果。

授权后的代码实现与验证范围见 [修改记录](ontology-graph-implementation-status.md)，包括本轮选择的字段格式和尚未完成的真实模型验证。

## 1 范围与最终决定

首期修改 extraction training：Direct、error-driven Repair、Assessment，以及支持这些流程所必需的共享 schema、图编辑、存储和读取契约。全局 merge 算法重设计另行讨论；共享接口的必要适配不等于已经解决全局语义整合。

| 已确定的决定 | 含义 |
| --- | --- |
| 业务基类为 Concept、Metric、GenerationRule、Event | 不增加 Entity、BusinessObject、Process、Constraint 基类 |
| Concept 必须最小化且有有效 targets | 不再允许 case-level 无绑定 Concept |
| Metric 必须包含计算 | 单纯映射存储字段的业务量属于 Concept |
| GenerationRule 表达原子生成行为 | operation registry 同时用于动作划分参考与标签选择 |
| 允许新的 operation intent label | 不要求一条规则只有一个标签，不强行套用已有 intent |
| Event 需要足够的事件结构 | 不凭动词、日期或事件名称直接创建 |
| 关系显式、单独保存 | 不再通过通用 refs 猜测业务关系 |
| 每条边具有 description | 说明具体联系、角色和必要限制；不增加 qualifier 字段 |
| 业务关系允许扩展 | 新谓词随图保存类型契约，不将不同业务角色压缩为统一 has_participant |
| 不追求全图最简表示 | 只去除重复断言与有明确推导依据的冗余断言，不强制唯一层级或压缩 Event |
| 除了mapping之外不建立状态、属性、类别的固定决策树 | 由 Agent 根据含义、场景、数据、证据自主选择 |
| 不新增 Constraint 类型 | 局部业务限制放在关系描述或节点定义；结构校验留在 schema/validator |

元数据最多由初始 database profiling 与 file extraction 构建，供 offline stage 读取。整个训练流程不应进行 NLQ–SQL metadata 抽取、error-driven metadata 修订或 metadata 优化。数据库只读探索仍可用于验证 ontology 声明。metadata 功能是否独立分支或由参数控制，尚未定案；它不属于论文使用与评估范围。

## 2 全图要求

### 2.1 证据与业务一致性

节点、关系与层级必须符合业务知识，不能违背 NLQ、SQL 和已提供 evidence。NLQ 提供请求语义，SQL 提供本 case 的实现依据，字段说明与探索提供数据库解释依据；不能单凭 SQL 结构扩展出未被支持的业务事实。

业务常识用于提出候选与发现不合理结构，不能覆盖领域文档或数据证据。来源冲突时记录冲突、收窄声明或继续探索，不静默选择有利的一方。

相同业务角色的字段或类别通常应采用协调的组织方式，并明确与核心 Concept 的联系。例如两种学校分类不应仅因为 SQL 顺序不同而形成父子关系。角色相近是检查线索，不是“必须同层”的机械规则；同层也不意味着互斥。

区分类别、属性、组成、角色参与、数据库关联与计算依赖。外键、同表、共同出现或数值大小，都不能单独证明业务层级、组成或因果关系。

### 2.2 按节点内容定义最小化

**Concept / Metric：自然语言语义与匹配的最小化。** 将名称放回 NL 场景，并结合定义、数据库和证据，判断其含义能否完整拆成两个或更多可分别匹配的 Concept、Metric 或 NL term。每部分应有独立完整的语义，分别承担不同的字段、取值或操作匹配职责；若这样的拆分成立，原节点不满足最小化。拆分出的语义单元不必已存在于图中，也不必全部创建为节点；普通计算意图可留在查询生成中。

这里的“不同匹配职责”不要求 targets 不相交。一个完整术语需要多个字段联合实现、跨表身份绑定，或一个 Metric 的定义公式有多个操作数，都不能单独证明可拆分。展开一个术语的定义/公式不等于分离其 NL 组合成分；反过来，“有业务名称”或“有业务定义”也不能豁免可拆分检查。名称、NL 用法、定义和匹配共同决定边界。

**GenerationRule：action 最小化。** 一条规则表达一个最小、完整、可复用的生成行为。若 action 捆绑了多个可独立适用、纠正或复用的完整行为，应拆分；共同实现同一行为的必要步骤应保留。不是压缩字数、按 SQL 子句切割或删掉使行为可执行的前提与参数含义。operation intent 是划分参考，标签数不是动作数。

最小化与准入价值分别检查：Metric 仍须有真实计算；GenerationRule 仍须有证据支持的实现决策；Event 仍须有充分的事件结构证据。通过最小化不自动获得节点资格，也不要求 Event 为增加 SQL 操作而存在。

这些要求针对节点内容，不针对全图节点数或边数。允许多种有证据的业务组织方式，不要求树、唯一层级或 Event→属性的结构压缩；case 可以没有 Metric、Event 或 GenerationRule。全图另行检查重复断言与可推导冗余断言，不能仅以另一种表示更简单拒绝受支持的知识。

### 2.3 数据观察与声明范围

当状态与日期、记录身份、粒度、单位或关系限制不清楚时，应进行有针对性的数据库探索，检查相关分布、连接及反例。保持只读，记录可复现的查询与结果。

区分业务定义、数据库声明、全量观察和样本观察。样本上没有反例不能证明普遍规律；SQL replay 正确不能证明全局业务关系。证据不足时收窄适用范围或不建立关系，不编造缺省规则。

### 2.4 全图完整性与作用域

- 节点、边、targets、expression 引用有效；关系不仅满足节点基类，还满足端点的业务角色。
- 名称、定义、数据库匹配、公式、关系与限制一致；真实业务事实与数据库是否记录该事实必须区分。
- 同一事件的参与者、时间、地点须由对应依据关联；同表或 JOIN 本身不证明配对、事件粒度或完整历史。
- 默认作用域为当前数据库及当前生效的节点/关系定义；不能解释为所有数据库、全部历史或全体实例。更窄的条件与时间范围写在 description；发生冲突时先澄清范围，不仅按端点合并。
- Concept 连边唯一性在当前同时生效的图中检查。description 不能自行声明一个例外作用域绕过它；互斥候选分支沿用现有候选语义，跨 case 副本由 merge 对齐。
- `is_a` 表达一般集合包含；环提示等价或错误检查，不直接等于矛盾。当前非递归 Metric 计算依赖必须无环；其他关系不一律禁止环。
- 逻辑无冲突与证据正确分别评估；不能用 SQL replay 正确证明图的业务语义正确。

### 2.5 去冗余的边界

只处理两类断言，不执行全图结构最小化：

1. **重复断言**：规范化端点、谓词含义及适用范围相同。关系别名/逆关系必须已有明确契约，不能猜测。合并保留兼容说明和来源；描述不同不一定是不同事实，范围不同也不能仅按端点去重。
2. **可推导的冗余断言**：移除候选后，依据已声明规则和仍保留的事实可以推出它，且范围、限制及来源可保留。例如同范围 `A is_a B` 与 `B is_a C` 支持 `A is_a C`。缺少规则时，多条路径、相同 targets 或当前数据结果相同均不构成证明。

每项删除建议给出候选 ID、支持边及使用的规则。当前推导范围限于可核查的关系契约，不从任意 description 推出隐含公理，也不引入通用推理器。先检验图的适用范围与一致性，不能利用冲突或候选自身证明候选冗余。

删除还须保持现有消费者可用：公式需要的直接 `depends_on`/`grounded_on`、物理 catalog 归属和匹配信息不能被间接路径替代。若检索或其他消费者尚不支持必要的推导，保留物化边并报告实现限制，不能先删边再假定消费者会恢复。多个删除逐项重验支持链，不能一起删掉互相支持的事实。

结构替换、Event 压缩、全局最小节点/边数不是质量标准；允许同一业务意义有多种正确组织方式。无法证明重复或可推导时，不凭图形复杂、菱形路径或环删除知识。

### 2.6 形式化约束与检查责任

以下符号明确验收条件，不新增图字段或通用推理器。设 G=(V,E)，P/C/M 分别为物理节点、Concept、Metric 集合；b 为一组相容的候选分支选择，G_b 是相应有效视图，不能把互斥分支合成同时成立的事实。K 包含八种核心契约及本图显式登记的业务契约；历史解释契约单独标记只读。

**F1 引用与契约合法性（代码＋角色语义评估）**

```text
∀ e ∈ E_b: src(e), dst(e) ∈ V_b
(type(src(e)), type(dst(e))) ∈ Sig_K(pred(e))
NewWrite(e) ⇒ pred(e) ∈ Core ∪ RegisteredLocal
```

Sig_K 是合法配对集合，而非所有源/目标类型的笛卡尔积；is_a 只允许 Concept/Concept 或 Event/Event。业务端点角色是否正确由语义评估补充。历史读取不自动授予新写许可；Learning 另禁止新写五种已停止抽取的谓词。

**F2 Concept 匹配及规范绑定（代码＋语义评估）**

```text
∀ c ∈ C_b: |targets(c)| ≥ 1 ∧ ∀ t ∈ targets(c), |t| ≥ 1
∀ c ∈ C_b: ⋃ targets(c) ⊆ Column ∪ DimensionValue  （新 extraction 写入）
∀ c ∈ C_b: grounded_on(c,p) ⇒ p ∈ ⋃ targets(c)
∀ p ∈ P_b: |{c ∈ C_b : grounded_on(c,p)}| ≤ 1
```

统计不同 Concept，而非边数。同一 Concept 可连接多个端点，不同 Concept 的 targets 可重合。内层项是联合匹配，不能从集合并集恢复条件；边不能把一个成员冒充完整匹配。唯一性以当前同时生效图为范围，不允许 description 自行创建豁免 scope。

**F3 Metric 直接输入完整、无多余边（代码）**

令 R_S(m) 为 expression.refs 中的 Concept/Metric，R_P(m) 为其中直接物理输入。每个有效分支满足：

```text
{v : depends_on(m,v)} = R_S(m)
{p : grounded_on(m,p)} = R_P(m)
placeholders(expression.expr) = keys(expression.refs)
Metric→Metric 的直接计算依赖子图为 DAG
```

重复使用一个操作数不需要重复边；集合等式不能用间接路径代替。DAG 要求针对当前非递归计算模型，不扩展到所有业务关系。

**F4 类别包含与类型级声明强度（语义评估）**

```text
is_a(A,B) ⇒ Ext_s(A) ⊆ Ext_s(B)
is_a(A,B) ∧ is_a(B,A) ⇒ Ext_s(A) = Ext_s(B)
```

s 必须是相同适用范围，Ext 表示类别所指对象集合。相互包含提示等价检查，不自动证明节点应合并；定义及映射仍需一致。同级类别不默认互斥。一般属性/角色关系不自动声明“∀ 实例都存在属性值”、单值或必填，只允许有明确支持的更强断言。

**F5 定义、映射与证据相符（语义评估）**

```text
Accepted(f) ⇒ Supported(f, evidence, scope(f))
```

Supported 是需要给出依据的评估义务，不是已有的形式证明算法。名称、定义、匹配、表达式和关系限制不得冲突。样本结果一致不能升级为业务等价；已知冲突中的断言不能作为普遍真命题推导其他事实。数据库缺记录与业务事实不存在分别解释。

**F6 关联保持（语义评估，必要时数据库验证）**

事件角色对应 R(e,participant,time,…) 时，多个角色必须通过同一次发生/记录的证据关联；参与者集合与日期集合不授权其笛卡尔积。若声明数据层面的拆分与还原，应满足：

```text
Decode(Encode(R)) = R
```

此式要求不丢配对、不产生虚假组合。类型级 Event 节点本身不证明实例对应，也不要求把 Event 压缩为另一种更小结构。

**F7 重复与可推导冗余（语义评估）**

```text
Duplicate(f,g) ⇔ 相同规范含义、端点、角色及适用范围
∀ f ∈ D: (G_b − D) ⊢_(Σ_approved, scope(f)) f
```

D 是拟删除断言集合。第二式还要求证明前提已接受且在相关范围无已知冲突，独立来源与限制可保留，消费者仍能取得所需语义。Σ_approved 仅含明确支持的规则，例如同范围 is_a 传递；不从新谓词名字或任意 description 自动产生规则。以最终剩余图校验整批删除，不能循环自证。描述字符串相等只是重复判断的一个可检查特例。直接公式边/目录归属不能被间接路径替代；证明不明或消费者能力未知时保留，不因“可能冗余”阻断接受。

**F8 契约变更的原子接受（代码）**

设 A_k 是新登记契约 k 影响的所有边，S 是接受单元集合：

```text
S ∩ A_k ≠ ∅ ⇒ A_k ⊆ S
Install(k) ⇒ A_k ≠ ∅ ∧ A_k ⊆ S
```

边内容未变、只补登记契约也进入评估。只接受无关节点不能安装未通过的契约。

**F9 Concept / Metric 的 NL 语义最小化（语义评估）**

令 E 为当前证据及语义作用域，⟦n⟧E 为节点在该作用域中的 NL 含义。一个有效拆分见证 d 包含语义单元 u₁…uₖ、各自的匹配 μ₁…μₖ，以及组合方式 C：

```text
ValidSplit_E(n, d) ⇔
  k ≥ 2
  ∧ 每个 uᵢ 是有证据的完整 Concept / Metric / NL term 含义
  ∧ 每个 μᵢ 分别将 uᵢ 匹配到字段、取值、表或操作
  ∧ 各 uᵢ 在该 NL 组合中承担可独立识别的不同匹配职责
  ∧ ⟦n⟧E ≡ C(⟦u₁⟧E, …, ⟦uₖ⟧E)（保留原义及必要限制）
  ∧ 该分解是 NL 语义组合的拆分，而非仅展开定义、公式或物理实现

Minimal_NL,E(n) ⇔ ¬∃d : ValidSplit_E(n, d)
AcceptConcept(n) ⇒ Minimal_NL,E(n) ∧ ValidTargets(n)
AcceptMetric(n)  ⇒ Minimal_NL,E(n) ∧ RealCalculation(n)
```

“不相同的职责”不等于物理端点互斥，也不要求每个语义单元绑定一个不同 Column。单个匹配 μᵢ 可以有多个联合 targets；拆出的计算意图也可能只匹配操作。同一对象的 identifier/name 等表示不是不同 NL 含义，不能仅因物理角色不同就证明语义可拆分。原子性相对于 E 判断，不声称存在唯一、跨领域固定的 NL 原子词典。 E 来自 NLQ、Gold、metadata 与已记录数据证据；候选自行编写的名称、定义、公式或单位是待评估声明，不能反过来证明其自身不可拆分。

Assessment 发现失败时，须在现有反馈中给出 uᵢ、μᵢ、C 和证据，即指出拆成什么、各自匹配什么、如何无损组合；不能只说名称太长、公式复杂或没有信息价值。无法确定时标为 inconclusive；未找出见证不等于数学上证明不存在。Concept 使用 minimality，Metric 使用 metric_minimality；SQL 正确不能覆盖失败。此处形式化明确判据，不新增确定性自然语言分解器。

**F10 GenerationRule 的 action 最小化（语义评估）**

```text
ActionSplit_E(r) ⇔ ∃a₁…aₖ (k ≥ 2):
  action(r) 在 condition(r) 下等价于这些行为的组合
  ∧ 每个 aᵢ 都是有证据、可独立适用/复用的完整生成行为
  ∧ 不是仅把同一行为的必要实现步骤分别命名
Minimal_Action,E(r) ⇔ ¬ActionSplit_E(r)
AcceptRule(r) ⇒ Minimal_Action,E(r) ∧ Complete(r) ∧ EvidencedDecision(r)
```

Complete 表示 action 与 condition 足以说明适用前提、操作对象、所选行为及必要结果语义。复用允许参数化偶然值，并不要求删除关联的 Concept/Metric 名称。Assessment 的 atomicity 反馈应指出可分离的行为及各自适用前提；decision_value 单独评估准入价值。

以上形式化表达明确目标及责任：代码检查结构条件，Assessment 检查证据与自然语言语义。当前不提供对所有候选分支组合、任意自定义关系或自然语言限制的完备逻辑证明。

### 2.7 子类与属性的表示（2026-11-05 修订）

已回退 v56 的额外业务断言准入条件与 subclass_value 门禁。证据通常只有 NLQ、SQL 和可探索的数据库，不要求另有制度文档、专属属性、关系、动作或 Rule 才能保留业务子类。属性优先不再是统一政策。

`C is_a B` 仍要求同作用域内两端都是可比较的业务类别，且 `∀x, C(x) ⇒ B(x)`。对象群体、属性角色、取值不可偷换。类别可以用属性限制定义，展开定义不自动证明 NL 非最小；仍按原有 NL 匹配分解标准判断，而非将所有子类都改为属性。

自然语言业务含义、SQL 匹配和数据库信息共同决定表达。Charter school、virtual school 等可作为 School 的业务子类，不应因为本 case 未给出其专属行为而否定，也不从常识补造具体法规或业务约束。属性关系与子类可同时使用，但各自承担不同事实；不为凑关系重复建图。

兄弟子类不默认互斥、相交或完备覆盖父类。互斥需要同一对象/时间/定义范围内不能同时属于两类的依据；相交需要业务定义或同一业务对象的正例，单纯 AND 语法不够。数据库没有观察到交集不证明普遍互斥。未有依据时不增加对应断言，不把未抽取的分类知识作为遗漏指标。完整分类分组、跨维度兼容及关系类型继承见 v57 提案，不新增未确认字段。

## 3 节点及其要求

物理节点 Table、Column、DimensionValue 来自 catalog。四类语义节点由 Agent 按权限创建与修改。case evidence 不增加为新的业务基类，沿用现有证据存储。

| 节点 | 含义 | 必须满足 |
| --- | --- | --- |
| Table | 物理数据库表 | 真实存在，标识及来源明确 |
| Column | 物理字段 | 所属 Table 明确，真实名称与规范 ID 区分 |
| DimensionValue | 字段作用域内的具体取值 | 所属 Column、真实类型、真实值已核实；不能只有裸字符串 |
| Concept | 最小完整的业务对象、属性、类别或术语 | 定义清楚、targets 非空有效、无重复概念化 |
| Metric | 具有计算定义的业务量 | expression 有实际计算，refs 完整，口径与名称一致 |
| GenerationRule | 特定条件下可复用的原子生成行为 | trigger/condition、action、必要说明一致，动作边界明确 |
| Event | 具有参与对象及发生上下文的业务事件模式 | 行为、参与者、时间或地点等信息共同支持事件结构 |

### 3.1 Concept

Concept 至少需要稳定 ID、可读名称、description、非空 targets 与证据来源。aliases 只表达同义名称，不能用于掩盖含义差异。

不强制新增 object/attribute/state/category 的角色枚举，也不提供学校领域专用的选择决策树。Agent 自主决定抽取哪些业务含义，并通过定义、绑定与关系使其可解释。

抽取前检查：

1. 是否包含可以独立替换的地点、年份、状态、输出或排序要求？
2. 是否只是当前问题的多个筛选条件拼接？
3. 能否给出 F9 的拆分见证：每个 Concept / Metric / NL term 的完整含义、独立匹配职责及无损组合？计算意图不要求物理 targets，拆出的 Concept 仍需有效绑定。
4. 是否已有 Concept 表达相同含义？若物理端点已有直接连接，是否应复用该连接并说明 Concept 间的关系？不能只因 targets 重合就判为重复 Concept。

不是每个字段、值和短语都必须成为 Concept。需要独立业务映射、重要含义解释或业务联系时才创建。case 常量通常保留为参数或 governed DimensionValue。

例如 partially virtual charter schools in San Joaquin 不整体创建 Concept。Charter schools 与 Partially virtual schools 可以分别绑定不同字段的具体取值。Grade span served 可以保留完整含义，不机械拆词。

### 3.2 匹配分组、身份与唯一性

新的 case-level extraction 中，Concept targets 仅允许 Column 或 governed DimensionValue，不直接匹配 Table，也不生成 Concept→Table grounded_on。Table 是物理数据源，其行含义、记录粒度、主键及覆盖范围由静态 metadata 说明；未提供的业务粒度保持未知，不能由唯一键推断每行都属于某一业务对象类型。Column 的 belongs_to 已说明数据来源，不需再次给 Concept 增加 Table 匹配。

每个外层 item 表示该 Concept 含义的一次完整匹配；内层多个端点必须共同完成该匹配。不能因为字段处于同一行、共同描述同一个对象，就放在一个 item 中。多个外层 item 可以分别提供同一对象的 identifier、name 等匹配表示，必须说明各自用途和限制，不自动具有可替换性。不同物理角色本身不是拆成不同 Concept 的理由。

命名优先使用 Question 中的业务用词，不由 SQL 所选字段反推 Concept 必须改名。若 Question 讨论 schools，School 可使用 [[schools.CDSCode], [schools.School]]：前者说明记录标识作用，后者说明名称表示作用，关系描述分别写明唯一性、可选性和记录/对象/时间范围。不能把两者联合成一个标识，也不能默认 COUNT(name) 与 COUNT(identifier) 可互换。仅当名称属性本身具有独立业务含义时，才另建 School name；如另建该节点，继续遵守物理端点的规范直接绑定唯一性。若两个表的 identifier 经证据确认表示同一种对象，可写为 [[schools.CDSCode], [frpm.CDSCode]]；这不自动授权 JOIN、记录替换或假定相同覆盖范围。只有真实复合标识或联合条件才使用 [[A, B]]；复杂布尔条件及关联仍需完整 target_expressions。

Concept 可以有多个匹配项，targets 可在不同 Concept 之间重合。图上一个物理字段或取值最多直接 grounded_on 到一个规范 Concept；同一 Concept 可以连接多个正确端点。这不是 targets 不相交要求，也不要求 extraction 完成跨 case merge。

Column 与其 DimensionValue 是不同端点。类别等值匹配绑定 governed Value，该值通过 value_of 指明 Column，不再重复绑定所属 Column 来表达同一个等值条件。其他 Concept 引用同一输入时保留匹配定义，并只建立有证据的业务关系，不能为绕过唯一性编造边。

存储读取保留历史 Table targets 兼容；新 extraction 的 schema 和写入校验另行收紧。正常 upsert、冲突候选及显式新绑定边均在保存前检查，拒绝不合法匹配，不静默拆分或改写 Agent 输出。

#### School 示例（抽取与评估共同使用）

```json
{
  "name": "School",
  "description": "学校业务对象；CDSCode 提供当前数据源的记录标识，School 字段提供名称表示。名称不视为唯一身份。",
  "properties": {
    "targets": [["column.schools.CDSCode"], ["column.schools.School"]]
  }
}
```

两条 grounded_on 各自解释 identifier 与名称角色；Table 继续保留在 metadata 中。示例需要真实 case/catalog 证据，不是遇到 school 就创建固定结构的规则。Case 若使用名称计数，应明确实际操作数和 NULL/非 DISTINCT 语义；不因存在 identifier 匹配就强迫 SQL 改成计数 identifier、增加投影或 JOIN。Assessment 检查该请求实际需要的匹配角色、业务主体和源/群体语义，不要求每个 target 都成为 SQL 字段操作；上下文包含本身仍不足以证明应用。 代码将 Concept grounded_on 表示绑定的 application_requirement 设为 semantic，按当前图中实际源节点类型判断；Concept 节点本身仍为 sql，必须证明请求相关含义的实际应用。Metric 的计算操作数仍需 SQL 应用；这一区分不允许跳过筛选条件、实际操作数或生成行为的检查。

#### DimensionValue 的可读身份（2026-11-05）

新建 ID 为 `dimension_value.<table>.<column>.<encoded-value>`，默认不计算 SHA-256。值采用可逆 UTF-8 percent 编码：P/52 等直接可读，空格为 %20，句点为 %2E，原始百分号为 %25，不裁剪/截断。~ 表示空字符串，原始 ~ 编码为 %7E；恰好形似历史 v_<64hex> 的原值转义首字符以避免与旧 ID 混淆。真实值以 Value.properties.value 为准，不用 ID 拼 SQL；Agent 复用工具返回的精确 ID。

历史已验证哈希节点可按原 ID 读取/复用，新编码引用经 owner/value 目录对齐到现有 ID，不从哈希逆推值，不重写历史快照。schema 变更后的训练使用新工作区；旧 checkpoint 的 schema 指纹保护不绕过。旧自定义 has_attribute 契约若与新 core 不同，需要显式协调，不能静默覆盖。数值与字符串沿用现有词法身份政策，类型冲突仍拒绝，本改动不扩展类型等价规则。

### 3.3 Metric

Metric 必须包含实际计算。表达式仅转述一个字段、名称或变量，不能使存储量变为 Metric。

仅为凑公式添加的 field * 1、field + 0，或单纯类型适配，不构成独立业务计算。单位换算、条件计算等是否定义业务量，由实际含义和证据决定，不按是否出现 SQL 函数机械判断。

FRPM eligible count, ages 5–17 与 Enrollment, ages 5–17 都是 Concept。FRPM eligibility percentage 是 Metric，其公式为 100 × eligible_count / enrollment，expression.refs 引用前述 Concept。

Metric 保留 expression 与 expression.refs，必要时说明单位、粒度、统计对象、分子分母范围和物理实现路径。操作数与公式占位符必须一致。Concept 或其他 Metric 都可以作为语义操作数；直接物理操作数的支持方式需要在实现中统一。

有计算只是必要条件。Metric 还必须是独立定义的业务量。对完整 Concept/Event 施加通用 COUNT、SUM、AVG、MIN、MAX 或其他请求操作不构成 Metric；筛选、分组和周期平均不能将其升级为指标。业务公式内部可以包含聚合，不能按 SQL 函数名机械拒绝。不要把整条问题或多项独立操作包装成长指标。

复合公式可以定义一个不可分割的业务指标，不按算术符号数量机械拆分。名称、公式、pattern、单位和实现必须一致，不将必要归一化偷偷移到其他节点。

保留原 case 的 COUNT 操作数、DISTINCT、空值、关联粒度和运算顺序。不能把 COUNT(nullable_name) 默认为 COUNT(*)；不能自行补零分母或缺值处理。

**名称与含义的可拆分检查**：采用 F9，与 Concept 使用同一 NL 语义判据。候选可以拆成 Concept、Metric 或 NL term 的组合，不限于“已有 Concept + 通用计算意图”；关键是各部分在当前 NL 含义中可分别匹配、组合后完整保留原义。不能仅凭存在业务定义豁免检查，也不能将公式的分子、分母及运算自动当成可拆 NL 成分。换名或缩短名称不是修复。

- average of number of schools：若只表示 School + count + average，应作为 Concept 与请求计算处理，不创建一个包装完整请求的 Metric。
- eligible free rate：在证据明确定义其为特定 eligible 数量与对应总体的比率时，可作为完整 Metric；分子、分母、总体与粒度共同定义该业务量。不能因为除法能拆成算术步骤就拆掉它，也不能仅因名称有 rate 就通过。
- 这是语义判断，不是对 school、eligible、average、rate 等词的关键词校验；生产代码不设置这些 case-specific 规则。拆出的计算也不自动获得 GenerationRule 身份，仍需满足有意义实现决策的准入条件。

语义评估对 Metric 单独检查 `metric_minimality`，不能以有公式、SQL 正确或已应用替代。联合检查名称、定义、expression/refs、实现计划和关联 Concept：依照 F9 检查可独立匹配的 NL 成分，分离被捆绑的请求筛选、分组、排名及周期处理。失败反馈必须指出语义单元、各自匹配及完整组合；拆分后只保留仍满足节点准入条件的内容。仅缩短名称或把组合行为移到 plan 不算修复。保留定义固有的总体、单位、粒度、常数及耦合运算，不按词数/操作符数判断，不强制创建中间 Metric，也不将每个普通操作都升级为 GenerationRule。

### 3.4 GenerationRule

GenerationRule 主要服务于 NL2SQL。其 condition 描述适用情境，action 描述原子行为，reason 或 description 解释依据及必要后果；检索触发词不等于适用条件。

动作原子性的判断：能否独立触发、替换、纠正与评估。多个 SQL 操作共同完成不可分割的决策时可以同属一条规则；多个可独立复用的行为必须拆开。规则不得复述整条查询，也不能拆为没有业务意义的 SQL 语法片段。

operation registry 同时是动作划分参考与标签词典。Direct 和 error-driven Repair 使用同一完整 registry，不按阶段限制 intent。两者区别是发现依据：Direct 使用 NLQ、Gold SQL、evidence 与数据库信息；Repair 还可使用实际错误。只有声称“观察到某错误”时才要求对应错误证据。

规则准入需要有证据支持的、影响语义或结果的实现选择；普通操作出现、多种等价 SQL 写法、假想错误并不足以创建规则。不得重复 Concept 匹配或 Metric 公式。准确复用已有 intent，动作不能超出其定义；多个标签不允许捆绑独立动作。只有现有 intent 无法覆盖该决策时才提出新 label，在 reason 说明含义、registry 缺口及有依据的实现差异。全局 registry 更新生命周期仍未确定。

condition 写明全部适用前提；action 写明 Gold/evidence 支持的最小可执行行为，必要时附局部 SQL pattern 并解释操作数和参数，不能复制完整查询或强制唯一 SQL 写法。允许在 condition/action 中提及通过 applies_to 关联的 Concept/Metric。参数化偶然年份、地点、阈值、K；保留定义所需业务常量。更换业务对象需要重新检查适用性，不以删除全部业务名词作为可复用要求。日期可缺失等静态限制写在关系描述，不强造生成动作。

Value 已实现的类别等值匹配，不再由同一 Concept 的额外 Column/filter 边重复保存。Value 的 value_of 记录所属 Column；真正不同的物理依赖必须单独解释。

### 3.5 Event

Event 表示事件类型或模式，不默认实例化数据库每条记录。创建时必须同时识别：发生行为、参与对象及其角色、至少时间或地点之一，以及这些信息属于同一事件的证据。

参与对象不一定是人物，也可以是学校、账号等。拥有日期列、动词、事件名称或一条 JOIN 都不充分。单一 Closed 状态不自动建立关闭事件。若能识别学校、关闭行为、相关关闭时间，并证明其联系，才满足事件结构门槛。

Event 必须有充分、可关联的结构证据。是否要求自身 targets，或允许通过已绑定 Concept 和显式关系获得锚点，属于实施 review，不作为已确定的放宽规则。无论采用哪种接口，不能为了满足旧校验编造非空日期即事件发生的定义。

不因 Event 无独立 target 就允许只有名称的空壳事件。也不由时间字段或一行记录推断可计数性、一次事件一行、完整生命周期、重复开办或阶段顺序。

## 4 关系及独立存储

关系单独保存，与节点一起参与版本、diff、评估、接受、撤销和快照。保留现有图容器时，单独保存可继续使用 GraphSpace.edges，不必另造关系数据库。

所有关系以显式边为正式结果，主要由 Agent 直接生成。代码最多在 Agent 写入后，依据仍保留且含义确定的引用检查并补全漏边，不把自动推断作为主要构图方式；补全边也必须保存为正式边。是否启用这种兜底属于实施选择，具体边界见第 5 节。

每条边必须有 ID、type、source、target、非空 description 与可追溯来源。当前模型可将 description 放在 properties.description；是否提升顶层字段只属于序列化选择。不增加 qualifier 字段，也不要求专门的 role 字段。

### 4.1 契约与完整关系清单

一种关系契约说明：合法端点及其业务角色、方向与断言含义、适用范围，以及明确允许/禁止的推导。核心契约保存在 root schema，扩展契约通过 `edit_graph.relation_contracts` 提交并保存在 `GraphSpace.relation_contracts`（复用 EdgeType 定义），由 Agent 按需读取；新谓词提交定义并随图版本保存，不能只写一个边名。具体边的 description 解释当前联系及有证据的约束，不能改写类型契约。不新增关系类型节点、Constraint 或 qualifier。

类型级业务边首先声明有依据的角色/属性对应，不自动断言实例存在、全称参与、基数、非空、历史完整性或可执行 JOIN。若需要更强断言，必须明确其范围与证据；自然语言说明交由语义评估，不冒充可执行公理。`is_a` 的集合包含与物理归属等明确契约单独遵守。

root schema 包含 **8 种核心关系**。前三种是物理结构，后五种组织映射、计算、规则、类别包含和业务属性；不要求每个 case 使用全部关系。

| 核心关系 | 合法端点/角色 | 契约 |
| --- | --- | --- |
| belongs_to | Column → Table | 字段的物理表归属；不是业务组成。 |
| value_of | DimensionValue → Column | 字段作用域内的取值归属；同字面值跨列不自动相同。 |
| joins_with | Table → Table | 经证据确认的完整连接键；不规定查询 JOIN 类型或业务身份。 |
| grounded_on | Concept/Metric/Event/GenerationRule → 各自允许的物理端点 | 直接实现绑定或输入；按源角色解释。Concept/Event 端点属于有效 targets；联合匹配的单个端点不是独立充分实现。 |
| depends_on | Metric → Concept/Metric | 公式直接语义操作数，与 refs 双向一致；间接依赖不是直接操作数。 |
| applies_to | GenerationRule → Concept/Metric/Event | 明确作用对象；condition 决定适用条件及多对象组合，不自动继承。 |
| has_attribute（抽象） | Concept/Event → Concept（属性角色） | 仅作为具体属性关系的基类，禁止直接生成边。具体谓词连接业务直接归属对象，不根据物理存储位置推断属性归属。 |
| is_a | Concept → Concept；Event → Event | 同类业务类别的一般集合包含，不要求真子集或立即父类，不允许跨基类。 |

**按场景定义的业务关系**：category_of、part_of、has_stage、precedes、has_participant、measures、concerns_event、analyzable_by 不再预注册于 root。它们可以在确有业务含义时，按与其他自定义关系相同的流程提交契约，不强制采用这些名字，不为使用某个关系额外造节点。

- 属性、分类维度、组成与时间顺序分别依据实际证据建立；不存在统一的必选层级或流程。
- 事件角色优先用确切业务谓词区分，但角色不明确时不编造方向。
- 被度量对象不等于公式操作数；不要将所有 measures 或 concerns_event 机械改成 depends_on。
- 分析维度必须有独立支持，不从当前 GROUP BY 自动抽成稳定知识。

**停止新抽取的 5 种历史关系**：Learning 不再新建/修改下列谓词的边，但可删除它们或迁移到证据支持的表示。历史图读取保留原定义，不自动改写边。

| 历史关系 | 原因与迁移要求 |
| --- | --- |
| computed_from | 与直接物理输入重叠；公式输入统一 grounded_on，原有额外限制不能丢。 |
| filters_on | 固定条件应在完整匹配表达式、公式或输入 Concept 中表达，避免字段边重复且丢失条件。 |
| expands_to | 混合类别细化、查询展开和计算展开；根据实际含义处理，不当作某一关系的自动别名。 |
| related_to | 过泛；改成受证据支持的具体联系，无法说明时不强行连边。 |
| aligns_with | 聚合后对齐通常依赖任务与粒度；必要时写入符合准入要求的原子 GenerationRule 或公式实现计划，而非无条件表间事实。 |

实现将 14 个历史定义单独保存在 legacy_relations.json，仅在读取使用它们的旧图时用于解释和校验。has_attribute 作为核心抽象基类仅供具体关系扩展，禁止直接写边；其余兼容定义不进入默认可写关系清单，也不授权新建；其余按需新建时必须保存本图契约。五种停止抽取的谓词不能通过补交契约绕过 Learning 限制。旧 snapshot 的 root schema_version 检查保持不变，不静默升级旧训练工作区。

`grounded_on` 的目标一定是物理端点。`depends_on` 的目标是公式语义操作数。合法源目标组合分别校验，不能把所有合法源与所有合法目标做笛卡尔积。重复使用同一公式操作数可以共享一条边，在公式及 description 中说明多个角色；不能机械按分子/分母拆重复边。

### 4.2 业务关系与扩展

语义节点之间的谓词表达业务关系，而非“某条记录存了某字段”。将 `source + predicate + target` 读成业务陈述，并检查端点真实含义；存储方式、证据来源及当前/历史记录范围放入 description。物理映射仍由 grounded_on、value_of 等专用关系表达，不更名为业务关系。关系名不应编码表名或“recorded 某字段”，也不能为了自然措辞把 code/category 端点偷换为业务实体。

例如 School → County 可用 `located_in`，description 限定为数据支持的学校所在地，不推断开校发生地；School → District ownership code 可用 `classified_by_district_ownership`，表示管辖类别分类，而不是“隶属于某个 code”。示例不是固定谓词表：Agent 按场景与关系契约选择并复用谓词，不引入学校专用确定性规则。

扩展谓词的类型定义与具体边必须共同可解释、可追溯。优先复用已保存同义谓词；Learning 新增谓词时，必须在同一次 edit_graph 中提交契约和至少一条使用它的边，契约随候选图保存；不支持 Learning 单独发布空契约。扩展不能覆盖、放宽固定关系契约，也不能将每条 description 变成一个新类型。

例如 `paid_from` 与 `paid_to` 分别说明付款方与收款方角色；仅有交易关联账号不能自行推断方向。`opens` 与 `occurs_on_date` 必须说明参与对象与日期如何属于同一发生。当前地址不能自动当作历史发生地点。关系名称本身不授予执行、继承、传递或计算依赖能力；需要相应的明确契约和消费者支持，不限制合法业务表达本身。

`category_of` 的源点表示维度中的类别/取值意义；“满足该取值的对象群体”是另一种含义，不能只因绑定同一编码就当作维度类别。Agent 可以自主选择表示方式，但定义、绑定、关系必须一致。不默认同时创建两种节点。

不默认使用 broader_than，不用 is_a 表示属性、位置、外键或维度取值归属。相同角色可采用协调组织，但不能强制同层、单继承或互斥。

### 4.3 Description 的内容要求

description 采用“具体联系＋必要约束”的简洁表达，通常一到两句话；这不是机械长度上限。约束可以说明基数、唯一性、可选性、适用条件或推断限制，但必须有证据并标明记录/业务对象/时间范围。没有必要约束时不强造。

记录主键只能支持当前记录的唯一标识，不自动支持某现实对象跨时间始终只有一个代码。已开设学校可能缺失记录日期时，描述应明确“日期可缺失，缺失不能推出从未开设”；是否允许缺失需要依据该群体的定义及数据，不能只看整表的空值数量。

字段类型、表/字段完整定义、profiling 行数和缺值数、值列表、探索 SQL 及证明过程留在 metadata/evidence。边中保留经这些证据支持的语义结论，不复制原始统计。继续使用 properties.description，不增加 Constraint 节点、qualifier 或约束字段。

description 必要时包含：

- 具体业务角色与标识作用；
- 关系成立的条件和范围；
- 字段可能缺失、数据覆盖或时间口径；
- 联系不意味着等价、必然或反向推断；
- 定义依据与数据观察之间的区别。

凡缺省理解可能造成错误的相关声明，必须写入 description，包括属性值是否可能未记录、联系是否有条件及是否允许反向推断；不要求每条边机械填写所有声明。业务图通常描述类型联系，不直接断言具体记录。has_attribute 不默认表示每个成员都有已知值；is_a 不自动产生字段非空条件、复制 targets 或执行 JOIN。

例如 Closed schools → Closure date：关闭日期用于记录关闭时间；本数据存在日期缺失的关闭记录，日期非空不是识别关闭状态的必要条件，缺值不能推出学校仍在运行。

不合格描述是“两个概念有关”。description 也不能绕过谓词：is_a 不能在正文中实际表示 located_in。描述修订不应自动成为全新的关系身份；不同角色优先用不同谓词区分。

## 5 引用与绑定契约

不再以通用语义节点 refs/references 代替边；Metric.expression.refs 保留。Column.table_ref 与 DimensionValue.column_ref 属于物理归属信息，不因删除通用 refs 而丢失。

可选的写入后兜底只处理引用含义唯一确定的漏边，例如公式中明确的 Concept/Metric 操作数可对应 depends_on。已有显式边优先；不能因引用存在就补 is_a、opens 等业务关系，不能从重合 targets 自动补多条 grounded_on 而违反连边唯一性。不为兜底恢复通用 refs 字段，不能在每次读取时反复编译。无法确定关系或与显式边矛盾时反馈 Agent，不擅自覆盖。

targets 回答怎样匹配含义；expression.refs 绑定计算操作数；edges 保存明确联系。Agent 应在一次一致编辑中提交节点及必要边。写入时校验各自有效及相互不矛盾，不要求每次 target 引用都产生一条该 Concept 的直接物理边，也不根据节点类型猜测并补业务边。

targets 的 references 子字段也不能继续承担自动关系编译。替代格式采用匹配分组方向，保留必要的绑定表达式与联合条件；是否需要 target-local ID 仅作为实施选择，不能据其强制生成直接边。保留 expression.refs 是明确例外，不应连带删除。

物理 catalog 的 belongs_to、value_of、已验证 JOIN 事实可由确定性导入代码创建并提供说明。它们与 Agent 编写业务关系权限分开。无论由谁写入，读取和投影都消费已保存边，不在展示阶段补业务关系。

## 6 分类重叠与 OWL 参考

可借鉴 OWL 的 subclass、equivalence、disjointness，但不引入完整 OWL 推理体系。is_a 对应一般类包含（允许相等，不声明立即父类）；等价优先检查 aliases 或合并；互斥必须有证据。

两个学校 Concept 可以共享 School 父类并绑定不同的值。共享父类不证明数据库里已有共同成员，也不要求互斥。某所学校同时满足多个类别，通过共同学校身份及其字段条件体现；case-level 类型图不必枚举实例。

交集通常作为当前查询条件，不强制创建交集 Concept。不为每对分类添加 overlaps 边。若某种交集本身有独立业务含义且满足最小化与绑定要求，Agent 可以依据证据决定，而非默认创建。

参考：[OWL 2 Primer](https://www.w3.org/TR/owl2-primer/)。其逻辑表达能力不是本项目物理绑定唯一性策略的来源；后者是本项目为避免重复概念化选择的工程约束。

## 7 抽取与 error-driven 优化

1. 读取 case、已有知识、字段说明及关系和 operation guides。
2. 提出最小业务节点与关系候选；优先复用已有身份与绑定。
3. 对歧义、粒度或限制进行必要的只读探索，记录证据。
4. 在同一原子编辑内提交节点、绑定与显式关系。
5. 如启用确定性漏边兜底，先补全并保存，再校验硬规则、整体业务结构、最小化及证据强度；评估与 replay 使用同一最终图。
6. SQL replay 独立检查当前任务实现；不以 SQL 正确替代语义质量。知识遗漏主要通过实际错误及其诊断发现，不另设必要知识覆盖率、节点数或类型覆盖配额。
7. Repair 可以修改 Concept、Metric、GenerationRule、Event 及边，不只修三种旧节点。
8. 错误应定位到最小知识单元；禁止追加整条 SQL 作为万能修复。

在 SQL 已正确时，仍可修正无证据关系、错误层级或过度组合节点。没有新的 SQL 语法贡献不是拒绝业务关系的理由，但也不能成为绕过语义质量检查的理由。

## 8 验收要求

### 8.1 硬规则

- Concept targets 非空，匹配分组和端点有效；targets 可重合，直接物理连边满足唯一性。
- Metric 有实际计算、占位符与 refs 一致，允许的操作数类型正确。
- 边有合法 ID、端点、方向、非空 description，固定关系满足精确端点组合；新谓词有随图保存的契约，不能靠未定义名称绕过类型规则。
- 引用无悬空，binding/expression/edges 一致；删除、撤销、版本与分支不会残留边。
- 不恢复通用 refs 主导构图；可选兜底限于可确定的漏边，不引入 Constraint 或 qualifier，不更新 metadata。

实际计算判定与自然语言描述质量不完全能靠静态校验，需要 Assessment 补充；格式校验通过不代表语义通过。

Assessment 首期继续使用已有 case、metadata、节点、关系和 SQL/replay 等输入，不强制新增数据库探索证据接口或逐边 probe 绑定。探索仍供 Agent 判断语义；已有结果易于提供时可作辅助信息，但不要求每轮探索或令 assessor 重新探索。仅有 description 中的强断言不能使无依据知识通过；无法确定时保留疑问或反馈 Agent。

### 8.2 语义评估

| 维度 | 要检查的问题 |
| --- | --- |
| 证据一致性 | 定义、边和限制是否超出来源支持范围 |
| Concept 最小化（minimality） | 按 F9 检查能否拆成分别匹配的完整 NL 语义单元；targets 重合/数量本身不能判定 |
| Metric 合理性 | 是否真实计算，口径完整，存储量是否误标 Metric |
| Metric NL 语义最小化（metric_minimality） | 与 Concept 共用 F9；说明可拆语义单元及各自匹配，不把公式展开当作 NL 拆分；真实计算另行检查 |
| Rule 原子性 | 是否可复用，是否夹带完整查询流程 |
| Event 克制性 | 是否有行为、参与者、发生上下文及正确关联 |
| 全图业务组织 | 定义、映射、角色、关联范围及允许的层级是否一致，不追求唯一组织方式 |
| 断言去冗余 | 重复项或推导链是否可核查；删除是否保留范围、来源及消费者所需直接联系 |
| 关系信息质量 | description 是否简洁说明具体联系及必要约束，范围正确；不混入 profiling 数字、字段百科或证明过程 |
| 数据解释 | 是否混淆状态、日期、缺值、标识与粒度 |

任何关键语义缺陷不能被其他维度的高分抵消。旧实验的 81/84 与测试通过记录不满足本轮的新验收要求。

## 9 放弃的方案与仍需 review 的实现选择

不采用：独立 Constraint 节点、强制角色枚举、状态/属性/类别专用决策树、broader_than 默认层级、has_participant 加 role/qualifier 的统一事件角色模式、无绑定 Concept、存储量 Metric、仅一个 registry 标签、通用 refs 自动编边。

当前实施采用分组 targets、可选 Event targets 与当前生效图内绑定唯一性；扩展谓词契约沿用 EdgeType 定义并保存在 GraphSpace.relation_contracts。仍需 review：全局 operation label 生命周期、跨 case 语义对齐及旧快照再生成策略。这些选择不改变前述业务规则，具体建议见配套修改方案。


本轮 Metric 适格性、Rule 复用及逐项 quality 验收补充见 [两例 review 规范](ontology-two-case-review.md)。


## 10 关系组说明与属性关系家族（v59 实施）

Concept 可选 properties.relation_metadata，每项仅 id、edges、description，用于同一谓词、同一相对方向的多条相邻边之间、单边无法表达的有依据组级知识。不强制生成，不自动推理或补边。具体业务属性谓词可选 extends=has_attribute，限一层；其他业务谓词仍可独立定义。子类不另设额外业务知识门禁。

完整结构条件、保存/检索边界、Learning/Assessment 输入和应用要求见 [v59 实现说明](ontology-relation-groups-v59.md)。该说明更新 v58 实施前状态，历史记录保持可追溯。

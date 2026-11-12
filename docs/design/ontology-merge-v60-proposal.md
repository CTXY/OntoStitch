# Ontology merge v60：受控局部整合方案（待 review）

> 历史草案：后续讨论已取消 U 作为正常方法输出，并进一步调整差异分析与局部边界。最新多角度审查及尚未解决的表示问题见 [v61 review](ontology-merge-v61-review.md)。下文保留原始提案，不应将其默认 uncertain/deferred 策略当作最新共识。

日期：2026-11-07。本文是基于 v59 extraction graph 的设计提案，不表示 merge 实现已修改或实验已验证。以下 mock 是设计走查，不是模型运行结果。目标是让代码管理检索、引用和事务，让 Agent 判断语义并提出有限范围的修改；不增加复杂的 ontology 类型体系。

## 1. 问题与实现基线

合并的输入不是只有节点集合，而是带有来源、显式边、关系契约、可选关系组说明的图。需同时处理：同义节点、兼容的不同表示、同义谓词、事实竞争、已有抽取错误。不能将这些问题都归为 identity matching，也不能都保存为 conflict。

当前代码可复用的部分：

- `capabilities/graph_merge/transactions.py`：候选 GraphState 编译为 GraphManager 原子操作、preview、revision 检查；已有回滚与 conflict 写入支持。
- `reconciliation/`：来源读取、完整候选、compact conflict、独立 Assessment、有限轮次反馈。
- `workflow/run.py`：checkpoint 和恢复；原始输入保留。
- `schema/relation_metadata.py` 等：节点/边引用重映射及结构验证。

主要缺口：

1. `workflow/run.py` 先一次性 match，再逐 group integrate，没有局部重新检索入口。
2. `models.py` 的 group 只有一个 existing canonical；identity 只有 same/different，unknown 被 prompt 编码为 different。不能准确处理新 case 发现两个历史 canonical 实为同义的情况。
3. `identity.py` 的实现内容检索不是物理端点倒排检索；身份输入主要是节点，不是显式关系邻域。
4. Consolidation 提案面向 NodeContent；`preview_group` 以单 group 节点替换为中心，无法表达联合层级重组、谓词变更及多 owner 关系组调整。
5. `merge_types/concept.md` 仍要求 target.sql_expression 等旧结构；需同步 v59 的 targets 分组与显式边规范。Metric guide 也需与当前有效模型及最小化要求核对，不能机械保留历史要求。
6. `merge_relation_contracts` 对同 ID 的不同契约直接报错；operator 在进入 Agent 前合并契约。跨 case 同名谓词只要 description 不同也可能提前失败。
7. `schema/relationships.py:is_dependency_edge` 将有 reference_origin 的边也视作依赖；operator 用其传递依赖安排 group。业务关系可成环，不能全部变成必须先完成的计算依赖。
8. 边 JSON 不同当前可被保存为 variant，但不能据此判断语义冲突；compact conflict 不自然支持内容完全相同、仅边冲突的任意子图选择。

## 2. 最少的运行框架

保留一个合并 Agent 和一个独立 Assessment；候选 matching 是合并 Agent 的一个判断阶段，不再另设长期规划、多角色辩论或新的层级 Agent。

```text
导入与物理身份对齐
  → 工作队列取一个局部任务
  → 代码检索候选和影响范围
  → Agent 判断身份、差异，提出局部 patch
  → 代码 preview / 结构验证
  → 独立 Assessment
  → 原子提交；仅重新排入受影响任务
  → 无待办或预算到达，报告已整合与未决部分
```

新增必要能力只有：候选/反向引用索引、增量工作队列、多节点局部提案。复用现有读证据、事务、校验和 Assessment 底座。节点、边、谓词映射都是不同对象，不能用一个混合字典代替。

工作队列必须覆盖所有 incoming 语义节点、边和本地谓词，不能只覆盖已发现违规的内容：两份完全合法的同义图也需要整合。队列任务可合并执行，但每项来源都需有处理结果。

## 3. 身份与候选：信号不是结论

### 3.1 物理锚点

先对齐物理身份：数据库/目录身份、表、列，以及 `(owner column, typed value)`。显示 ID 字符串只作引用；不同数据库的同名列不相同。值的类型、NULL 和字符串字面量必须保留，不用大小写折叠、数值化或哈希替代语义身份。沿用可读 Value ID。

物理元数据参与识别，不在本流程中做 metadata store 的优化或更新。

在同一数据库及可同时生效的语义作用域内，对物理端点 p：

\[
|\{c\mid type(c)=Concept\land grounded\_on(c,p)\}|\le1.
\]

这是本项目选定的规范化策略，并不是所有 ontology 天然成立的定理。它约束直接边，不是 targets 不可重合，也不是所有类型节点都只能用该端点。Metric 可以共享输入。不同 conflict 分支不得被误算成同时生效；作用域未知时不能凭新造 scope 字符串绕过检查。初版以实际数据库、已有分支及有证据的范围为准，不增加通用 scope 推理引擎。该策略可能让某些合理但尚无规范表示的输入暂缓整合；实验应统计这种情况，不能靠强制同义合并消除它。

两 Concept 争用端点产生一个必须处理的任务，并不证明两者同义。可通过同义合并、修复错误绑定或有依据的表示重组解决；不能确认时，争议修改不能发布为一个违反唯一性的正常图。

### 3.2 检索候选

以下公式针对语义节点候选。检索宇宙 U 是当前 working 中的语义定义与本批所有尚未发布 incoming 定义的并集，使用 source-qualified ID 并排除自身；不是只检索已提交的内容。令 D(n) 为直接绑定的物理端点，T(n) 为 targets/expression.refs 中涉及的端点，N(n) 为已有显式语义邻域：

\[
Candidates_U(n)=Mandatory_U(n)\cup TopK_U(Text(n),T(n),N(n)).
\]

- n 为 Concept 时，Mandatory 是 D(n) 各端点所有可同时生效的 Concept 直接 owner；其他类型不因输入相同触发 Concept 独占约束。潜在唯一性争用项必须全部检查，不能被 top-k 截掉；代码完整枚举，Agent 可分页读取。
- targets/input 重合、名称/alias、已有对齐邻居、operation intent 是软召回信号。
- 普通 identity 候选以同 node type 为主；其他类型可作为修复上下文，不能仅因名称相似直接跨类型折叠。
- 仅有边的更新也进入队列：若新增 c grounded_on p，以 c 为 seed，按待处理边加入后的局部预览计算 D(c)，不能因 c 没有内容变更漏检碰撞。谓词-only 任务按契约及使用者处理，不硬套节点公式。
- 检索未命中不证明全库无同义节点。初版提供可审计的 top-k/分页，不承诺穷尽所有语义对应。

### 3.3 身份判断三态

`same / different / unknown` 是工作流判断，不是新 ontology 属性。每项简短说明比较对象、业务指称/义务、关键证据。unknown 与已知不同分开：有新证据或邻居变更才重试。

| 类型 | 同 identity 的判断 | 不能作为充分条件 |
|---|---|---|
| Concept | 相同业务指称，且定义范围、对象角色可统一；可同时容纳 identifier/name 等不同匹配角色 | 同 column/value、同父类、同词形 |
| Metric | 相同业务量及语义输入角色、适用对象/范围和结果含义；可能存在竞争计算实现 | 同输入字段、同公式字符串、当前数据算得相等 |
| GenerationRule | 相同最小独立生成义务、适用触发和业务角色；差异仅实现同一义务的方式 | 同 intent 标签、同 SQL 运算、同 case 共现 |
| Event | 相同事件含义及参与角色、发生上下文；类型级事件与具体发生实例不混合 | 共用时间列或参与者 |

Metric 的粒度、单位、总体、NULL 策略首先帮助解释“究竟请求什么”。若业务目标不同则分开；若同一明确业务量有两种不兼容实现，则进入内容冲突判断，不能以公式不同直接分开，也不能凭同名强行并入。

对拟合并组必须能给出一个适用于所有成员的共同身份说明，并检查原成员的限定。不得以相似性连通分量、A 像 B 且 B 像 C，直接推出整个组同义。组级矛盾或 unknown 阻止该组折叠。

“统一”必须保持成员原本的所指、范围和角色，不是删除限定、概括成共同上位类。School 和 University 可以同属 Educational institution，但不能因此合并 identity。新共同父类属于另一项有证据的结构提案。

## 4. 先判断差异，再选择动作

不要求 Agent 先猜一个大而固定的冲突 taxonomy。按下表顺序判断即可。

| 问题 | 结论与动作 |
|---|---|
| 同义、差异只有措辞/重复表示？ | 统一 canonical，合并可追溯来源，保留必要 alias |
| 同身份、内容互补且可同时成立？ | 合并内容；不能简单 union targets、条件和 relation_metadata |
| 不同身份、存在有证据的业务联系？ | 两节点保留，新增/修正具体关系；不是 conflict |
| 两种结构都成立但粒度/分解方式不同？ | 有依据时局部重组，保留有效语义；无须追求唯一规范层级 |
| 某项被 NLQ/SQL、契约或数据库证据明确驳倒？ | 修复该项；原文和修复理由留在审计，不继续当有效业务口径 |
| 同身份、同适用条件下两个有依据的主张不能同时成立？ | 真实内容冲突，沿用完整 alternatives 和 conflict_points |
| 不知道是否同义、兼容、正确或如何无损表示？ | unknown / unresolved；不能把缺证解释成已确认 conflict |

错误、不准确、缺证需区别：描述措辞含糊可澄清；只有不被某个 case 用到不意味着错误；Gold SQL 只支持其查询范围内的行为，不证明普遍业务法则。不同 case 的 SQL 矛盾也可能来自粒度、时间或数据版本差异，必须先排除这些解释。

### 4.1 真实冲突的明确标准

对节点内容竞争先确认同一身份；对关系或组声明，确认端点、角色和成员已正确对齐，并不要求它们属于同一个节点。对少量待比较断言集合 A，核对真实作用域、条件、量词和各自来源支持，然后判断它们在这些解释下能否同时成立。其理想语义判据是：

\[
ConfirmedConflict(A)\Rightarrow Aligned(A)\land SourceSupported(A)\land Unsat(K_{scope}\cup A).
\]

K_scope 是经确认且本身一致的背景约束。SourceSupported 表示能追溯到各自真实来源立场，并不表示所有互相矛盾的立场都已经被证明正确。这里是 Agent 的解释义务，不是对 description 自然语言做完备自动逻辑证明。应说明冲突的具体主张和原因，不能只写“定义不同”。A 可为两项，也可为必要的小组，例如两条类别关系与一项组互斥声明。

上式是确认冲突必须满足的语义条件，不承诺发现所有冲突。例如 C is_a A、C is_a B 与 A/B 互斥会使 C 无法有成员，但在允许空类时不必造成整张图逻辑矛盾；有 C 的实际成员证据或明确非空要求，才得到相应不一致见证。不能把潜在空类、结构违规与事实矛盾混为一谈。

范围有交集仍不足以证明冲突：“所有 A 学校有日期”与“某个 A 或 B 学校缺日期”可以同时成立，缺日期对象可能只在 B。不能把存在量词擅自缩到范围交集，也不能把“没有观察到交集”升级为互斥。

“未保证有日期”“业务允许日期未知”“数据库观察到缺日期”分别是缺少保证、许可要求与事实，不可互换；缺少保证本身不是非空断言的反例。

计算实现的竞争应指出在合法输入下行为不同的条件；必要时给一个满足键、粒度等约束的小反例。当前快照返回相同结果不足以证明公式等价；虚构违反键约束的重复行也不能证明两个合法实现冲突。

例：COUNT(id) 与 COUNT(DISTINCT id)，id 在该输入粒度唯一时可能等价；经过一对多 JOIN 后可能不同。若原请求分别是学校数与记录数，则是不同义务；若同一学校计数义务上两方声称不同策略才进入竞争判断。该例用于解释 Rule，不强迫将可分解的 school count 建为 Metric。

### 4.2 冲突存在哪里

- 已确认同身份的节点内容竞争：沿用现有 compact conflict，公共字段 + 完整字段覆盖 + 来源；分支边保留 alternative_ids。
- 同身份但尚不能判兼容：沿用现有 unresolved 表达，并保留来源；不能伪造已确认 conflict_points。
- identity 尚未确认时，不能将两份定义装进同一 canonical 的 unresolved alternatives。若分别发布不违反绑定唯一性等契约，可以先分开保留并记录待匹配项；若有硬约束争用，则暂存争议写入。unknown 不等于一律禁止独立发布。
- 纯关系竞争或需要跨多个 identity 共同选择的结构：不强塞进单节点 alternatives，不捏造节点内容差异；本版先将无法无损表示的争议 patch 留在 merge 运行产物，列明原因。
- 留待处理的是争议写入及依赖其尚未发布 ID 的事实，不是无向连通分量；School 等中心节点不能导致整库一起暂存。
- 正式图保持先前已发布版本，并明确报告未整合事实/已发现旧图问题；“没有写入新错误”不等于“旧图已证明正确”。与争议无依赖的事实可独立发布。

## 5. 关系和层级如何合并

### 5.1 谓词先解释，再比较边

导入期把 case-local 谓词及其引用按来源临时命名空间化，避免同名契约在进入 Agent 前报错。root 核心谓词不可重定义。物理身份仍按数据库对齐，不能因临时命名空间失去同一端点信息。

Agent 比较谓词的定义、端点业务角色、方向、适用范围、已有 extends：

- 同义契约：复用 canonical 谓词 ID，并重写本次涉及的边；只采用证据支持的共同语义。
- 同名不同含义：保留不同谓词并改为清晰名字，不放松 root 验证。
- 具体谓词与 has_attribute：如符合现有单层家族契约则保留；不能同时生成父边来表达同一事实，也不能仅凭两端都是 Concept 判定继承成立。
- 不扩大为一般 schema 设计 Agent；任意类型继承、根类型修改不在本轮范围内。

不能为加一条 incoming 边改变已有谓词契约的含义、偷偷影响所有旧边。若需改已发布契约，完整取出所有使用者做影响检查；初版更倾向复用等义契约或新建更准确的本地契约。

### 5.2 边的差异

边比较单位是 `(已对齐端点, 已解释谓词, 实际适用范围/分支, 断言含义)`，不只是 triple 或 description 字符串。

- 相同断言、不同措辞：一条边，保留来源和必要限定。
- 同端点同谓词、描述互补：在范围相容时合成简洁说明。
- 不同业务角色或不同时段：通常并存，不因同端点就 conflict。
- 同范围主张“每个对象必须有日期”和“允许缺日期”：需判断一方错误、范围差异或真正竞争。
- `located_in` 与 `has_attribute` 可能是同一事实的精确/泛化表达，也可能不是；只有确认语义涵盖后才去掉重复，不按名字或 extends 自动删除所有父边。

### 5.3 层级与环

`C is_a B` 仍要求相同作用域中 `∀x, C(x)⇒B(x)`，两端均表示可比较的对象类别。共享父类不证明互斥、共属或覆盖完整。

不同父类可能是合法多重分类，不强制只有一个 parent。不同深度也可能兼容，不自动叫冲突。如果发现相反 is_a，需检查是否同义、方向错误或范围不同，不能把环本身当作同义证明。

只对契约明确要求无环的子图判环，不要求全图 DAG；业务关联的环不作为调度失败。计算依赖与“需要查看的相关节点”分开。多节点引用可在一个原子 patch 中同时创建，不能据此强迫逐节点拓扑排序。

合并可能新增过去不存在的间接继承。代码可提供 affected is_a 可达性变化的路径见证，Agent 判断是否受支持；不绝对禁止新增可推导知识，也不要求推理任意自然语言约束。

### 5.4 重复和可推导冗余

仅清理重复断言和已能在当前契约及相同范围下证明的冗余断言，不追求最少节点或唯一层级。

\[
Redundant(e,s)\iff K_s\cup(G_s\setminus\{e\})\models e
\]

G_s 表示相同范围内一组可同时成立的分支断言，推理前提必须一致；不能将互斥 alternatives 全部混入推理。K_s 只包含实际支持并已声明的推理规则。这个公式不是承诺完备 prover。初版只处理精确重复、明确同义及明确允许的层级传递等有限情况。删除 e 还必须处理其来源、限定和 relation_metadata 引用；若删除会丢这些信息，须改写保存或不做这次删除。

`relation_metadata` 只说明列出的成员边。ID 重映射可由代码做；两项自由文本组说明不能自动 union。两组各自互斥，不意味着合起来全部互斥；两组分别有交集也不意味着三者共同有交集。成员集合、owner 或谓词改变后，需在同一个 patch 中重审说明。不得擅自把组声明外推到新子类。

## 6. 局部事务的管理与安全边界

### 6.1 只保留三类运行状态

1. 不可变 original graphs / evidence；引用形如 `(source_graph_id, local_id)`。
2. 当前 working graph、revision，以及已提交 node/edge/predicate 映射。
3. 任务队列和提案/验证审计；记录已整合、待处理、修复理由及引用。

不增加业务节点上的工作流状态，不在 ontology 中创建 Review/Issue/Constraint 节点。新 canonical ID 由代码分配，现有 canonical 优先稳定；合并多个 existing 时按固定规则选 survivor，旧 ID 映射和来源保存在运行结果，可供下游解析。原始图保留原 ID。实际使用旧 ID 的持久调用方仍需通过结果映射迁移，不能声称保存映射就完成了所有外部迁移。

### 6.2 任务上下文

每个任务包含 seed、候选分页目录、必须处理的端点冲突、局部双向边、涉及契约、完整组说明及受影响消费者。大字段、原始 case、数据库证据按需读取。规则 guide 首次提供，后续只读相关类型；operation registry 仅涉及 Rule 时提供一份。

复用 `read_nodes/read_evidence`；新增一个有范围限制的 `find_candidates` 接口，支持 ID/端点/文本/邻域和分页。数据库查询可复用现有只读服务，按需授权到本数据库，不能只让 Agent 看到结论却无法读支持证据。是否需要包成新 tool 由实施时复用检查决定，不先增加一套探索工具。

### 6.3 提案需要什么

最小逻辑输出：身份判断、节点/边/契约修改、短的改变理由及来源对应。复用 GraphManager 操作格式，不造第二种图 DSL。涉及删除/改义时必须说明原有受支持信息移到哪里，或为何被证据否定；这不是要求抽取所有可能知识的 coverage 指标。

代码事先枚举 write-set 及其反向引用：incident edges、expression.refs、已有 alternatives、relation_metadata 的 edge IDs、谓词使用者。普通读取可很小，结构影响检查不能只看 top-k。范围不足时申请扩展后重新 preview，不让 Agent 任意改全图。

多对一同义合并可用映射改 ID；一对多拆分不能靠一个 node mapping 猜消费者该指哪一项。若本地拆分能显式改完全部受影响引用，可作为受审 repair；影响过大或无法决定时暂存，首版不承诺任意自动拆分和全图重构。

### 6.4 发布门槛

\[
Accept(\Delta)=StructuralValid(G\oplus\Delta)\land SemanticApproved(\Delta,E)\land PreservationAccounted(\Delta).
\]

Structure：字段、端点契约、引用、绑定唯一性、分支与组说明结构、明确禁止的循环。唯一性只按代码可判定的数据库/已有分支范围检查；description 中隐含条件是否兼容仍归语义审查，不假设代码能完整求解作用域交集。哪些检查当前已有、哪些需加，应在实施时列清；这不是声称当前 GraphManager 已覆盖全部新要求。

Semantic Assessment 只分三层，避免堆一长串互相重叠的检查名：

1. **对象与内容**：身份是否成立，节点类型/最小化、公式与动作含义是否正确。
2. **关系与范围**：业务边、层级、谓词、组说明及竞争/未知处理是否受支持。
3. **修改影响**：原有受支持知识、分支条件和消费者是否保留；争议信息是否被错误提升为无条件事实。

SQL 回放可作为改变计算/生成策略后的风险检查，不要求纯别名合并每次调用 SQL Agent；当前快照回放通过也不是语义等价证明。无 SQL 消费行为变化时可免回放，有变化时由明确的风险条件触发。不能把“暂未设计回放”包装成已验证 NL2SQL 正确。

## 7. 迭代但不失控

```python
queue = seed_all_incoming_items_and_detected_collisions()
while queue and budget_available():
    task = take_task()
    view = retrieve_current_candidates_and_impact(task)
    if unchanged_since_completed_episode(task, view):
        defer_with_reason(task)
        continue
    feedback, committed = None, False
    for attempt in bounded_attempts_within_run_budget():
        proposal = merge_agent(view, feedback)  # 可请求分页/有限扩展并刷新 view
        preview = validate_patch(proposal, expected_revision=view.revision)
        if not preview.valid:
            feedback = preview.errors
            continue
        verdict = independent_assessment(preview, originals, evidence)
        if not verdict.accepted:
            feedback = verdict.reasons
            continue
        commit_atomically(preview)
        refresh_indexes_and_requeue_affected_items()
        committed = True
        break
    if not committed:
        defer_with_reason(task)
    record_completed_episode(task, view)
return committed_graph, mappings, unresolved_tasks, audit
```

只在节点内容/身份、相关谓词或邻域、证据发生有关变化后重检索。同一个 episode 内允许依靠具体反馈修订，不被“输入未变”检查挡住；episode 结束后才记录签名防止无限重开。身份 unknown 不因无关节点更新重试。签名必须包含实际读取的定义和证据版本，不能只看 seed 名称；同时，通用 hub 的无关新增邻边不应引发全库重试。实施可先保守扩大重查，记录调用量再优化，不宣称已找到理论最优影响集。

不保证每次成功 patch 都让图更小，也不保证数学上的唯一固定点。每任务有限提案/扩展次数、全 run 预算、防止相同状态往返；超限明确 partial/unresolved，不叫 fully merged 或 stable_correct。后续新证据可显式重开任务。已提交错误合并需要独立受审 repair，不以 union-find 自动拆回。

## 8. Mock 流程走查

### A. 同一核心实体，多种 identifier/name 匹配

G1：School → schools.CDSCode；G2：School → frpm.CDSCode，并匹配 schools.School。

检索通过名称和物理目录关联召回；有来源支持它们都指学校实体，而不是把 name 当成独立文字属性的请求。确认 same 后保留一个 School，targets 是不同独立匹配项，不把三个字段塞到同一个联合 item。各 grounded_on 的 description 说明标识/名称角色。若 G2 实际是“学校名称”属性含义，则不凭 name 字段共享自动合并，需要另审角色。

### B. 同端点，不同粒度或角色

G1：Operating status → StatusType 列；G2：Closed schools 错接 StatusType 列，来源 SQL 实际使用 StatusType='Closed'。

代码发现直接绑定争用；Agent 不能把 status 与 closed-school 群体并成一词。读取 SQL 后，可把 G2 修为绑定具体 Value，保留其类别表示和有依据的业务边。两个 Concept 不再抢同一物理端点。这里是从证据修错，不是硬编码 Closed 的处理规则。

若两节点含义确实不同却都只有同一个不可区分的物理表示，且没有足够信息选择规范 owner 或构造可靠关系，本轮暂存相关改动；不随意加 depends_on 或一个新 scope 来通过校验。

### C. Metric 输入相同，目标不同

G1：某业务定义的 eligible share = SUM(eligible)/SUM(enrolled)。G2：另一业务定义的 mean school eligibility rate = AVG(eligible/enrolled)。假设这里两个术语均已通过各自最小化审查。

共享输入产生候选，不能直接合并。用两学校数据 `(eligible,enrolled)=(1,2),(90,100)` 走查：前者 91/102，后者 (0.5+0.9)/2；分母权重及目标不同。若证据明确定义了两个业务量，分别保留；若双方都声称实现同一个明确“总体 eligible share”，需要先判断错误实现或真实口径竞争，不能仅凭这个数值反例决定 conflict。

此例不为任意“平均值”复合名称授予 Metric 资格，实际抽取仍须通过 NL 最小化。另一边界是比例与百分比：若来源明确 0.2 和 20% 是同一个量的不同编码，不因乘 100 就判不同指标或冲突。先解释单位；不能在单位未知时任选公式，也不能把两个 expression 简单并起来。假设数据只作为行为反例，不冒充源数据库观测。

### D. 相同类别的不同结构表达

G1：Charter school is_a School，绑定 Charter=1。
G2：School has_attribute Charter designation，后者也绑定 Charter=1。

共享 Value 必须审查，但“学校子群”与“特征值”不是当然同 identity。两个表示可兼容，也可能存在类型使用错误。Agent 查看下游 Rule 的对象角色、case 的真实需要，决定能否保留一种规范表示，并将另一表达的必要语义放入正确节点/关系。targets 可保留重合，但直接 owner 唯一；如果同时保留两个语义节点，需要有支持它们关系及各自匹配用途的证据，不能仅删一条绑定装作问题解决。无法无损重组就暂存，不规定所有 charter 表达必须转属性或必须转子类。

明确终点：若源定义证明 G2 实际也指 Charter=1 的学校群体，修正不准确的名称/关系后合并为同一类别；若 G2 确实指属性值角色或仍无法判断，且无证据决定规范 owner 与两者关系，本 mock 结果就是 deferred collision。后一结果不是 ontology conflict，也不是成功消除了表示差异。

### E. 真正竞争的 GenerationRule

两个 Rule 都处理“选择排序最前的 k 个对象”，相同适用范围、相同 tie 情况：一个明确返回恰好 k 条，另一个明确保留第 k 位所有并列。

先检查它们是否分别对应请求语义不同的两个最小义务，或存在明确 tie-policy 条件。只有同一义务、条件重合且双方都被支持时，才在同一 canonical Rule 保留两个 action alternatives，边随各自分支。若其中只是无证据的生成猜测，则修正/待定，不制造业务政策冲突。两个 Rule 的 intent 可相同，intent 本身不做裁决。

### F. 新 case 桥接两个已有 canonical

旧图 School(A) 与 Educational institution(B) 分别指学校还是 broader institution 尚不明确。新图出现“school / educational institution”别名及双表标识。

不能因新节点像 A 和 B 就把三者连通折叠。读 B 的原始限定：若包含大学等更广对象，B 保留为 broader class，只在证据充分时连 is_a；若 B 的实际定义也严格指同一学校实体，则整组三方确认后原子并入一个 canonical，改写所有 incoming/outgoing 引用。邻居刚获得共同身份后，相邻网站节点等重新获得候选比较机会。无新证据时不无限再次检索。

### G. 谓词同名与组说明

两个 case 都叫 located_in，一份含义是实体实际位置，另一份是行政注册地。导入先命名空间化，Agent 不能凭名字统一；若来源区分角色则保留不同谓词。若仅描述措辞不同、实际同角色，则复用。

两个父节点合并后，原 metadata 分别约束 `{e1,e2}` 与 `{e2,e3}`。映射 edge IDs 后逐组保留或澄清，不生成 `{e1,e2,e3}` 的全组互斥/交集声明。遇只有关系互相矛盾而内容完全相同的情况，先保留争议 patch，不为塞进节点 alternatives 而添加虚假内容字段。

### H. 冗余层级与合法环

已有 C is_a B、B is_a A；incoming C is_a A 在当前契约和相同范围下可推导。若没有额外限制及 metadata 消费，合并来源并省去重复断言；有组说明引用时先处理引用，不能机械删边。反之 School→Opening→School 之类不同业务谓词形成环不据此删除 Event，也不使逐节点调度死锁；需独立检查每条边的含义和是否重复。

## 9. 研究参考与可检验贡献

- [PARIS](https://www.vldb.org/pvldb/vol5/p157_fabianmsuchanek_vldb2012.pdf)：实例、关系与 schema 对齐可以相互提供信号。启发是邻居/谓词对齐后重新检索；不能直接把实例实体对齐假设搬到数据库绑定的 Concept identity。
- [Ontology alignment repair through modularization](https://arxiv.org/abs/1307.5322)：先提取相关片段再修复不一致。启发是有限影响范围；本文自然语言 description 不具备该类正式逻辑系统的完备性保证。
- [Agent-OM](https://www.vldb.org/pvldb/vol18/p516-qiang.pdf)：LLM 与检索/匹配工具结合。我们的索引、范围和事务仍由代码控制，不增加层层 Agent。
- [MILA](https://arxiv.org/abs/2501.11441)：程序化搜索、向量召回与 LLM 判断结合。可借限制 LLM 调用的思路，不能将高相似分数当自动 canonical 合并证据。
- [AgentMap](https://arxiv.org/abs/2607.27130)：同时考虑 equivalence 和 subsumption，说明 matching 不宜只有同义合并。本文还要处理数据库锚点、SQL 生成行为、显式关系重组及来源保存；不复制多 Agent 分工。

可能的贡献是：**数据库锚点约束下、保留源 case 行为的增量局部图整合**。这只是待验证的研究定位，不能凭设计宣称创新优先性或优于上述工作。

实验应把算法主张变成可测问题：

- same-identity precision/recall，尤其错误合并率；hierarchy/relation 修正是否受支持。
- 唯一绑定与契约违规率；有效信息被误删或错误泛化的比例。
- 受影响 source case 的 SQL 行为回归；不把单快照执行一致当普遍等价。
- 未决率与覆盖的 tradeoff，不能靠大量 defer 换取看似完美精度。
- 调用数、token、时间、局部任务规模；不同 case 顺序的语义结果稳定性，不要求 canonical ID 一模一样。
- 消融：无锚点召回、无局部重检索、仅节点合并、无独立 Assessment。先用 A–H 单元/mock，再用真实 case47/60 等图验证，比较现有 merge baseline。

## 10. 建议实施顺序

1. 同步所有 merge prompts 到 v59，并补 current-schema 测试；修复合法业务环与计算依赖混淆、谓词导入提前冲突。
2. 增加物理 owner/内容/反向引用索引、unknown、统一检索接口；保留现有事务和完整来源。
3. 将单 group proposal 扩展成有界多节点/边 patch，支持多 existing canonical、契约映射和 relation_metadata 同步处理。
4. 增量队列与有限重试；加入未决改动的明确输出，避免误报整合完成。
5. 上述 mock 和真实 graph 回归；不同时引入全局 schema 重设计、任意一对多拆分、完整 OWL 推理或通用子图冲突引擎。

本轮只提出和审查设计，未修改 merge 生产代码，也没有执行新的真实 merge 实验。

## 11. 本轮审查记录

三个 subagent 分别审查 identity/检索、冲突判据、流程与复杂度，随后对综合草案复审。根据反例修正了：unknown 与 different 混淆、纯关系冲突被节点 identity 限制、量词/空类误判、未提交 incoming 和 edge-only 候选遗漏、业务环调度、提案失败后缺少明确 retry、冲突分支混合造成的错误冗余推导等问题。

框架与冲突审查者在修订后确认无阻塞项，可进入用户 review。identity 审查的候选范围、单位与不同输入角色、禁止通过上位概括制造同义等意见也已纳入。该结论仅为设计审查通过，不能代替运行验证。

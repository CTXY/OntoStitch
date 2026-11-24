# Signal 驱动的局部子图选择与简化呈现

## 1. 选取单位是一个可独立判断的局部任务

输入来自三类触发：suspect 结构模式、validator 已核实错误、节点整合移交的明确保义义务。普通新节点/未覆盖节点不自动变成无目标的全面审计任务；检查覆盖通过触发器、变更影响检测和最终结构 gate 保证。这个调度变化需要代码实施，不能仅修改 Prompt。

每个触发项保留 id、kind、mode、observed、anchor_ids、check_question、required_coverage 和 evidence IDs；mode=suspect 允许 keep，mode=required 必须证明义务已闭合。观察事实不是语义结论。例如 parallel_assertions 表示两端间多条断言，不是“已发现重复边”；source_selection_variants 必须保留每个来源选中的物理输入、来源/evidence IDs，不能投影成只剩 kind/节点 IDs。

先为每个 Signal 构造不可分的必要关系集合，再合并同一决策需要一起修复的触发。不能仅因两个任务共享一列/大 hub 就递归合并全连通分量。合组后仍保留独立 signal_id，Writer 与 Assessment 分别覆盖。测量的是全部 System、工具、节点视图、详边、草稿和反馈的实际序列化大小；单个完整任务已超限时重新划分检查目标或显式保留阻塞，不截掉必要关系。

## 2. 粗节点、详关系，metadata 放在 Concept 内

### 节点摘要

每个节点提供 id、node_type、name、一两句 summary、分支目录及 available_fields。summary 明确不是完整 description。普通 Concept 可提示有几种匹配方法，但不默认展开 targets；需要比较匹配时 read_nodes 精确读。Metric 不默认发送长 SQL plans，Rule 不默认发送全部 action/reason，Event 不默认展开所有角色材料。

涉及当前选中边的 relation_metadata 要直接放在该 Concept 的 properties.relation_metadata 中：保留组 id、完整成员边 IDs 和原 description。它是该节点已经保存的组级知识，不新造 top-level relation_groups、不生成额外节点。某 Concept 仅作为邻居但其 metadata 依赖待改边，controller 也要纳入这部分必要材料。

### 关系记录

每条选中边完整提供 id、edge_type、from_node_id、to_node_id、properties（尤其 description/alternative_ids/target_id）和 evidence_ids。不为了简化而删掉限制条件、把不同 scope 的同端点边合并，或把两条不同角色的关系压成一条摘要。

已登记谓词提供当前所需的含义、方向、允许端点、真实属性约束与允许推导。仅给谓词名字不足以让模型知道理想关系。物理端点给准确表/列/值定位，目录保持只读。

### 分支摘要与细节

已有 conflict 先给节点身份和分支目录，说明当前边在何分支生效。若关系判断依赖具体分支含义，就读取完整解析的那些分支；准备 node_rewrite 时必须读取全部旧完整内容/来源单元和必要关系，不用有限分支投影重写全部。原始source_node_id已在before退役时，通过controller授权的source归档快照精确读取，不凭旧ID制造活节点。before 和 candidate 同 ID 的记录分别绑定快照，不彼此覆盖。

## 3. 各类任务的必要关系集合

| Signal/义务 | 关系集合与覆盖声明 |
| --- | --- |
| abstract_predicate | 全部触发边、两端摘要、该抽象家族/候选具体谓词契约；不扫描所有同类业务 |
| binding_contest | 同一物理 ID 的全部 Concept grounded_on 边及各节点/起点分支目录；coverage 明确物理 ID 与全部 claimants；保留其各自匹配定义待按需读 |
| parallel_assertions | 涉及端点间的全部候选边、完整谓词/描述/scope/来源；涉及边的 Concept metadata；相同端点只是候选分组 |
| triangle | 三条路径边及完整范围、允许的推导规则、相关 metadata；不能删 direct edge 后用已删边自证 |
| same_role_neighbors | 起点到相关多个邻居的同谓词完整边集、必要反向角色及起点 Concept metadata；Event 没有 relation_metadata 字段 |
| related_decisions | 相关 Rules 的 applies_to 边、共同作用对象、分支/condition/action可读目录；共享对象不自动说明identity相同 |
| source_selection_variants | 当前 Concept 的相关表示/消费者边，按来源列出实际选中输入及证据；差异不是缺失的证明 |
| value_concepts | 同列的值类别绑定、准确 DimensionValue/value_of 定位、已存在层级/业务边；不把有限值样本假定穷尽 |
| Metric输入错误 | 所有 direct depends_on/grounded_on、公式 refs 的端点目录及分支对应；validator expected/missing/stale 作为 observed，决定修复前读真实 expression |
| metadata/终点conflict/node followup | 义务原边/目标版本/组描述与当前映射全部保留；controller主动计算影响，不能指望粗略节点Writer已经知道全量入边 |

coverage 记录 selected范围及分页状态，绝不把局部未出现解释为全局不存在。对同一个物理绑定/Metric直接输入等可确定范围，controller可以直接一次列齐边，减少模型重复inspect。节点详细业务内容则按判断需要读取。

## 4. 写范围和读取范围分别维护

初始任务给范围规则和锚点。request_context 扩展出的只读节点不自动可写。controller 根据具体 change 的关系耦合、节点引用、metadata 和来源义务批准必要更新；无关全局ID即使存在也拒绝。

修改节点字段须读取该字段完整 before 值及决定其含义/消费者的相关字段；重写完整内容/alternatives 须读取完整内容和对应来源单元。普通 rename 不强迫读取全部 SQL plans，但重写定义不能把未读字段省掉。新增或实质重构先取得 write_contract，不能由通用关系指南猜结构。

## 5. 编译后重新选择实际影响，而非复用修改意图

候选编译后，controller 计算真实增删改、指向改变节点的入边、Metric/Rule内容引用消费者、受影响 Concept metadata、分支scope和来源去向。这些组成 Assessment 的最小影响闭包；如果初始摘要没暴露某个消费者，controller主动补入，不能把节点Writer未见当成无影响。

候选与before相同也不能跳过评估。Assessment同时收到原Signal完整事实、当前关系集合和Writer keep理由，逐项判断；它也可以因最初Signal是合法结构而accept。新出现的当前修改回归即使无旧Signal，也在affected closure内形成issue，不能因此变成全图理想化审计。

## 6. Working view 的具体约束

原始图由controller保存；API只序列化一次canonical working view。初始摘要在读取详情后被精确字段替代/补齐，同对象/view/snapshot/字段不同时保留摘要和同字段完整值。完整content读取后回收相同对象分散字段副本；before/candidate相等字段可以共享controller记录引用，但不能把不同快照内容误并。

一个被退休对象在before存在、candidate不存在，应明确列出其删除和语义后继映射，而不是把它当作两个存活副本。候选展示不能由模型手写。relations中的来源IDs与evidence正文映射分别存放，正文去重不改变哪项断言由哪个来源支持。

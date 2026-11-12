# Extraction training 修改方案

日期：2026-11-03。状态：实施前提案，保留供追溯；授权后的实际修改与验证见下方链接。

> 后续已按用户授权实施。具体已选字段、代码逻辑、验证结果与外部服务阻断见 [修改记录](ontology-graph-implementation-status.md)。下文保留实施前提案，不能把当时待 review 的字段当成此前已确认的设计共识。

目标规范为 [Ontology graph 设计与抽取规范](ontology-graph-design.md)。先统一结构契约与关系生命周期，再修改 Agent guidance 和 Assessment，最后用 BIRD cases 验证。仅改 prompt 无法解决现有 refs 编译、端点限制和公式引用类型问题。

## 1 当前代码与目标的差异

| 当前入口 | 已核对的现状 | 必要修改 |
| --- | --- | --- |
| models/semantic_mapping.py | Concept.targets 默认空；target.references 只允许 Column/Value；target 强制 sql_expression | 非空 Concept binding；支持 Table 和多来源标识；删除 references 编边用途 |
| models/metric.py | expression.refs 的文字契约主要是物理节点或 Metric；另有重复 references | 支持 Concept 操作数；删除通用 references，保留 expression.refs |
| models/generation_rule.py | 单字符串 operation_intent，另有 references | 支持多个标签与新 label 定义；改用显式 applies_to 等边 |
| models/event.py | 可空 targets，但非空时强制完整布尔事件谓词 | 将事件创建门槛与发生识别 target 分开；不要把属性绑定强行变成布尔谓词 |
| schema/relationships.py | reference_relation_type 和 synchronize_reference_edges 根据引用生成默认边 | 移除语义引用推断；保留确定性 catalog 归属构建与一致性检查 |
| application/ontology_graph.py | 状态准备与片段写入调用关系同步器 | 原子检查节点、绑定和显式边，不默默恢复默认语义边 |
| schema/graph.py | GraphEdge 已有 properties 和 reference_origin | 沿用 edges；统一要求 properties.description，清理旧 anchor override 生命周期 |
| learning/inputs.py、prompt_rendering.py、tools.py | 向 Agent 暴露 refs 默认关系、类型 guide 与 registry | 暴露新契约、边说明、可扩展谓词与 intent |
| prompts/asset_contract.py 与 learning guides | 允许无绑定 Concept；Event 门槛宽；单 registry intent | 与新规范统一，移除相冲突的旧示例 |
| training/assessment_* 与 assessment.md | 仍容许 unbound Concept，事件参与信息不是创建前提 | 同时评估硬规则、最小化、事件证据及全图结构 |

上述路径除 schema 与 application 外，均相对于 src/cortex/capabilities/sql_training。现有独立业务边与 edge_operations 可以复用，不需要另造图容器。

## 2 建议的契约选择

### 2.1 Binding 与 edges

保留 Concept.targets 作为匹配定义，按用户澄清采用嵌套分组方向：[[A, B], [C]] 中每个内层列表是一个完整匹配项，列表内共同参与匹配。不同 Concept 的 targets 允许重合，不按引用逐项生成 grounded_on。元素的序列化格式与必要表达式如何关联，仍需 review；不能只存分组却丢失值条件或跨表对应依据。

Table/identifier 等直接绑定不应被迫编写布尔 SQL。表达式具体字段名、是否需要 binding kind，应先用四类实例检验：表对象、标识字段、编码取值、联合年级边界；不为可由端点类型判定的信息重复增加枚举。

target_id 是否保留由序列化需要决定，不作为 target 与边一一对应的前提。系统 provenance 与分支信息仍应保留，不因删除 qualifier 提议而删除必要的快照元数据。

模型更新、targets 更新和相关边更新一起提交；最终状态校验失败时整批拒绝。删除显式边后不自动恢复旧默认边。targets 出现某个端点本身不要求该 Concept 必须有一条直接边；公式操作数与显式 depends_on 的一致性要求单独检查。

以 Agent 直接生成全部正式边为主。可以选择在写入后启用确定性漏边检查/补全，主要检查保留的 expression.refs 与 catalog 归属引用，不恢复通用 refs 主导构图。只补缺失、关系含义唯一且不与显式边冲突的连接；不猜业务谓词，不按 targets 全量补 grounded_on。补全结果先保存，再执行 Assessment/replay；删除边是否与公式矛盾应反馈或按明确的兜底开关处理，不继续采用旧的默认/override 自动恢复机制。

### 2.2 绑定唯一性

校验图上的直接连边，而不是 targets 的重合度：对同时生效的 Concept → grounded_on → 物理字段/值，按物理端点统计不同 Concept 的数量，最多为一个。同一 Concept 可以连接多个端点。表端点是否采用完全相同的计数约束可沿用总体原则并在契约实例中核对，不能与字段/值身份混合。

两个 Concept 的 targets 可以引用相同端点，匹配信息不因直接边已存在而删除；业务关系说明它们之间的实际联系，不为绕过校验编造关系。跨 case 对齐不在 extraction 阶段构建全局锁；互斥候选分支按分支检查，不将未同时生效的定义当作共存连接。

无需引入“直接所有权”与“辅助使用”的新角色字段：按实际 grounded_on 连接计数，不按 SQL 列名、target 引用次数或 description 中的身份措辞决定唯一性。补充“targets 重合但直接连边不冲突”的正反例。

### 2.3 计算引用

建议 expression.refs 支持 Concept、Metric，以及需要时的直接物理输入。语义引用必须有显式 depends_on；直接物理输入对应 grounded_on。一个公式变量绑定一个确定节点，公式变量与 refs 双向完整匹配。

本设计不要求通过已识别依赖边自动补齐节点；图读取使用明确保存的节点和边。Metric 的实际计算由静态检查与 Assessment 联合判断，不使用“字符串含运算符”作为充分条件。循环检查只施加到有明确 DAG 语义的计算依赖，不能套用到所有业务关系。

### 2.4 关系描述与谓词扩展

建议保留当前 GraphEdge 字段，强制 properties.description 非空。catalog 边由导入代码生成明确描述，业务边由 Agent 编写。没有 qualifier，也不强制 role 字段。

保留固定关系的精确端点约束；新业务谓词允许即时使用，要求具体 description、合法端点与一致方向。提供轻量可复用谓词词表作为推荐，不先增加必须审批的图内 RelationType 节点。自定义与内置关系都属于显式图内容，不以“是否被依赖边白名单识别”决定其能否保留和呈现。关系名称不单独构成 SQL/JOIN 执行指令或传递性公理。

旧 broader_than 和统一 has_participant 示例从默认 guide 中删除。is_a、opens、paid_from 等用有证据的示例说明；不能把示例名称变成所有领域的必选词。

同一事实修订 description 时复用边 ID；但同一 source/type/target 不足以证明适用范围相同，不能仅按三元组静默覆盖描述。条件与范围仍写在 description；遇到不同声明时保留差异并反馈审阅，不在本轮新增 qualifier 或复杂的条件身份机制。去重与来源、分支的具体处理仍需实施 review。

### 2.5 Operation registry

建议改为 operation_intents 列表，标签是字符串。已有 registry 标签可直接引用；未注册标签随编辑提交简洁定义。新标签先记录为候选、供其他轮次发现而不立即改全局 registry，是待 review 的存储建议，不是已经确认的生命周期要求。

标签列表不是行为列表：规则仍需要一个完整原子 action。Direct 与 Repair 使用相同的多标签能力，SQL-side triggers 等已有阶段性证据边界保持独立审查，不因标签开放而取消。

新标签定义、重名冲突与同义合并由一个共享辅助契约处理；标签不是新 ontology 基类。字段名和候选存储格式需要 review 后定案。

### 2.6 Event

已确定 Event 需要充分且可关联的事件结构证据。是否强制独立 targets，或允许通过已绑定参与者与时间/地点 Concept 及显式关系落地，尚待 review；后一种是候选实现，不作为共识。新事件关系是否能正常保存和呈现属于读取适配问题，不用旧依赖闭包限制事件设计。

校验分为结构可解析与语义证据充分两部分。静态检查不能仅数两条边就认定事件成立；Assessment 应确认行为、角色、发生上下文及其联系。不要把必填 Event.targets 作为替代门槛。

## 3 分阶段修改

| 阶段 | 修改范围 | 可 review 的交付与退出条件 |
| --- | --- | --- |
| A 契约与最小示例 | root schema、节点模型、绑定格式、边格式、intent 字段 | 四类 binding、Concept 公式操作数、新业务角色边均可往返保存；禁止旧通用 refs |
| B 写入与生命周期 | GraphManager、references/relationships、graph_io、快照、依赖读取 | 原子编辑、删除、撤销和分支一致；不自动恢复默认语义边 |
| C Agent guidance | shared contract、四类 Markdown guides、relations、Direct/Repair | 一个来源维护规则，逐步加载；无旧指导冲突；registry 可参考及扩展 |
| D Assessment | assessment inputs/models/views、prompt、接受控制 | 节点局部检查与全图语义检查并存；SQL 成功不能覆盖质量缺陷 |
| E BIRD 试验 | 47、49、60、76、77、financial 104 | 原始 evidence、探索、节点、边说明、SQL replay、问题与修订全部可审阅 |

每阶段先完成与其直接相关的有意义测试。schema 与生命周期稳定后再运行有成本的模型试验，避免用 prompt 反复掩盖结构问题。

## 4 Guidance 的组织

建议保留现有 progressive disclosure，不先引入另一套动态 skills 框架：system 给短总则；类型 Markdown 给定义、最小化、有效例与反例；relations guide 给固定关系及业务扩展；GenerationRule guide 同时加载 registry。

asset_contract.py 可以作为共享文本入口，但应避免 Pydantic 描述、root 描述、Markdown 和 Assessment 各维护一份不同规则。精确结构来自模型/schema，语义 guidance 来自共享契约，Assessment 使用相同定义并增加整体评估问题。

不加入学校专用的状态/属性/类别决策树。学校案例放在 examples，展示证据驱动的不同合理表达。

## 5 评估与验收提案

硬检查包含：非空且有效的 Concept 匹配分组、直接物理连边唯一性、公式 refs、边说明、端点类型组合、悬空引用、生命周期一致、禁止旧 refs/qualifier、metadata 未写入。targets 重合不算冲突。

语义 review 每项 0–2 分：证据一致性、Concept 最小化、Metric 正确性、Rule 原子性、Event 门槛、全图层级与角色、关系描述、数据与时间解释。0 为明显错误，1 为有实质不足或待确认，2 为满足；不适用项单列 N/A，不靠缺少类型获得满分。

不增加必要知识覆盖率评分或类型配额。遗漏主要通过实际 SQL 错误、明确的不一致及其诊断发现。Assessment 首期不新增探索 evidence 数据流，不强制读取 probe、重新探索或逐边验证查询结果；沿用已有输入，确有不确定之处给出反馈。Agent 的只读探索与正常训练日志保留，不扩展为新的证据子系统。

建议通过条件：没有关键缺陷；适用项中没有 0；证据、绑定语义、最小化和事件门槛不得留实质疑问；剩余问题明确记录。分数辅助审阅，不将总分阈值当作自动正确证明。独立 reviewer 评估未被人工修饰的实际输出，并允许与示例不同的合理图。

| Case | 重点检查 |
| --- | --- |
| schools 47 | 月均是请求操作还是稳定指标；opens 事件角色；COUNT 原口径 |
| schools 49 | 状态与缺失关闭日期；没有长 Metric 或整查询 Rule；平局范围 |
| schools 60 | 两类学校共享身份但绑定不同值；没有虚假互斥、交集节点或 Event |
| schools 76 | grade range 非空绑定；跨表身份与记录粒度；不同分类维度不等同 |
| schools 77 | 存储量为 Concept，比例为 Metric；公式引用 Concept、年龄口径一致 |
| financial 104 | 交易时间与开户时间；具体但有证据的账户角色；不虚构币种 |

每个 case 交付原始 NLQ/SQL/evidence、必要 metadata、只读探查及结果、实际图、逐边说明、replay 和 review。保留失败轮次，不人工修图冒充 Agent 输出。旧通过报告仅作历史比较，不继承其结论。

## 6 影响范围与测试

即便只优化 extraction，schema 变化仍影响图序列化、online 投影和 merge 消费。先增加最小适配，保证这些模块读取同一版本的显式节点和边，不因关系名称未在旧依赖白名单中而丢失业务结构；不在本轮重设计 merge 算法。现有按依赖类型补齐节点是代码现状，不是用户规定的构图要求。

回归重点是：多来源 School 标识、targets 重合且连边唯一、联合匹配分组、非空 Concept、存储量误标 Metric、Concept 公式操作数、必要显式边缺失反馈、description 必填、不同业务角色边、多个和新增 intent、节点更新/删边/撤销、候选分支、快照指纹、下游显式图完整性与 metadata 不变。

旧格式不能默默转换为看似符合新设计的业务图。建议更新 schema fingerprint，旧训练快照保留为历史文件，并重新生成 review cases；是否需要一次性离线转换另行决定，不先增加运行时兼容层。

## 7 实施前 review 项

1. targets 采用内层一项一种匹配的嵌套分组；需 review 元素精确格式和联合条件如何保存，不强制 target 与边一一对应。
2. 唯一性已经明确针对直接连边，允许 targets 重合；需 review 分支范围与正反例，不再设计“所有权”例外。
3. 同意 operation_intents 为多标签列表，新 label 连定义进入候选记录，不自动发布全局 registry 吗？
4. 同意沿用 GraphEdge.properties.description，不增加 qualifier，业务谓词允许即时扩展吗？
5. 同意先实施 A/B，再改 prompts 与 Assessment，最后重新运行六个 BIRD cases 吗？

这些是具体实现选择，尚未当作用户已经批准的字段契约。建议按此顺序 review，避免在 Agent 实验后再修改整个存储结构。

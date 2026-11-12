# Ontology merge v64：显式身份判断与局部图整合

> 2026-11-10 补充：[匹配、使用决策与输出形式的边界](ontology-decision-boundaries.md)。本文中的 Concept/Rule 准入、复合规则整合与相容性判断以该补充为准；实验通过与否单独记录。

日期：2026-11-07。

## 当前状态

已修改默认 live merge 入口，接入新的图批次整合流程；旧的逐节点路径仅为显式注入的历史测试/消融及离线兼容保留，不作为新流程失败后的 fallback。

已获用户明确授权，将 BIRD california_schools case 47、60、77 的 extraction 图、NLQ/SQL/evidence 与 prompts 发送至 `https://api.deepseek.com`（deepseek-v4-flash），关闭 LangSmith。真实模型合并已完成，最终结果为 `output/ontology_merge_v64/live_r4/`：结构校验通过，独立 Assessment 经一次修订后 accept，subagent 复核未发现阻塞性语义问题。没有人工改写最终图 JSON。

## 1. 执行流程

```text
来源隔离、物理对齐、候选检索
  → 显式 identity 判断
  → 同身份内容整合 + 相关结构修改
  → 代码构造 candidate + 独立 Assessment
  → 原子提交图与映射结果
```

identity、整合、Assessment 各是独立调用；前两者复用 Merge 角色，Assessment 使用独立上下文。内容反证可以返回 identity。结构错误与评估反馈修订提案；预算耗尽或表示能力不足使运行失败，不产生正常 U，不发布未通过的图。

LangGraph 保存阶段检查点，包括输入、证据、identity、提案、候选、评估与最终结果。重放要求图、schema、代码/prompts 和运行配置一致。

## 2. 代码层次

| 层 | 文件 | 职责 |
|---|---|---|
| 公共入口 | `src/cortex/bootstrap/graph_merge.py` | 默认 live 模式构造新 engine；沿用 `.merge(existing, incoming, schema)` |
| 编排 | `capabilities/graph_merge/integration/engine.py` | 显式阶段、有界反馈、候选刷新、checkpoint、提交 |
| 数据契约 | `integration/models.py` | identity 分组/区别、标准 edits、changes、独立评估 |
| 上下文 | `integration/context.py` | 来源 handle、物理身份对齐、检索、邻接与反向引用索引 |
| 契约导入 | `integration/contracts.py` | 来源局部的同名不同义谓词隔离，不覆盖 root |
| 工具 | `integration/tools.py` | 分页节点/关系/消费者/候选/契约、来源证据、按需规范 |
| 修改编译 | `integration/compiler.py` | 安全重映射、显式图批次修改、来源对应、结构校验、发布 ID |
| 模型调用 | `llm/integration.py` | Markdown prompt、结构化输出、有界工具调用、交互 trace |
| 图事务 | 原 `transactions.py` 与 GraphManager | 复用原子验证、revision 和完整图更新 |

没有将业务语义判断塞入编译器；它不规定某种学校应是类别还是属性，也不按 case ID/县名/SQL 模板决定合并。

## 3. Identity 和候选

物理对象按 schema 的 exact identity 字段对齐；列先对齐表，Value 先对齐列。短 handle 可逆，真实来源 ID 保留；提交时恢复稳定的来源 ID，避免将临时模型编号发布为规范图 ID。

语义候选使用名称/定义相似度、共享物理或语义引用、关系方向/角色作为召回信号。Concept 直接绑定争用完整列为 mandatory 比较。信号不决定同义。

Agent 显式返回 groups（成员、共同身份、理由）及必要 distinctions。不同描述、实现或口径不能直接拆身份。不同类型的纠错通过明确结构改写完成，不自动变成跨类型 alias。

当前局部范围是本次 incoming 批次的语义节点、边端点与其候选；existing 图的其他节点按需读取。并非固定 k-hop，也不展开整个连通分量。修改后的名称/别名可触发对完整索引的新候选检索；新增候选先返回 identity，避免未判断即提交。soft top-k 不证明全库无漏配。

拆分或新建的语义节点也会检索 source 索引。新命中未读来源时，将“新节点 → 来源候选”反馈 proposer，决定复用或保留；不把临时创建的节点塞入不可变来源 identity。原始节点出现新的匹配候选仍返回独立 identity。两类反馈可同时保留，无需额外阶段。

## 4. 图修改和两种映射

代码先构造临时 base：保留输入，做已确认的 identity 重映射，再归并**属性、描述、端点、谓词均相同**的重复边。base 只是待修正输入，不被发布；不同含义的内容仍需 Agent 整合。

Agent 使用 GraphManager 既有 create/update/delete_node、set_conflict、create/update/delete_edge 操作词汇。updates 是完整对象。节点和边可以同批修改，无须所有业务边构成 DAG。

`GraphMergeResult` 增加：

- `rewrites`：before/after 的类型化来源引用、可选 alternative_id、解释、前后 revision。
- `existing_node_mappings` / `existing_edge_mappings`：原有全局节点/边受到合并时的对应。
- 原 `node_mappings` / `edge_mappings` 保留，用于输入对象的安全单值对应，不再要求每个对象必须有唯一映射。

边到路径、一个 Rule 拆成多个 Rule 通过 changes/rewrites 表达。该记录不是 alias、业务边或推理引擎；不会将 before 的来源按笛卡尔积传播给所有 after。仅有一个 before/after 也不自动构成同义映射。

仅 schema 声明的引用槽位可机械重映射，包括 expression.refs、targets 和 relation_metadata 的边引用。不会替换 SQL 字符串和描述文本。删除不级联静默丢边；非一对一消费者必须显式修正。变更竞争版本时须说明具体 alternative 的去向。

发布前将临时 handle 转为最终 ID，随后验证提交的语义内容与 Assessment 看过的候选相同。映射、结构对应和图一起进入最终 checkpoint/result；任何失败都不返回成功图。

## 5. Prompt 与上下文

三个 Markdown prompts：

- `integration_identity.md`：身份判断标准与检索工具。
- `integration_propose.md`：内容差异、完整定义、标准 edits 与结构对应。
- `integration_assess.md`：决定正确、候选质量、修改保义三个明确检查项，由一次独立 Assessment 完成。

每次修订只传当前来源上下文、上一提案和具体反馈，不重放全部历史。候选目录只传一次 ID；重复评分解释按需读取。长来源 ID 用可逆 handle 表示。

三个 case 的初始输入审查中，payload 从约 66 KB 降至约 42 KB（不含随后按需读取）。定义与业务关系未删；主要去除长 ID 和重复候选说明。最终精确计数见运行目录 `context_audit.json`。

operation registry 不自动注入。`read_guidance` 按需读取当前 extraction 类型规范、关系规范、operation registry 和冲突写入 schema；避免过时的 target.sql_expression 指引。`read_evidence` 现保留 BIRD 补充 evidence，不仅 NLQ/SQL。

高连接度节点的定义和关系分开读取；分页明确 total/next_offset。输入与候选是独立视图；消费者包含节点引用、相邻边和 relation_metadata 引用。所有读取可在 trace 中审计。

## 6. 校验和评估的边界

代码校验引用、字段、端点、绑定唯一性、明确禁止的循环、竞争版本、关系组、来源去向和事务一致性。代码不判断自然语言语义等价。

Assessment 对 identity、条件解释、冲突/纠错依据、节点最小化、完整定义、结构保义及受影响消费者作判断。候选质量包括保留不变的 incoming 语义内容，不能仅检查显式 edits。输出 decision_check、quality_check、preservation_check；不要求证明所有潜在知识均已抽取。只允许结论与具体 issues 一致的 accept。

本次未引入：

- case-specific deterministic 修复或业务分类树。
- metadata store 更新或优化。
- Constraint/Process/Entity 新节点、全局最小图优化、通用 OWL reasoner。
- 新的纯关系 alternatives 存储格式。无法用现有图内表示保存的真实关系/多对象竞争以 representation_error 终止，不能声称成功，也不能把互斥事实并列发布。
- 通用数据库查询工具。现有证据工具提供 NLQ/SQL/补充 evidence；不能声称观察了未提供的 NULL、唯一性或分布。需要此类证据的合并判断仍是实际试验要检查的能力边界。

## 7. 测试与真实试验入口

新增 `tests/test_graph_integration.py` 覆盖显式 identity、绑定争用、同义合并、边到路径、Rule 拆分、消费者/关系组修复、业务环、分页、独立评估、失败不发布、checkpoint、来源契约隔离和 edge-only 更新。

最终生产代码的全套测试为 **933 passed**（1 个既有 Pydantic 弃用告警）。随后新增真实 Agent 包装层的 scripted-model 测试，专项共 **15 passed**，确认工具绑定、结构化输出和阶段上下文隔离；该测试不调用外部模型。Ruff 对本次修改文件通过。不能用这些确定性 fixture 证明模型语义准确率。

真实运行反馈及末次上下文裁剪后的最终回归为 **941 passed**，其中 integration 专项含 **22 项测试**。新增覆盖编辑包装契约、证据压缩保留来源、candidate 目录、新建节点的召回反馈、物理节点/边改类型绕过防护，以及物理证据读取不会扩大候选展示到整个目录。独立代码复审通过；最后一项上下文展示修正另经全套回归验证。

通用 runner：`scripts/run_graph_integration_review.py`，接受 3–5 个 `--case-workspace`，各目录包含 incoming_state.json 与 inputs/sql_training.qa_sql.jsonl。`--prepare-only` 只准备与审查输入，不调用模型。

已准备输入位于 `output/ontology_merge_v64/prepared/`：303 个来源节点、318 条来源边；物理对齐后有 20 个语义节点、6 组直接绑定争用。`context_audit.json` 记录初始 payload 为 42,992 字符，模型调用数为 0；未包含 operation registry。`prepared_identity_context.json` 是准备提交给 identity 阶段的实际上下文，不是已评估结果。

本次选定：

| Case | 原始问题涉及内容 | 合并观察点 |
|---|---|---|
| 47 | 1980 年 Alameda 的小学学区学校开办月均数量 | School identity、事件/日期、业务口径与 Rule |
| 60 | San Joaquin 部分虚拟 chartered 学校的网站 | School 复用、独立类别、县和网站的业务关系 |
| 77 | Los Angeles K–9 学校的 5–17 岁 FRPM 比例 | School/County 对齐、存储输入 Concept、计算 Metric 与 Rule 边界 |

实验检查真实 trace 的 identity/提案/Assessment、实际保存图、对应记录、工具可达性、token/调用量和语义错误，发现问题后修改通用实现并重跑。真实运行使用保存的 prepared 输入快照，可通过 `--prepared-input` 重放，避免依赖已删除的历史测试目录。

## 8. 真实调用发现与修复（持续更新）

第一轮 `live_r1` 的 identity 已找出跨三个 case 的 School/County 同身份，但图修改输出连续把 `operation` 写成 `action` 或 `op`，并把节点内容平铺到编辑包装层。模型未获得足够明确的接口约束；该轮已停止，未发布结果。修复为 `GraphEdit` 结构化外壳，按操作要求 `node`、`edge`、`node_id` 或 `edge_id`，拒绝混用；prompt 给出准确的嵌套写法。节点内部仍由既有 GraphManager/schema 校验。

实际 trace 还暴露两种上下文重复：同一 QA 内容因来源 locator `qa`/`QA` 被重复读取；已在初始 payload 中的候选定义又在 candidate 查询中完整返回。现在相同 evidence body 合并展示，保留全部 source_refs；已展示候选仅返回 ID/检索信号，未展示者仍返回完整定义。空 IDs 的 nodes 查询默认提供语义节点目录，物理节点可按 ID 明确读取。无业务知识被删减。

`live_r2` 初次 identity 调用数从 4 降为 2；单次最大输入从 55,015 降为 15,406 tokens。不同轮次模型调用具有随机性，这只是本次观测，不是受控效率基准。18 项 integration 测试通过，包含新增的编辑契约和上下文压缩回归。

`live_r2` 完整真实合并与独立 Assessment 通过：一次 identity、一次 proposal、一次 Assessment，共 18 次底层模型调用，累计输入 528,301 tokens（包含工具往返中的重复上下文，不等于独立信息量），输出 47,097 tokens。结果为 16 个语义节点（12 Concept、1 Metric、2 Rule、1 Event）、18 条业务边，8 项结构改写；包含物理目录时为 115 节点、129 边。

但独立人工式复审仍发现两个语义问题：计数 Rule 保留不决定其行为的请求值与邻近除法示例；FRPM Rule 把 Gold 指定计算方式过度解释为已证明存储列不同。Website 关系也仍以“字段被记录”为主。已改进通用提案/评估指引并启动 `live_r3`：只保留决定实现的条件，区分任务口径与实证差异，解释业务角色，并允许同义来源契约归一。没有按 case ID、学校类型或字面值写替换规则。

独立代码审查还修复了物理对象保护漏洞：除了新对象，也检查修改前快照，防止将 table 改成 Event 或将物理目录边改成业务边以绕过只读检查。所有修改经过既有 schema/GraphManager 校验，不引入额外业务判断。

`live_r3` 的结构和模型 Assessment 再次通过，但复核发现质量要求没有真正执行：评估结论集中于 identity 与消费者保义，漏过未编辑的 Rule 和重复泛化关系。因此没有把模型 accept 当作语义终点。后续修正为同一次 Assessment 中三个明确输出项：decision_check、quality_check、preservation_check，显式涵盖保留的 incoming 内容；不增加 Agent 或状态机。同时移除评估 payload 内重复的 edit bodies，仅保留 changes 解释、原图与实际 candidate。`live_r4` 用同一输入快照检验该修正。

proposer 的 candidate view 是 identity 映射后的 base，不含 previous_proposal 的未接受 edits。控制器在提案提交后对新建节点检索；修订时提供新节点到 source 命中的对应，proposer 根据反馈复用或保留。Assessment 的 candidate view 才是实际已编译提案。

## 9. 最终结果与实际限制

`live_r4` 完成一次 identity、两次 proposal、两次 Assessment。第一次 Assessment 返回 5 项具体修订意见，第二次 accept，之后原子提交。最终有 12 Concept、1 Metric、2 GenerationRule、1 Event，共 16 个语义节点、18 条语义节点间关系；包括目录节点/边时为 115 节点、129 边。8 项结构改写分别记录合并、边归并和内容纠错的来源去向。

主要结果：

- 三个 School 合成一个学校记录概念，CDSCode 与 School name 仍是两项独立 targets；名称是 nullable、未断言唯一的标签，不把名字当实体键。
- 三个 County 属性合并，School→County 的泛化属性边由 located_in 承载，保留分类角色独立、开办时间及其他报告期的限制；去掉与物理列非空信息不一致的缺失值说明。
- 计数 Rule 保留 COUNT(School) 的操作数/NULL 选择，去掉偶然县名、年份、学区过滤和 /12 示例。
- FRPM Rule 保留 Gold/evidence 指定的 count/enrollment 计算口径，不再宣称已证明 stored percentage 不同；Metric 表达式、两个输入 Concept 与依赖保留。
- Charter/partially virtual 类别和 School opening Event 保留原有不同业务含义，没有为了缩图而强制合并。

底层共 24 次模型调用，累计输入 779,058 tokens、输出 104,628 tokens；累计数包括工具往返重复上下文。该小批次仍有明显成本，不能据此声称大规模性能优秀。最终二次 Assessment 的输入在 trace 中还出现了已读物理目录的重复展示；运行后修正 candidate 展示范围仅为语义节点，目录仍可用工具读取，新增回归验证。此最后的上下文裁剪未另付费重跑；已交付 graph_state 未被改变。

审查入口：`live_r4/index.html`；完整交互图：`live_r4/graph.html`；原始结果：`graph_state.json`；映射与结构对应：`merge_report.json`；完整阶段与模型交互：`workflow.json`、`agent_traces.jsonl`；测量：`review_audit.json`。

已知限制：本轮是向空全局图整合三个已有 case 图，并非大库增量召回基准；未重跑下游 NL2SQL/SQL 执行；没有真实竞争口径的实测样本，相关路径由通用回归覆盖。有限 top-k 不保证无漏配。website 的泛化描述、Rule 中 T1 示意别名，以及同义但来源隔离的 has_attribute 契约仍可进一步整理，复核未将这些表达问题判为实质语义阻塞。旧快照中的历史哈希 Value ID 原样保留，本轮没有重新启用默认 SHA-256 ID 生成。

## 10. 审查后修复（2026-11-08）

方案经过独立 subagent 审核，沿用 identity → proposal → validate → assess → commit，不增加决策阶段。职责仍为代码管理引用、实际变更范围和结构安全，Agent 判断业务语义。

### 实际变更必须进入 Assessment

新增 `integration/assessment.py`，比较 identity 对齐后的 base 与编译后的 preview，机械生成节点/边的 created、updated、deleted 目录。新增或修改的节点、边完整呈现；删除项提供 before-image；新关系契约单独呈现。普通未修改邻接仍最多初始展示 60 条并支持分页。保留 incoming 和已经读取的受影响知识审查，不依赖提案作者的 `changes` 来决定可见范围。因而纯新增且 `changes=[]` 的对象也不会漏过评估。

### 类型纠正必须使用明确 rewrite

compile 禁止现有 ID 的 node_type 原地变化，包括 incoming-only 和 identity 合并后的规范节点。反馈要求使用新 ID 创建、删除旧节点、修复引用并记录 rewrite，避免把跨类型纠正伪装成安全 identity alias，也避免只在提交时才发现 CRUD 不接受类型变化。物理对象仍只读。

### 默认物理边来源映射完整

GraphManager 提交时按公开节点 ID 重建默认物理目录边，可能改变边 ID。commit 后通过规范端点和谓词恢复原始默认物理边的对应；只处理默认且两端均为物理节点的来源边，不对业务边推断三元组等价。找不到唯一最终对应时不发布结果。原有结构 rewrite 与业务边去重规则保持不变。

### 上下文展示局部化

初始 evidence_directory 仅包含局部节点、其 alternatives、全部相关边的 sources；local_contracts 仅包含相关谓词。底层证据仍可通过 read_evidence 按来源访问。relations 空 IDs 默认读取已读语义节点和新增 candidate 节点的邻边；物理目录仍支持显式 IDs 查询。当前证据仍一次性加载，本修复不引入懒加载框架或改变召回算法。

### 泛化判断标准

用户确认：去掉偶然条件后，只要已提供的 training cases 没有反例，允许 Rule 泛化。不得用假想数据反例要求规则收窄。之前对计数 Rule 的过宽指控撤回；它并非当前 training cases 证实的错误。这不允许捏造已观测的 NULL、唯一性或字段差异，也不允许忽略明确的来源限制。proposal 与 Assessment prompts 均同步此标准。

### 回归与真实运行结果

全套回归 **958 passed**；随后追加 authored ownership 覆盖默认物理边的回归，commit integrity 专项共 **10 passed**。Assessment 可见性专项覆盖纯新增且无 changes、超过 60 条边的实际写入、删除 before-image 和新契约。局部上下文专项验证 41 个来源案例只展示局部 2 个来源目录，远端证据与物理关系仍可显式读取。独立代码复审通过，未增加 case-specific 业务判断。

`live_r5` 使用相同 BIRD 47、60、77 输入和已授权模型服务真实重跑，完成 **1 次 identity、3 次 proposal、3 次 Assessment**，最终 accept 后提交。修订循环实际修复了业务边合并后引用来源不全、FRPM Rule 对存储列的无证据推测，以及通用/具体谓词重复表达学校所在县和同义属性谓词未统一。计数 Rule 的泛化在 Assessment 中明确接受。

最终 **16 个语义节点（12 Concept、1 Metric、2 GenerationRule、1 Event）、18 条业务关系**；含物理目录为 115 节点、129 边，9 项 rewrite。独立 subagent 对最终图复审通过。县属性与两类可重叠学校类别在完整图中保留不同业务维度，不要求逐字保留原 description 的 `independent`，更不推断统计独立或所有属性组合都可成立。

新增只读审计入口 `scripts/audit_graph_integration_review.py`，通过原始输入、最终映射和逐轮 trace 核对后写入 `integrity_audit.json`：**274/274 默认物理来源边均有正确最终映射**；全部来源节点/边具有安全对应或明确 rewrite；安全节点映射类型一致；三轮 Assessment 实际创建/修改/删除对象和新契约均可见。本轮真实合并未新建节点或原地改类型，此类漏洞由定向回归触发验证，不能把未触发当作真实案例覆盖。

本轮累计 **44 次底层模型调用，1,539,617 输入 tokens、123,636 输出 tokens**，包括工具往返重复上下文。局部目录裁剪通过回归，但本轮多了修订与工具读取，总成本比 live_r4 高；不据此声称整体运行效率已提升。后续成本优化应另行测量，本次不扩大算法架构。

审查入口：`live_r5/index.html`；完整图：`live_r5/graph.html`；机械审计：`live_r5/integrity_audit.json`。未手工修改模型生成的 graph_state；未重跑下游 SQL 执行或评价 NL2SQL 准确率。

## 11. 上下文与增量修订优化（2026-11-08）

实现与可控对照详见 [上下文与修订成本优化](ontology-merge-context-economy.md)。新增 packing 与 revision 两个小模块，保留原编译器和独立 Assessment；修订基于最后成功编译的候选图。固定 live_r5 语义决策回放后的最终图、来源映射及 rewrites 不变。七阶段初始 payload 的本地 token 代理合计减少 13.87%；该结果不含阶段内工具往返、system prompt，也不是实际账单降幅。未再次调用外部模型。

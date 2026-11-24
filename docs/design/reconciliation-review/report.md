# Reconciliation 两个 Agent：当前完整 Prompt 与上下文复阅

本报告整理**当前真实运行实现**，不修改规则、工具、上下文或 checkpoint，也不调用新模型。完整中文 System、按需 Guidance、工具说明、真实 User 消息和逐轮日志分开提供。译文忠实保留现状；发现的重复、矛盾与缺口放在 review 部分，不混进译文修正。

建议先看本页的角色与输入输出，再看两份完整中文 System，最后按问题查[代码契约](contracts-review.md)与[逐轮上下文](context-review.md)。这次范围是 phase=reconcile 和 phase=graph_assess；Identity 和两个节点 Agent 仅用于说明交接，不重新梳理它们。

## 1. 两个 Agent 各自做什么

| 项目 | Reconciliation Writer | 独立 Reconciliation Assessment |
| --- | --- | --- |
| phase | reconcile | graph_assess |
| 核心工作 | 协调业务关系、层级、直接物理 owner、Metric 直接依赖、Concept relation_metadata；必要时做保义的耦合节点调整 | 检查实际候选及其影响是否满足关系含义、来源、分支范围和保义义务 |
| 节点内容 | 已接受节点/分支是基线，默认不重写；必要结构调整才改变 | 未变内容是判断前提；不重做 extraction 准入，不偏好唯一拓扑 |
| 结果 | submit_patch，公开参数为通用 MergePatch | Review，公开参数为 verdict / issues |
| 能否直接提交 graph | 不能，只编译候选 | 不能直接修改；accept 仍需 controller 的事务/来源/发布门禁 |
| 是否只读 evidence | 否，六个读取工具都可用 | 否，和 Writer 相同的六个读取工具 |

它们不是节点阶段“单 group 合并”的另一遍执行。关系 Writer 主要解释和修改边，但当前公开 schema 也允许必要节点更新、新节点、冲突/分支处理和新关系契约，因此权限与输出契约明显更宽。

## 2. 实际交给模型的指令分成四层

| 层 | 内容与构造位置 | 是否每个请求都发送 |
| --- | --- | --- |
| System | 角色文件 → incremental_relation_shared → conflict_contract → incremental_protocol；由 llm/task_agent.py 拼接 | 是，当前 Writer 10090 字符、Assessment 10149 字符 |
| User 任务 packet | relation_context 选出的局部图、关系契约、sources 对应 evidence、scope/signals 或 delta/before，再由入口添加义务/反馈 | 每轮重建一份；当前草稿与错误也在这条消息里 |
| Tools | 六个读取函数的英文 descriptions/JSON schema，加当前结果工具 | 正常轮 7 个；fallback/最后一轮集合会改变 |
| 后续工具返回 | 图字段/分页、完整分支、Question/SQL、英文类型 Guidance、source units、草稿投影 | 按需读取后进入保留的 AI/Tool 消息对 |

**User Prompt 没有一个独立的静态 .md 模板。** 它是代码产生的 JSON 任务和后续工具交换。只翻译角色文件会漏掉绝大部分规则；只看 System 也会漏掉 tool description 和 read_guidance 注入的英文规则。

当前两个角色均使用通用 TaskDialogue，未使用节点阶段的 NodeTaskDialogue。System 当前把角色放在共用规则之前，亦未按当前节点类型只注入一个尾部 guidance。

## 3. 完整 System 分别由哪些文件构成

```text
Writer：
  incremental_relation_write.md
  incremental_relation_shared.md
  conflict_contract.md
  incremental_protocol.md

Assessment：
  incremental_relation_assess.md
  incremental_relation_shared.md
  conflict_contract.md
  incremental_protocol.md
```

| 模块 | 实际职责 | 中文译文 |
| --- | --- | --- |
| Writer 角色 | 本轮关系任务、必要读取、操作选择和完整草稿 | writer-role.zh.md |
| Assessment 角色 | 六类评估、读取边界、Review 结论 | assessment-role.zh.md |
| 关系共用模块 | 新建/重构四类节点、各谓词、直接 owner、metadata、层级、冗余和关系待办 | shared.zh.md |
| 冲突模块 | 等价/冲突、完整分支、公共/分支证据、source_routes、操作互斥、分支边和退役 | conflict.zh.md |
| 回复协议 | 读取、唯一结果调用、草稿替换、版本绑定、反馈和无进展 | protocol.zh.md |

完整拼接后的两份中文 System 在 HTML 的“完整中文 System”区展开或下载。每份保留全部共享内容，没有以“同上”替代；五个源模块亦可逐份查看。按需读入的六类 Guidance 与全部工具描述另有完整中文对照。

## 4. 三个入口决定本轮必须解决什么

| 入口 | 固定 before 到底是什么 | 本轮义务 |
| --- | --- | --- |
| 普通 structure 协调 | 当前已经接受的工作区图 | 检查选定区域；signals 只是线索，不能因为出现三角形/相同列就强行改图 |
| 节点通过后的 relation_followups | 开始该关系事务时已接受的图 | feedback 中的 node_relation_followup 是明确保义目标；即使不改图也要独立 Assessment 核实 |
| 节点编译前的 coupled repair | **尚未接受的节点整合中间态** | node_relation_obligations 带结构诊断、原边、原目标含义/映射；先修到结构合法并独立评估，再返回外层节点 Assessment |

因此，before 是 controller 持有的固定快照，并不意味着把整个快照发送给模型。coupled repair 的 before 也不等于外层已经接受的 graph；它是当前节点草稿编译出来、尚待关系修复的中间态。

mandatory obligations 与普通 signals 必须区分。共同 Prompt 已说明 node_relation_followup / node_relation_obligations 必须逐项闭合，不能被当作可选提示。若原入边改指向 conflict owner，controller 提供原目标版本含义，不能要求看不到全量入边的节点 Writer 猜分支，也不能用仅限定起点的 alternative_ids 代替目标版本选择。

## 5. 初始局部图包含什么，哪些仍要读

Writer 先从 task targets 开始，选择必要的语义边、关系分组、端点和物理引用，再补选中物理端点的 grounding owners。它不是把所有邻居无限递归展开，但 metadata 分组和物理 owner 可以扩展区域；初始选取没有“最多 N 个节点/字符”的强硬截断。

Assessment 从实际节点/边变化、被删除边的旧端点和必要消费者开始；补充保留的规范 owner 与 Metric 的实际直接输入。mandatory followup 即使没有 delta，也保留任务 targets 和相邻当前关系。

| 材料 | 初始呈现 |
| --- | --- |
| focus 语义节点 | id/type/name/aliases、必要 description、**全部 properties**；这不是完整 NodeContent，tags 等未全部发送 |
| 邻居 | definition/action/condition/relation_metadata 等选定摘要字段；未展示不等于没有 |
| conflict 分支 | current_conflict_views 中 focus 为完整解析内容，邻居为摘要；说明文案却统称轻量摘要，存在待 review 歧义 |
| 物理端点 | 表/列名、值与直接 Concept owner，不展开完整 catalog metadata |
| 边 | 真正起终点、谓词、完整 properties、evidence IDs |
| before 对比 | Assessment 提供 updated/deleted 节点及边；不是整个 before 图，before conflict 也不是自动全展开 |
| 原始证据 | **初始已注入所选来源的 Question / Gold SQL / evidence 正文**，并非只给 ID |
| 来源单元 | 普通 runner 追加当前任务 manifest；耦合 owner 缺少时可读 read_source_units；coupled repair 入口不会自动获得同样的完整任务 envelope |

完整选取算法和全部字段表见[输入输出契约](contracts-review.md)。真实首轮 User JSON 可在 HTML 下载区查看，保留 SQL/ID/数据原文，不把业务 evidence 翻译后冒充原始输入。

## 6. 输出如何被代码解释

Writer 可使用 node_updates、edge_updates、create_nodes、create_edges、retirements、edge_merges、conflicts、resolutions、relation_contracts，以及通用 schema 继承的 integrate_groups。最后一项不代表普通关系任务获得重做 Identity 的授权；还需真实计划匹配。

更新与完整内容的区别尤其重要：

- node_updates.properties / edge_updates.properties 只列改变键，但每个已提供键的值整体替换；省略键保留 before 值。
- 整个 Patch 替换上一草稿，省略上一草稿的某操作就撤回该操作。
- conflicts 的每个 alternative.content 和 integrate_groups.content 是完整 NodeContent，不是字段增量。
- retirements 声明原断言和实际语义后继；有据错误弃用才允许 after=[]。edge_merges 则声明相容范围内同一事实，不是任意删除捷径。

独立 Assessment 的 accept 必须没有 issues；revise / revisit_identity 每项问题须有 basis；needs_context 指明具体缺失前提。refs 使用真实对象、视图与字段。依赖“缺边/缺规范 owner”的判断可以用精确 AbsenceClaim，由 controller 在选定权威图中核验；它不是命令代码创建一条边。

具体嵌套字段、公开 schema 没写全的隐藏门禁及通用局部编译/最终严格发布的区别，集中列在 contracts-review.md。报告未用“schema 能解析”代替“语义和整个 graph 都正确”。

## 7. 每轮消息和多轮循环：一个解释性示意

以下是代码行为示意，不是新模型调用，也不冒充真实日志：

```text
第1轮
  System = 固定四模块
  User   = 局部当前图、scope/义务、evidence
  Tools  = 6读取 + submit_patch
  模型读取一个 Metric 的 expression

第2轮
  同一 System
  User = 仍是一份任务 packet
  AI(read) + Tool(expression读取结果)
  模型读取两个 Concept 的完整内容

第3轮
  同一 System
  User = 仍是一份任务 packet
  已保留读取对 + 新增读取对
  模型提交一份完整 Patch

编译拒绝时
  User 的当前 draft / issues / 绑定被替换
  相同历史 Patch 不再作为 AI 全文重复发送
  图读取继续保留，旧 inspect_draft 读取在草稿变化时清除
  必要历史诊断进入 repair_facts，但当前通用实现不自动回收过期 facts

编译通过后
  Writer结束
  controller用实际candidate单独构造Assessment任务与新TaskDialogue
  Assessment不直接继承Writer的全部工具对话
```

读取不是按“每个对象一份”存储。当前缓存键是工具名+参数+结果：完全相同交换可覆盖，参数不同但结果相同则仍可重复；相同数据也可能同时出现在当前边、before 对比和 obligations 原边中。不会把每轮整张图叠加 N 次，不等于所有关系和事实都已去重。

## 8. 真实请求量化与阅读重点

本次审查先前 Metric 小样本的 **13 条真实关系请求**，不运行新测试。它们的 System 与当前 active 源文逐字一致。

- Writer：6 次请求，消息数 **2 → 4 → 8 → 12 → 14 → 16**；单次最高 **49886 序列化字符**，不是 token。
- 首轮 Graph Assessment：4 次请求，2 → 4 → 8 → 10 条消息；恢复复评：3 次请求，2 → 6 → 8。
- 一次 concept guidance 返回 **7526 字符**，占该 Writer 最后累计读取正文的约 **57.6%**，是增长的主要单项。
- 恢复复评中，两个不同查询保留了两份完全相同的 **983 字符**结果。
- 6 条响应明确报告 cache_read，合计 58913 tokens；其他响应未报告，不能当作零，也不能据此算完整命中率。

完整逐轮表、缓存/预算分项、真实请求下载和 checkpoint 说明见[上下文报告](context-review.md)。

## 9. 供本轮 review 的问题，尚未实施修改

| Review 点 | 当前证据/可能影响 | 可讨论的方向 |
| --- | --- | --- |
| 主体职责较宽 | 关系 shared 同时含四类型新建准入、层级/冗余推导、owner、conflict、来源和节点重构；System 已约1万字符，仍可再读整类资产规则 | 先确认关系 Agent 的必要职责，再决定共享、角色、输出、按需说明的分工，不能先机械删句 |
| 输出硬契约分散 | schema 的12顶层字段、代码隐藏校验、System操作说明分布不同；某些公开字段在关系阶段实际不可用 | 集中解释允许字段与隐藏门禁，审查是否需要关系专用窄 schema |
| Guidance 与初始规则重叠 | conflicts 与 System 同源；concept guidance 大于初始局部图的若干主要部分合计 | 去重或按具体缺少前提读取小节；避免已接受节点被 extraction 准入指南重新审计 |
| 上下文尚非新节点布局 | 当前草稿/错误放在第二条 User；读取以 AI/Tool 对累积，repair_facts不自动回收 | 讨论稳定输入与动态状态分离，以及按事实/对象/版本去重；必须保留错误和来源绑定 |
| schema恢复与节点阶段不同 | Reconciliation普通 schema 错误仍切fallback，而且fallback保留读取工具 | 是否统一结果修复模式，避免无效重读；当前仅记录差异未修改 |
| 读取权限与描述不同 | description说按展示evidence IDs读取，代码未加节点阶段同样的局部白名单；Assessment也能读所有六工具 | 明确真正需要的权限和授权范围，不把“局部初始输入”误当“局部硬读权限” |
| 初始归属与完整性容易误读 | focus全部properties不等于完整NodeContent；normal节点缺初始evidence映射，common_sources收集亦不完整 | 明确呈现层完整性和来源归属，减少为核实已接受含义而补读或误拒 |
| 预算不是自动上下文管理 | resilient调用方max_calls=None、model_call_budget=None；读取轮不受失败计数限制，最终可能到输入门禁才停 | 讨论可控的读取/字数预算和压缩/驱逐，保留已完成任务与准确诊断，不能只加大上限 |

这些是本次复阅的讨论点，不等于每一项都已经造成失败，也不代表已决定修改。完整中文译文保留原文现状；涉及规则与实际 schema 的差异请结合原始代码契约表判断。

## 10. 主要源码定位

| 位置 | 作用 |
| --- | --- |
| [task_agent.py](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/llm/task_agent.py:58) | System组装、工具集合、模型轮次、schema/fallback与恢复 |
| [relation_context.py](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_context.py:65) | 初始局部图、focus/邻居、evidence、before/delta与义务选取 |
| [packets.py](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/llm/packets.py:285) | TaskDialogue渲染、读缓存、唯一草稿、repair facts、snapshot/restore |
| [task_tools.py](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/llm/task_tools.py:15) | 六个公开读取工具及实际说明 |
| [task_runner.py](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/task_runner.py:74) | 普通事务、独立评估、needs_context补读、来源/候选/checkpoint门禁 |
| [node_relation_repair.py](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/node_relation_repair.py:30) | 节点中间态的coupled Writer/Assessment与保存修复恢复 |
| [relation_followups.py](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/relation_followups.py:34) | 节点通过后的持久关系义务与防重放 |

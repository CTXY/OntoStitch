# Reconciliation 两个 Agent 的实际上下文与恢复行为

本文件为只读源码与既有真实请求审查。没有修改运行代码、没有调用 API、没有启动测试或后台任务。统计来自 `carcinogenic_molecule_count` 的 live 与 retry_live `model_calls.jsonl`，不把重建的示例冒充真实请求。机器可读结果位于 [context_metrics.json](/Users/yichendezaizai/autoonto-nl2sql-release/output/reconciliation_review_20261120/context_metrics.json)。

## 1. 当前实际加载的 System 与历史请求是否一致

`task_agent.py::invoke` 对 `reconcile` 与 `graph_assess` 使用通用 `TaskDialogue`，没有使用新节点阶段的 `NodeTaskDialogue`。System 拼接顺序为：

```text
incremental_relation_write.md 或 incremental_relation_assess.md
incremental_relation_shared.md
conflict_contract.md
incremental_protocol.md
```

角色模块在前，共用规则随后；没有按当前节点类型拆分。这与节点阶段“共用前缀 → 角色 → 输出契约 → 唯一类型 guidance”的布局不同。

| 实际角色 | 当前 System 字符数 | 当前 SHA256 | 本次分析的历史请求 |
| --- | ---: | --- | --- |
| Reconciliation Writer | 10090 | `c55c8b260c011682e763fcc1bfa9226556f392f8f259564fa5372f951ba2ad6d` | 所有对应请求逐字相同 |
| Graph Assessment | 10149 | `04699f3d77344ddbc9588a1c462810bd97c2ca5d9790dbc20d9216be149c8e44` | 所有对应请求逐字相同 |

这里的相同只证明这两类 System 文本与当前源码一致，不证明整个 checkpoint 的全局 policy 未变化。retry_live 的 migration.json 明确记录：新旧 prompts/tools 合约哈希不同；保存的 relation Patch 保留，旧 relation Assessment 移入 historical_review 后要求独立重评。节点 Prompt 或其他适配代码变动也能改变全局合约。

## 2. 每轮实际 messages 的顺序

### 普通轮与读取轮

```text
System：上面四个模块合成的一份完整 System
Human：一份任务 packet = 局部图 + 初始 evidence 正文 + 当前草稿 + 绑定错误 + 必要历史事实
AI：一次已执行读取调用，content 清空，只保留该调用
Tool：对应读取结果
AI / Tool：后续仍保留的读取交换……
```

- `TaskDialogue.messages()` 每轮从当前状态重新组装，不是把上轮请求全文再 append 一次。
- 局部图在本轮 `messages[1]` 中一份。提交过的完整 submit_patch 不再作为 AI 消息重放；唯一当前草稿写进该 Human packet。编译反馈也更新到这份 packet。
- 多个读取调用同轮出现时，保存为独立 AI/Tool 对。AI 的长自然语言分析不发送回模型；原始返回保留在 trace。
- Writer 的 packet 初始来自 `relation_context`：focus 的 properties 完整（不等于完整 NodeContent；tags 等可能未发送）、必要关系与物理事实、邻居摘要、当前冲突摘要、关系契约、signals/feedback/约束及被选来源的 Question/Gold SQL/evidence 正文。这里不是只有 evidence ID。
- Graph Assessment 的 packet 重新从实际 candidate 构造，含实际修改、必要 before 节点/边、来源、关系与必要消费者。它不会自动继承 Writer 的整个工具对话。
- mandatory node followup 的 Assessment 即使无 delta，也保留任务 targets 及相邻当前关系；普通 no-op 不等于已经解决移交义务。

### 拒绝后修订

`set_draft` 完整替换当前草稿和当前草稿反馈；被拒草稿不成为已接受基线。已有图读取继续保留，只有 `inspect_draft` 投影在草稿变化后清除。错误中的精确 before/after/counterexample/required_context 可被提取为 repair_facts，通过引用避免同一个错误段内重复；这些事实带原草稿或历史 candidate 绑定。

需要注意：通用 TaskDialogue **没有节点版的过期 repair_facts 回收**。同 episode 中历史诊断事实可继续累积，即使当前草稿已经改变；它们被标为历史事实，但仍占上下文。普通最新错误可以替换，不等于所有历史事实都被清理。

### schema / JSON 恢复轮

| 情形 | 当前 Reconciliation 路径 | 当前节点路径 |
| --- | --- | --- |
| 原生工具 JSON 可解析，但输出 schema 错误 | `stage=output_schema` 会启用 fallback | 保留原生工具格式修订，不因普通 schema 错误切换 |
| 工具 JSON 损坏 | 启用 fallback | 启用无工具结果恢复 |
| fallback 提供工具 | 仍提供六个读取工具；最终结果工具移除，最后一轮/submit_only 时才变成无工具 | 不提供读取或结果工具 |
| 恢复指令位置 | 所有读取后，再追加一个 Human：Return final result as plain JSON matching this schema + 完整 schema | 唯一当前 tail 内明确 plain_json、tools_available=false、read_allowed=false 及 schema |

因此，不能把节点阶段刚完成的“schema 错误不切换、无工具 fallback 禁止读取”描述成 Reconciliation 也已采用。fallback 状态下 relation Agent 仍可发起读取，读取交换还会保留。恢复 schema 只在本轮末尾一份，下一轮重新渲染，不是每次失败再追加一份历史 schema。

## 3. 去重的实际粒度与仍存在的重复

读取缓存键是 `tool_name + JSON([args, result])`，不是按对象 ID、字段或结果正文去重。

- 同名、相同 args、相同 result 会覆盖旧条目；新 sequence 使其移到读取末尾。不是每次完全相同读取都增加两条消息，但重排可能破坏此前可复用的读取前缀。
- 相同对象、不同字段投影会保留多份；不同查询 args 返回完全相同结果，也会保留多份。
- args 的 JSON 不做 sort_keys；仅对象键顺序变化也可能被当成不同缓存项。
- graph read 自动附带新来源的 evidence；EvidencePresentation.seen 会减少这类跨读重复正文。直接 `read_evidence(refresh=True)` 会刷新正文，TaskDialogue 未按 evidence ID 建立节点版的统一正文 ledger。
- 全部读取不会自动与初始 focus 的已知字段合并去重。模型可以重新读取输入中已经完整提供的字段；但邻居摘要未含的属性确实需要按需补读，不能把这类必要读取一概视为冗余。

**真实重复例子**：retry_live Graph Assessment 第 2 次请求后，模型先前按 Metric ID 查询 relations 得到一份结果，又按两个 Concept ID 查询 relations 得到完全相同的 983 字符 Tool 正文。第 3 次请求中这两份正文同时保留，冗余 983 字符；两个 args 不同，所以当前缓存不合并。

**packet 内关系重复**：live Writer 的 obligations.original_edges 中一条原边与 edges 中的同 ID/谓词/端点/属性记录相同。live 与 retry_live Graph Assessment 中，该保留边同时出现在 edges、before_edges、obligations.original_edges 三处。它们服务当前/历史/义务绑定，不能仅凭发现重复就直接删知识；可在未来用稳定记录引用表达。context_metrics.json 的 duplicate_semantic_records_inside_task 给出精确路径，比较时忽略 provenance 表示差异，不声称不同视图就是同一逻辑角色。

## 4. 真实请求的轮次、规模与工具

字符指标：messages 正文为所有 message.content 的字符数之和；wire 与工具 schema 数量直接采用 trace 中 request_budget 的保存值，包含序列化结构，**不等同 token 数**。同 episode 的任务 Human packet 在以下请求中逐字不变；差别来自读取追加和最后一轮工具集合变化。

### node_consolidation_smoke_20261120_live / carcinogenic_molecule_count

**integration_reconcile**；episode `1608f80d119c471792a2139cfbd84bb9`；6 次真实请求。

| 轮次 | messages | 正文字符 | wire 字符 | 工具 schema 字符 | 累计 Tool 正文 | 与上轮相同的消息前缀数 | 明确报告 cache_read token |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 16319 | 34462 | 17351 | 0 | 0 | 未报告 |
| 2 | 4 | 18231 | 37096 | 17351 | 1912 | 2 | 10752 |
| 3 | 8 | 20160 | 39958 | 17351 | 3841 | 4 | 未报告 |
| 4 | 12 | 21231 | 41942 | 17351 | 4912 | 8 | 11169 |
| 5 | 14 | 28757 | 49886 | 17351 | 12438 | 12 | 12544 |
| 6 | 16 | 29393 | 47010 | 13411 | 13074 | 14 | 未报告 |

**integration_graph_assess**；episode `b6cec893e9fc435f82e14cd4354bd127`；4 次真实请求。

| 轮次 | messages | 正文字符 | wire 字符 | 工具 schema 字符 | 累计 Tool 正文 | 与上轮相同的消息前缀数 | 明确报告 cache_read token |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 16858 | 23861 | 6162 | 0 | 0 | 未报告 |
| 2 | 4 | 17670 | 25200 | 6162 | 812 | 2 | 8064 |
| 3 | 8 | 18906 | 27453 | 6162 | 2048 | 4 | 8320 |
| 4 | 10 | 19889 | 28916 | 6162 | 3031 | 8 | 未报告 |

### node_consolidation_smoke_20261120_retry_live / carcinogenic_molecule_count

**integration_graph_assess**；episode `83dc62f40ae2471e8644b3ca0d160087`；3 次真实请求。

| 轮次 | messages | 正文字符 | wire 字符 | 工具 schema 字符 | 累计 Tool 正文 | 与上轮相同的消息前缀数 | 明确报告 cache_read token |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 16958 | 23969 | 6162 | 0 | 0 | 未报告 |
| 2 | 6 | 19537 | 27726 | 6162 | 2579 | 2 | 8064 |
| 3 | 8 | 20520 | 29199 | 6162 | 3562 | 6 | 未报告 |

live Writer 第 6 次只提供 submit_patch，前五次提供六个读取工具加 submit_patch。该 episode 之前都是读取，没有协议错误；这是本轮 smoke 的 episode 调用预算进入最后一轮，不能误解为任意生产任务都最多六轮。Graph Assessment 对应六个读取工具加 Review。Writer 最后一轮 wire 从 49,886 降到 47,010 字符，是工具 schema 从 17,351 降到 13,411，消息仍从 14 增至 16；不是已有上下文被压缩。

live Writer 的 read_guidance(concept) 结果正文为 7,526 字符，占其最终累计读取正文 13,074 字符的约 57.6%；一次 guidance 阅读是这条小图任务增长最大的单个部分。Writer 起始 Human 6,229 字符，其中 obligations 2,037、nodes 1,615、edges 762、evidence 728；Graph Assessment Human 为 6,709 字符，retry 为 6,809。

读取本身也产生可避免的诊断负担：live Writer 第一轮把 Concept 与 Metric 混在同一个 fields 列表读取，得到 4 项不适用字段 read_errors；live Assessment 后续对两个 Concept 请求 expression，得到 2 项；retry Assessment 混合读取得到 3 项。结果明确反馈 available_fields，没有静默伪造值；这属于读取选择器使用不精确，不是编译器拒绝候选。

原始日志共 13 次 relation 请求，有 6 次明确提供 usage_metadata.input_token_details.cache_read，合计 58,913 tokens。其余请求未报告该字段，不能把缺失当 0，因此不据此计算完整命中率。

retry_live 中没有新的 Reconciliation Writer 请求；保存的 relation Patch 被保留并确定性重编译。Graph Assessment 出现三次新请求，是 migration 有意归档旧 assessment 并要求新合约独立复评。另有 accepted_repair_regression.json 记录同策略恢复已接受修复时 new_model_calls=0；两者场景不同。

## 5. “有界”实际指什么

| 层次 | 类默认值或机制 | 限制的实际对象 |
| --- | --- | --- |
| IncrementalIntegrationAgent | max_calls=12；request_max_characters=210000 | 一次 invoke 的模型轮次；单次 messages+工具序列化字符预算 |
| IncrementalGraphIntegrationEngine | model_call_budget=80；max_rounds=3 | 一次 merge 的共享实际模型调用次数；外层内容/关系修订循环 |
| review_batches | 根节点估算 budget=14000 | 按节点自身 JSON 估算分组；不包括 System、邻居、evidence、tools、义务上下文 |
| RelationReads | 单页默认 30、最多 100 条 | 对象数量，非单页字符数；允许继续分页 |
| RecoveryTracker | 三个独立协议失败停止；同输出/拒绝/事实重复两次停止 | 结果/协议失败；不是每一次读取 |
| Transport | 同一逻辑请求连续可重试连接故障三次停止 | 网络重试；不当作新的模型决策或新事实 |

**实际 resilient 训练调用方不同于类默认值。** `scripts/run_resilient_training.py` 约第 497 行创建 agent 时传 `max_calls=None`，约第 502 行创建 engine 时传 `max_rounds=5, model_call_budget=None`；单次输入字符限制从 config 读取。因此此调用方没有每 episode 或整个 merge 的模型调用数上限。不能用“默认 12/80”证明这些后台运行有轮次硬上限。

纯读取轮直接 exchange、persist 后 continue，不进入 tracker.observe；换不同读法或无结果的新查询可以持续占用调用并扩展读取缓存。外层 max_rounds=5 也要等当前 invoke 结束才推进，不能限制内部纯读取循环。最终仍有输入预算检查、协议/重复失败检测、网络止损，但没有自动按读取价值驱逐旧内容、超限前自动压缩或按需分页预算合并。超过字符预算会在 transport 前抛 RequestInputTooLarge，保持 checkpoint，不会自动省略知识继续。代码没有无条件把整张 graph 放进输入，但 roots、邻接关系、共同物理 owner 和 metadata 的展开范围可能很大，不能仅凭“局部”一词保证体积小。

## 6. 无进展、错误绑定与恢复

### 什么算“新事实”

`premise_fingerprint` 收集 view+对象 ID+字段事实、来源单元、evidence、契约、明确 required_context 和历史 basis；对事实原子做集合化，重复读同一已知事实不应改变版本。工具调用 ID、错误叙述、草稿台账不作为新事实。System、结果 schema、读取工具的描述/schema 参与 Agent policy；外层另绑定全局合约。ID-only 或错误读取不产生新知识。

输入事实 hash 在模型调用前采集；新错误提供的 counterexample 只能影响下一轮，网络重试不产生新决策。结果失败按输出、拒绝、事实版本计数；两个独立模型轮次在相同事实下重复同一被拒结果才停。协议失败按整个回复计一次，混合读取/结果或多个结果调用整轮不执行。

### 错误不被“恢复”冒充当前事实

当前 draft_id、issues_draft_id、candidate_draft_id 明确区分。旧 candidate 反例通过 snapshot_role/snapshot_binding 标为历史，而不是把旧 candidate 重新标成当前 graph。inspect_draft 的全量相同正文转到唯一 draft 槽，只保留新诊断；过期错误留作历史，不覆盖当前已绑定错误。快照含草稿、精确 reads、repair_facts 与错误绑定，restore 对整个 detached 对象先验证后应用，避免半恢复。

恢复 key 绑定 task、phase、before hash、candidate hash 和 policy；外层还校验 envelope/hash 与 checkpoint 合约。候选变化不会直接继承旧候选的 Agent 工具对话。相同候选的继续恢复可保留已读事实与错误，保留无进展计数，不靠重启清零。

### Assessment 的跨轮与跨候选

- 每个新 invoke 有新的 TaskDialogue 和 episode_id。Writer 的工具读不会自动传给 Assessment。
- 普通 task_runner 保存候选后的独立 Review；新候选重新构造实际图 packet，携带必要 semantic_issues/basis/required_context。不是转发完整旧会话。
- 相同来源、base、candidate、policy 和实际事实下，已拒绝 Assessment 可从 assessment_gate 复用；相同拒绝重复到阈值会停止，避免重复付费评估不变候选。
- needs_context 指明对象后，relation 路径可由 controller 精确读取、持久化 required_context 并重新评估；这与节点阶段禁止隐式扩大图权限不同。
- ready_to_commit 的已接受候选经过确定性重编译且 hash 一致后，可以复用保存的 Review，不重跑模型。
- 嵌套 node_relation_repair 有独立 relation Patch、candidate hash、Review 与 recovery namespace；保存的 relation Patch 先重编译校验，不重跑 Writer；已接受关系 Review 可复用，策略迁移时应显式归档并独立重评。

## 7. 缓存能复用到哪里

当前 relation 布局在同 episode 的读取阶段有稳定的 System、工具集合和任务图前缀。真实调用中前缀依次扩展，日志 usage 也实际报告了部分 cache_read；可以说明存在缓存利用，不能承诺所有请求都命中。

修订时，draft 与 issues 处于第一个 Human 的 graph packet 中，位于已有 AI/Tool 读取之前。即使图本身未变，草稿位置一变化，后续工具读的字节前缀就不再保持。新节点阶段把当前状态放到 ledger 后面，此改善尚未移植到 relation TaskDialogue。

同类不同任务可复用固定 tools/System 到第一个不同任务数据处；第一条 Human 早期包含 task_id/revision 等变动字段，限制跨任务局部数据的可复用前缀。Writer 与 Graph Assessment 的第一个角色段已经不同，工具结果模型也不同，不能认为共享 md 文件等于两角色共享了大段实际请求前缀。最后一轮移除读取工具、或进入 fallback 改变工具集合，也会降低原工具前缀的复用。

本报告仅定位现状与可 review 的改动点，不已经实施缓存重排、按事实合并读取、关系正文去重或读取轮数限制。

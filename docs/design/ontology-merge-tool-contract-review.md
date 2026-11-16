# Ontology Merge：简化工具与修改契约

日期：2026-11-16。状态：已接入运行时代码与 prompt，进行可控回归验证；本轮未调用外部模型或启动完整 merge。语义职责见[节点整合与关系协调设计](ontology-merge-content-and-relations-review.md)。

## 1 保留哪些工具

| 项目 | 当前选择 | 原因 |
| --- | --- | --- |
| inspect_graph | 保留一个公开图读取入口 | 按对象、关系、消费者、契约定向查看；关系阶段的 inspect_items 仅留旧记录兼容，不展示给模型 |
| read_evidence | 保留 | 批量读取缺失的完整证据，避免重发已提供正文 |
| read_guidance | 保留、按需调用 | 读取缺少的类型要求；已提供内容返回引用，不重复整个 registry |
| extend_task | 删除公开工具 | 控制器从实际 Patch 检查局部关联与缺失上下文，不再先请求扩大范围再提交 |
| Patch | Writer 的最终结构化输出 | SDK 中仍可用 submit_changes(patch) 承载；不是先提交字段、再调用一次提交的双重流程 |
| Review | Assessment 的最终结构化输出 | verdict、issues、issue_refs；不是图修改工具，不再增加确认调用 |
| conflicts、retirements、edge_merges | Patch 字段 | 描述完整分支、替代去向或同义边合并，不是分别执行的工具 |
| 部分更新、来源维护、事务、提交 | 控制器行为 | Agent 不逐个管理内部来源账本或保存状态 |

Node 阶段暂留现有 BatchResult 输出和 inspect_graph 读取模式；仅在存在待修复的暂存操作时提供 inspect_pending。没有必要为了统一名字重写底层存储。两个阶段均没有额外 finish/confirm，也不要求 Writer 看完成功回执再返回一次结果。

## 2 呈现的图

关系阶段首屏顺序：任务与范围 → 轻量节点 → 当前边 → 关系组/分支/契约 → **集中 evidence 正文**。

节点展示真实 id、node_type、name、aliases、必要 description 和 properties.definition/action/condition。边展示 id、edge_type、端点、properties.description、必要 properties.alternative_ids 和 evidence_ids。保留实际字段名，不把 definition 改叫 meaning。

完整 targets、公式、Rule reason、内部 sources 和物理 metadata 不默认展示。分支展示实际名称/定义、必要条件/动作及该分支的生效边，完整内容按需读取；公共 owner 的空字段不能被误解为分支缺少含义。

物理上下文只提供必要 ID、类型、名称；不经 table hub 扩展其他语义节点。节点未展示的字段不等于不存在。

### Evidence

- 从相关节点、边和分支收集来源；只在边上的 evidence 也必须提供。
- 同一 ID 的正文只提供一次；同 ID 不同正文明确报错。
- 不同 ID 的**完整正文完全相同**时，一份条目列 evidence_ids；后来出现的同正文新 ID 用 same_content_as 指向已提供正文。
- 去重不改变每条边/分支的精确来源，不把区域证据广播给所有对象。
- 同一 Agent 对话只追加新正文，新对话重新提供必需内容。不得从另一 Agent 的已读状态推断当前已读。
- 正文超过输入预算不能截断成伪完整证据；需要缩小任务或按需读取。预算超限是明确失败，不是知识为空。

## 3 读取契约

关系阶段公开接口：

```text
inspect_graph(mode: items | relations | consumers | contracts,
              ids, view: candidate | input = candidate,
              fields?, direction?, edge_types?, offset=0, limit=30)
read_evidence(ids)
read_guidance(topic)
```

- items 读取节点/边；fields 使用真实路径。`content` 是完整业务内容的读取选择，`alternatives` 返回完整解析分支；二者不是可写属性。
- relations 只读取指定对象的邻接关系和轻量端点，不递归展开；使用方向/谓词筛选和分页。
- consumers 查询声明式引用，不等于任意可达节点。
- contracts 使用谓词 ID。读取不会注册新谓词。
- 返回 view、revision、items、complete/next_offset；错误指明对象和字段，缺少 ID 不影响同批其他有效读取。
- 同参数重复读取返回 earlier-result 引用，其他必要读取继续可用；不再因一次重复读取封锁整个读工具集。

Node 阶段沿用 nodes/relations/consumers/candidates/contracts 模式，读取目标内容与完整输入成员；不将关系阶段的轻量视图误当完整节点整合输入。旧 ID 与当前 ID 通过已有 identity_aliases/来源对应区分；读取不会静默将 archived 内容冒充当前内容。

## 4 无 extend_task 的局部处理

控制器从 Patch 推导实际触及对象，复用现有局部任务，不增加新的计划 Agent。

1. 原任务范围内的对象按原契约处理。
2. 范围外对象必须与任务直接耦合：语义根节点及一跳语义邻居、显式引用其内容的消费者、拥有相关 relation_metadata 的节点，以及此次必要相邻边。物理目录节点不可借此修改，也不作为扩张 hub。
3. 仅看过/读取过一个无关对象，不产生写权限；无关组件的修改直接拒绝。
4. 必要耦合对象或新关系端点尚未提供时，返回 `required_context`：当前轻量对象、拟修改属性的当前值和缺失证据。**不应用任何修改**。Writer 使用该反馈重提完整一致 Patch，无需再调用范围扩展。
5. 已有必要上下文且提案机械合法时，将明确触及的对象纳入本次范围并记录 scope event；不把所有邻接对象开放为改写任务。
6. 所需补充上下文超预算时停止该提案并报告范围缺口，不静默截断依赖。没有固定“三次扩展”额度。

这里的范围判断是代码上的局部联系检查，不是自动证明修改必要或正确。最终语义仍由相应 Assessment 判断。读取对象的摘要也不代表已读取其所有属性；替换结构属性前必须掌握该属性的完整旧值。

## 5 Patch 与修改语义

| 字段 | 表达什么 | 处理方式 |
| --- | --- | --- |
| node_updates | id、改变字段、explanation | 未改内容保持；关系阶段须有命名/结构调整理由 |
| edge_updates | id、改变的端点/谓词/properties | 未改来源、字段和范围保持 |
| create_nodes / create_edges | 新 ID、完整必需字段、evidence_ids | 校验类型、引用及契约，不创建物理目录对象 |
| conflicts | owner、完整 alternatives、conflict_points | 每个候选有 alternative_id、完整 content、evidence_ids；代码无损提取公共内容 |
| edge_merges | source_ids、survivor_id、explanation | Agent 判定同事实同范围，代码维护引用和精确来源 |
| retirements | before、after、explanation | 同时删除被替代对象和记录保义后继；输入历史不删除 |
| relation_contracts | 新谓词完整定义 | 复用已有谓词不重复声明；不能覆盖已安装契约 |
| explanation / failure | 本次局部结论或无法完成的缺口 | failure 结束而非当作格式错误反复重试 |

### 部分更新

未给出的顶层字段、properties 键保持原值。给出 targets/expression/relation_metadata 等结构属性时，替换这个属性的**完整值**，代码不猜深层合并。properties 某键设 null 表示明确移除；重建后仍须符合 schema。顶层空值按具体字段 schema，不能把所有 null 解释为删除。

普通更新自动保留精确来源。新增支持用 add_evidence；新对象/分支用 evidence_ids。Agent 不重建 SourceReference 或内部 overrides。多个后继不自动继承所有来源。

退役引用必须是对象而非裸 ID：

```json
{"before":[{"kind":"edge","id":"E_old"}],
 "after":[{"kind":"edge","id":"E_new"}],
 "explanation":"原关系由新关系保义表达。"}
```

同 ID 更新不需要 retirement。节点退役必须有知识去向；明确错误的旧边可说明后删除而无后继。Node 的原接口仍需 edits 执行删除、changes 记录对应，二者不是两个模型调用；关系 Patch 的 retirements 已将这两项声明合在一起。

## 6 校验、评估与失败

结构化输出先执行类型/字段、引用、范围、来源和完整图机械校验。整个提案原子执行；任一错误都恢复之前有效候选、暂存操作与写范围。反馈返回具体错误；范围缺口直接附上下文。

`validated` 只表示可进入语义评估，不能写成 accepted。Writer 正常输出即结束，回执供控制器和日志使用。

- Node Assessment：比较整合结果是否完整表达输入知识及差异，不重新认证原事实/理由。新运行输入不再要求逐 Rule rationale/decision 清单；旧 checkpoint 字段保留读兼容但不暴露在输出 schema。
- Graph Assessment：只评估实际关系修改及必要影响；默认节点内容是前提。必要重构检查保义与新组织，不重审未变节点。
- 无实际变更且无尚未通过的累计修改：跳过 Graph Assessment。
- 已有未接受修改却无进展：不能用空 Patch 伪装通过；保留现场和具体未解决问题。
- 旧源记录与旧反馈不是当前候选，不能反复拒绝已经移除的旧内容。

## 7 管理员例子

当前节点含义已确定：School、School administrator（姓名是其表示）、Administrator email。

```text
E1: School --has_administrator--> Administrator   evidence_ids=[Q_A,Q_B]
E2: School --has_email_address--> Email           evidence_ids=[Q_A,Q_B]
```

Q_A/Q_B 的正文在末尾各提供一次。根据主体含义，Writer 只需输出：

```json
{"patch":{
  "edge_updates":[{"id":"E2","from_node_id":"C_admin",
    "properties":{"description":"管理员用于联系的邮箱。"}}],
  "explanation":"邮箱属于管理员，学校与管理员关系保持。"
}}
```

无需读完整 targets；未改来源保持；无额外 extend/submit/finish。若 School 的 relation_metadata 也需要调整但未展示，控制器在任何改动之前返回该组当前内容，下一次 Patch 同时调整边与组。Assessment 只检查新归属、组说明及证据相容性，不因为 Q_A 没有投影姓名而取消它的来源支持。

## 8 Review 入口与验证范围

代码职责：`proposal_scope.py` 管理局部耦合与上下文反馈；`evidence_refs.py` 管理展示去重和精确归属；`relation_reads.py` 管理字段读取；`batch.submit_atomic` 统一原子校验；`reconciliation.py` 管理读工具和唯一最终结果。

正式 prompt 是 `src/cortex/capabilities/graph_merge/llm/prompts/` 中 incremental_consolidate、incremental_content_shared、incremental_node_assess、incremental_relation_shared、incremental_relation_write、incremental_relation_assess。文档不再维护另一份完整 prompt 副本。

本轮使用可控工具对话与回归测试检查范围、回滚、字段、来源、分支和重复读取。实际模型的图质量、调用次数与 token 成本尚需固定输入对照；不能将模拟请求数当真实模型效果或成本结果。

可控验证结果见[验证记录](ontology-merge-tool-contract-validation.md)：325 项合并相关回归通过，其中 14 项针对新接口。

## 2026-11-16 真实评测修订

审查和首轮真实交互进一步修正了 malformed tool-call 恢复、空过滤、未知 ID/metadata null、精确参数示例与字段化错误反馈。Node 关系/层级事实仍须保留，不因后续另有 Reconciliation 而忽略；Concept 匹配不能由属性边隐式替代。当前匹配组只在存在唯一当前成员时展示 current_owner，不推断新的身份关系。

真实评测记录见 [本轮审查与实验记录](../../output/merge_contract_v87/REVIEW.md)；未完成或未独立通过的实验不会因模型返回 accept 就被视为通过。

# v59：关系组说明与业务属性关系的实现

日期：2026-11-05。实现范围为 extraction training 及其必要的保存、交接和读取边界。v58 是历史方案，本页记录实际实现。

## 1. 本轮行为

保留 Concept、Metric、GenerationRule、Event 的现有框架和最小化要求；不要求子类必须另有特殊动作或额外属性。NLQ、SQL 和数据库支持类别时，允许 is_a；共享父类本身既不证明互斥，也不证明实际存在共同成员。Value ID 继续使用可逆、可读编码，不恢复默认 SHA-256。

属性优先表达具体业务联系。独立的新业务谓词仍可直接定义；属于通用属性家族的具体谓词可选 `extends: "has_attribute"`。不自动加第二条 has_attribute 边，不把业务关系改成存储记录关系。边的 description 简洁说明真实业务联系及必要限制，不堆积字段说明、profiling 数字或探索过程。

## 2. 可选关系组说明

字段为 `Concept.properties.relation_metadata`，省略表示没有声明组级知识。每项只有：

```json
{
  "id": "independent_categories",
  "edges": ["edge.category_a_is_object", "edge.category_b_is_object"],
  "description": "These categories describe independent dimensions; membership in one does not imply or exclude the other."
}
```

例子仅说明格式，不预设任何实际学校类别关系。id 在所属 Concept 内唯一；edges 至少两个不同的现存边 ID；所有边相邻于 owner，具体谓词和相对方向相同。该说明仅适用于列出的边，不自动覆盖未来相邻边。含分支限定的边还必须能处于同一有效分支。

只记录单边无法表达的组级信息，例如共同分类的互斥、允许共属、观察到交集、覆盖范围。不要把“未声明互斥”“允许共属”“数据库观察到交集”“穷尽所有类别”混为一谈。声明强度服从证据；没有依据就省略。自由文本不是可执行公理，不生成两两边或推理闭包。不增加 Constraint、qualifier 或固定互斥布尔字段。

新建成员边和说明可用同一 edit_graph 原子操作。修改或删除引用边时需同步调整说明；悬空引用拒绝整个编辑，不静默丢知识。

## 3. 关系家族的最小边界

EdgeType 可选 extends 当前仅允许 has_attribute，深度一层。父契约一直来自 root；子契约端点只能兼容或收窄，方向不变，不能改写父字段要求。原始契约原样保存；校验时无副作用读取有效字段。没有一般继承系统、递归注册、专用索引或自动 SQL 展开。

## 4. 必要的保存及视图适配

- Concept 构建保存显式保留 relation_metadata；空字段省略，旧格式兼容。
- 跨 case 交接时，用实际 authored edge → incoming edge 映射替换组内边 ID；节点映射和边映射分开。
- 自动合并重复边时，同步替换普通属性及冲突分支 `properties.relation_metadata` 中的引用；成员塌缩等不合法情况拒绝，不猜测重写说明。
- 普通检索投影只保留全部成员边都存在的完整说明项；不截短成员，不改源图，不扩检索范围。
- 冲突分支的特有说明可能是分支差异本身：若部分投影会丢失其成员边，明确拒绝该临时投影；完整视图保留原分支。当前不实现分支折叠、强制闭包或自动补回不可信 Value。这是受控边界，不能宣称支持任意不完整冲突子图。
- 现有 Properties 展示即可阅读组说明；具体业务边沿用其真实名称，关系契约保留 extends。没有新增展示子系统。

## 5. Learning / Assessment 上下文检查

Learning 的初始 prompt 给任务和规则导航，类型 guide 按需载入。Concept guide 提供可写字段 schema；关系 guide 提供组说明、属性家族及边操作要求。inspect_graph 的 node 模式已能读取双向边和完整属性；入向谓词可用不带 node_id 的 edge_type 查询完整契约，无需新增工具。operation registry 仅随 GenerationRule guide 返回；修复了 fallback 函数在已有 registry 时仍被提前调用的问题，不重复注入全局清单。

Assessment 使用原来的四层组织：单元内容、跨单元一致性、SQL 应用、条件性 target/regression 检查。原 related_knowledge 已覆盖受影响语义组件；改变成员边时仍能看到未改 owner 和组说明。不添加 relation_metadata_consistency 或额外评估单元。组声明是否有依据归 support；是否矛盾归 graph_consistency/scope。

已有 Concept 相对 accepted_before 的真实差异仅为 relation_metadata 和系统来源时，沿用 semantic 应用要求；未变绑定不要求重新贡献 SQL。新 Concept 或混合绑定、名称、定义等变化仍按 sql 评估。此判断由代码比较快照，不接受 Agent 自报标志。全图稳定 SQL 和独立语义验收不变，无 review_only 状态。

本轮记录的字符量：Learning direct 4583、repair 4546；各语义类型完整 guide 约 7–8k，关系操作 guide 约 16k（含可写 JSON schema）；Assessment 指引约 24k，registry 仅在相关 Rule 评估时提供一份。字符数不是 token 数；不为缩短而删除验证必需 schema。重复边不同时出现在 unit 和 related_knowledge 中。

## 6. 验证边界

回归覆盖原子写入、悬空边拒绝、选择接受边界、导出 ID 重映射、普通投影不改源图、冲突分支重映射和部分视图拒绝、属性父契约、metadata-only 应用要求及成员边修改时的 Assessment 上下文。

初轮全量 913 passed；审查边界修复后最终全量 916 passed（102.30 秒），新增专项 16 passed，Ruff 通过。case60 正式重跑结果见本次运行产物。代码不按 case ID、学校类别或具体值做确定性分支。可选字段不设抽取配额，模型未输出并不自动判错。

## 7. Case60 execution evidence

Formal sql-extract completed with stable_correct: two correct replays, all 19 Assessment units valid, accepted_revision = final_revision = 809a12db338b4f55b81d7c4c015b14cb. It generated five Concepts and thirteen explicit semantic/binding edges, without a Metric, Event or GenerationRule quota. The business view contains fifteen nodes and twenty-two edges including physical ownership; the full graph contains one hundred nodes and one hundred seven edges.

School has separate identifier/name matching groups. The two categories use is_a; the place uses located_in. School carries one relation_metadata item referencing both category edges. Value IDs include readable .P, .1 and .San%20Joaquin.

The actual Learning transcript contains one full operation_registry tool response; model input ranged from 4730 to 28487 tokens. The run retained generic has_attribute for website and did not generate an extends child contract; that optional capability is covered by regression tests. Some category matching groups still combine Column and governed Value. These representations are preserved for review, without hand-editing model output.

## 8. Follow-up refinements from real interaction

The first run requested the identical full Concept guide twice. The runtime now compacts only the model's temporary message view: for identical successful guides of the same node type, keep the latest full result and replace earlier copies with a short reference. Preserve raw transcripts, changed contracts, errors and ordinary graph evidence. Apply this before the existing budget trim so the latest result is retained; the existing budget fallback may still externalize it with a readable evidence_ref. If earlier history was already dropped, the remaining guide is not suppressed. No persistent cache or new tool is introduced.

Learning and the existing Assessment grounding/predicate_precision criteria now also clarify two general rules: a governed Value already identifies its owner Column, so equality alone does not justify a combined Column+Value target; an evidenced named business role should be expressed by a specific predicate, with generic has_attribute reserved for cases lacking a supported narrower role. These are semantic instructions, not case-specific deterministic transformations.

The independent r1 review found observed P/1, P/0, N/1 and N/0 combinations in the recorded Virtual/Charter investigation. Thus overlap and lack of either inclusion are supported for recorded classifications. It recommended narrowing “independent” wording and avoiding nullability as the reason for non-exhaustiveness. No extra structure or assessment key was added. r1 is preserved; later runs use the refined code rather than manually editing that graph.

The real transcript exposed a second detail: each read generated a fresh contract_ref even when the guide content was unchanged. Compaction therefore ignores only top-level contract_ref and revision for comparison. It preserves the latest values of both fields, all nested schema content, registry definitions and instructions. Independent review confirmed this is a bookkeeping distinction, not a semantic summary. A regression covers fresh evidence IDs across revisions; changed schema remains separate. No cross-run cache is used.

Independent r4 review identified a semantic family error: category_of (category to attribute dimension) is not a specialization of has_attribute (object to attribute role), despite both endpoints being Concepts. The guide and existing endpoint_meaning criterion now require child(a,b) to imply the parent meaning with the same business roles; matching JSON endpoint types is insufficient. This remains a semantic Assessment responsibility, without a new check or deterministic predicate-name rule.

Final runtime/full regression reached 919 passed; the subsequent two semantic guidance additions passed the 69-test agent/Assessment/feature suite. A replay of the r4 raw transcript through the final temporary-view compactor removed two duplicate guide bodies, saving 16553 serialized characters while retaining raw evidence. This is a character comparison, not a measured token or latency improvement.

## 9. Final delivered run: case60_r5

The delivered graph is the formal accepted/sealed revision 258679e2a3a54c0bb0a29a24f26835ec. Completion is stable_correct, with two correct replays on that graph. The final Assessment reviewed sixteen units, all valid; the last acceptance evidence has assessment_acceptable=true and sql_stable_correct=true. accepted_revision equals final_revision. The earlier partial acceptance is preserved as history and is not mistaken for full acceptance.

An automatic repair clarified Charter 1/0/missing semantics and replaced permanent profile-frequency wording with supported Website optionality. Independent review reconfirmed the revised graph. Five Concepts and nine authored binding/business edges remain; the business view includes fourteen nodes and seventeen edges, and the full catalog graph has one hundred nodes and one hundred three edges. No Metric, Event or GenerationRule was forced into the case.

This final graph uses generic has_attribute for Website and County location, plus is_a for the two school categories; it does not demonstrate a specific attribute child contract or relation_metadata. Those remain optional tested capabilities; r1 retains an actual generated relation_metadata example. Generic predicate usage and “independent” wording are review choices, not claims that every attribute has been fully specialized or that statistical independence was proved.

Review entry: output/ontology_v59_case60_relation_review/review/index.html. Raw snapshots, interactions, both Assessments, acceptance evidence and intermediate runs remain available; no graph was hand-edited. Context audit is saved in agent_context_audit.json and independent semantic reviews in independent_semantic_review.json.

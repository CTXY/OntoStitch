# 局部 Reconciliation：编辑、编译与独立评估

当前协议由 `reconciliation_contract.py`、`reconciliation_compile.py`、`reconciliation_context.py` 与 `reconciliation_agent.py` 实现。可编辑中文 prompts 位于 `llm/prompts/reconciliation/`，共用前缀与角色契约先排列，末尾只追加本轮 signal guidance。

Writer 只维护图修改草稿。没有决定台账、`put_decision`、repair/keep/deferred、依赖等待队列、身份阶段路由或模型填写的版本号。Conflict 是已接受知识的竞争版本表示，与执行是否阻塞无关；合法 conflict 可以保持原样。

## 一次局部事务

1. Controller 固定 before、Signal/明确义务及必要局部节点/关系；物理目录只读。完整内容及谓词契约已提供时不开放重复读图，原始 Question/Gold SQL/evidence 可按 ID 读取。
2. Writer 通过 `edit_draft({put_changes,remove_changes})` 维护完整草稿；`change_id` 相同表示替换同一草稿项，remove_changes 只撤回草稿项。格式错误不保存；合法编辑先保存，再自动编译。
3. 编译以固定 before 重建候选，检查授权范围、端点、类型、谓词、metadata、计算直接依赖、grounding、来源和 conflict 分支保护。失败保留草稿及完整错误；相同草稿且所有绑定材料未变时复用编译，不重建模型结果。
4. 每次 `edit_draft` 都表示本轮完整候选；编译成功自动进入 Assessment，不再调用 Writer 确认。`edit_draft({})` 显式请求评估当前草稿（初始空草稿也适用，已有修改不会清空）。版本、候选哈希、输入来源、策略与 schema 绑定均由代码维护。
5. 独立 Assessment 比较 before 与真实 candidate，只检查局部结果是否合理、原有有据知识及来源是否保留，直接返回 `explanation` 和 `feedback`。API 请求启用 JSON object 输出格式，严格校验 schema；不使用 Review 结果工具。
6. feedback 为空才接受；否则具体问题及必须达到的结果返回同一 Writer。格式错误仅反馈给当前 Assessment 修正。重复相同候选、反馈与可见材料会停止并保存 checkpoint。

```json
{"put_changes":[{"change_id":"retain-input","kind":"edge_replace","edge_id":"old-input","replacement_edge_id":"retained-input","reason":"旧关系知识由实际存活的直接输入边承接。"}]}
```

`edge_replace` 将旧边来源精确并入真实后继再删除旧边。`edge_remove` 只撤销错误/无据断言，必须引用公开 before 事实，完整原来源保存在审计记录中；有应保留后继时不能用撤销替代。原有来源不能由模型直接覆盖。

```json
{"explanation":"局部关系仍有问题，原来源尚未正确保留。","feedback":[{"problem":"旧关系的一份来源缺失。","required_outcome":"将原关系来源保留到实际关系后继。","refs":[{"view":"candidate","kind":"edge","id":"retained-input","field":"evidence_ids"}]}]}
```

## 每轮上下文

固定三条消息：system；固定任务与首次完整局部图；当前唯一草稿、实际候选变化、新增读取与有效反馈。每轮重建，不累积旧对话。未变节点正文沿用 before，增量只发送状态；历史问题的原候选快照留在 checkpoint，不能重新标为当前事实。公开对象投影同时用于输入与引用校验，输入中的 evidence_ids 可以直接引用，私有 sources 不可引用或写入。

## Checkpoint 与节点整合内部调用

Writer 完整编辑意图与草稿原子保存，编译成功持久化候选与待评估状态。恢复已保存的完整编辑先机械编译/复用绑定，通过则直接进入 Assessment，不重复调用 Writer。编译失败保留草稿及具体诊断，交 Writer 修订。初始化空草稿的预编译不代表 Writer 已判断，恢复后仍需一次 Writer。完整旧任务历史保留。输入、schema 或策略发生变化不能静默复用旧评估。

节点整合内部触发的关系协调使用自己的预编译/自动移交回调及绑定，不借用外层节点事务。内部反馈仍回内部 Writer，不重跑节点 Writer；内部候选未独立接受前不发布到已接受图。

本次旧 checkpoint 不自动迁移或切换后台。完整测试记录见 `output/reconciliation_auto_assess_20261121/`；早期设计报告保留为历史记录。

## Conflict 内容与关系范围

不再提供分支编辑操作。普通节点更新拒绝 conflict；完整 alternatives 仍用于读取及比较。允许依据固定分支含义修复边的 alternative_ids，不能选择终点版本或将分支独有知识广播。需要裁决竞争内容时保留 checkpoint 并明确说明问题，不绕过限制。


## 节点整合与最终关系协调的阶段边界

已确认同一身份的 Metric 和 Concept 均应完成各自 group 的 consolidation，重定向引用和关系，再运行 Relation Merger 与最终 Reconciliation。若 Concept group 尚未完成，应处理其节点整合；若 Identity 漏分组，应补正身份判断。只移除重复 Concept 的依赖边不能替代 Concept 整合。

历史 `metric_retirement` 样本只重建 Metric group 合并后的局部中间状态，两个 Concept 尚未整合。保留它用于内部关系编译及来源迁移回归，并显式标注 `node_consolidation_coupled_repair`；它不再进入默认最终 reconciliation 小测列表。该分类修正不是生产阶段回跳调度的实现，也不证明其上游 Identity/Concept consolidation 已经通过。

Writer 空编辑的合法参数为 `{}`，或等价的 `{"put_changes":[],"remove_changes":[]}`；没有 `empty` 字段。空编辑保留当前草稿，编译成功直接独立评估；不等于清空已有修改，也不等于自动通过业务义务。

## Single-graph contract (2026-11-21)

The current merge contract does not author `relation_metadata`. Legacy business
statements are explicitly migrated into the owning Concept definition, preserving
complete prose, matching methods, branch IDs and per-assertion provenance. The
migration is not conflict resolution. It does not discard genuine multi-relation
knowledge or replace source attribution with a group of repeated edge IDs.

Reconciliation receives one current working ontology graph and an evidence
repository. `before` is this graph at the start of the local transaction;
`candidate` is its compiled change, not an archived case graph. Historical source
handles, source snapshots and source-view references are not public tools.

Core business anchors are derived from the task and actual nominated edges.
Boundary nodes provide content without gaining adjacency expansion rights.
Physical tables and columns never recursively expand the local neighborhood.
Predicate catalog browsing is read-only and on demand; normal tasks initially
receive the predicates actually used by their local relations.

`integration/single_graph.py` resumes a local task using G, evidence and its
checkpoint only. A pending Assessment does not replay Writer; committed tasks do
not replay either model. Policy changes still require explicit migration.

An existing binding contest may remain while an independent local Signal is
processed, but a transaction cannot create or enlarge a contest. A validation-only
copy checks other invariants without those existing collisions masking errors.
Final publication continues to run strict global validation; local acceptance is
not a declaration that the database graph is finished.

Regression artifacts: `output/single_graph_merge_fix_20261121/index.html`.

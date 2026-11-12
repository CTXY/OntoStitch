# v58：relation_metadata 实现方案的复杂度审查与收敛

日期：2026-11-05。本页为实施前审查历史；后续已落地，实际实现及受控边界见 [v59 实现说明](ontology-relation-groups-v59.md)。

## 审查范围及结论

两个 subagent 分别只读审查结构/状态/消费者和 Assessment。原方案把必要完整性适配与未来通用能力混在一起，应收敛为：小数据结构、结构校验、已有状态管理、交接处局部 ID 替换、临时检索视图过滤、现有语义评估和现有展示。

明确删除新增 `relation_metadata_consistency` 检查的提案；不增加质量键、评估对象、输出状态或验收门禁。

## 保留的最小结构

Concept 可选 relation_metadata，每项仅 id、edges、description。与所属 Concept 相邻、同一具体关系类型及相对方向，组 ID 和边引用不重复。组只约束列出的边；无组级知识时不生成。描述可以表达互斥、允许共属、实际交集等，但不将自由文本当成代码可执行公理，不自动生成两两边。

父类、属性、Value 的含义仍分开；不存在额外子类知识门禁。可读 Value ID 与核心 has_attribute 不回退。

## 必须处理：不是新增子系统

1. 结构引用校验放到合并后的候选图校验中。新建边和 metadata 可在同一 edit_graph 提交；删除边造成悬空时原子拒绝，提示 Agent 同步修改说明，不能静默删除知识。
2. 复用 normalize_groups 的边端点依赖和 GraphStore.select 的选择/保存校验。用测试验证 metadata 所属 Concept 与相关修改被正确处理，不增加反向索引、另一个接受器或 metadata 专用状态。
3. build_incoming_graph 会将 authored edge ID 变为 incoming_edge_<hash>。这些 ID 只依赖 namespace、case_id、旧 edge_id，因此在 case 开始处预计算 edge_id_map，对 relation_metadata[].edges 一次字段级替换即可。节点/边映射分开，不把边引用混入 property_reference_ids 的节点引用语义。
4. 自动 reconcile 若合并重复边，必须用实际旧→新映射更新 metadata。若引用塌缩为重复成员或无法保留组说明，拒绝并让 Agent 修改；不推断、重写自然语言。这是实际改 ID 的入口适配，不是重构全局 merge。
5. 临时检索图不做强制扩展、不加索引、不改排序。仅保留其全部引用边都在子图中的 metadata 项，其余整项省略；不截短成员，不改原图。组级信息不完整时不对外呈现为完整断言。

## extends 的第一期边界

如本轮实施关系家族，仅允许具体属性关系 `extends=has_attribute`，深度 1。父契约始终在 root，不做任意本地多级继承，不需要中间父契约注册、递归闭包、父契约裁剪或级联修改。

保留 child 契约的原始内容；通过无副作用查询/校验使用父要求，不将补齐字段后的契约覆盖原始契约，否则 effective_schema 重复调用可能误报契约重定义。端点/方向不得违背父契约，父 description 要求沿用现有校验，不引入一般 JSON Schema 蕴含证明。Assessment 在使用 child 时只需同时看到 root has_attribute 契约，各一份。

located_in 等业务谓词仍可独立注册，不强制归入该家族。属性家族不变成 SQL 连接/计算依赖展开规则。

## Assessment 的四层

1. 单元内容：按 node/relation 类型检查意义、最小化、绑定/公式/action、谓词和证据。
2. 跨单元：用已有 graph_consistency、scope、非冗余规则比较关联知识。
3. SQL 应用：沿用代码给定 application_requirement；正确 SQL 不证明知识正确，语义说明也不必产生新 SQL。
4. 条件性：有 fixed_targets 才评估 target resolution，有 prior evidence 才报告 regression。

quality_rubric 是各检查定义的来源，prompt 主要讲顺序、输入边界、路由及输出。不改 response schema 或现有 quality_checks 键，不新增 relation_metadata_consistency。组级声明是否有根据归 support；是否与边/节点矛盾归 graph_consistency。不因没有 metadata 判遗漏。

已有 assessment_payload 从修改边加入端点，再取受影响语义组件，compact_node 保留非空 properties。先用测试确认 metadata 单改、成员边单改时相关原内容已完整提供；不提前新建 related_relation_metadata、只读 owner 单元或上下文展开器。

只检查已声明事实和解释当前行为必要的限制；不得因缺少非必要的 nullable、基数、历史免责声明而判错。COUNT、公式、Rule 的必要 NULL/粒度/操作数仍严格检查。同一个根因可影响多个已有 check，但反馈应合并，不发出重复/冲突的修复要求。

## 必须保留的 metadata-only 应用边界

subagent 复查确认：仅靠 prompt 写“组级说明无需产生 SQL”不足够，现有 Concept application_requirement 无条件为 sql。未来实现时由代码对 raw snapshot 作真实差异比较：已有 Concept 相对 accepted_before，除 properties.relation_metadata 和系统维护来源外完全相同，才使用既有 semantic。不能与上一份 Working 比较，不能使用 Agent 可写 flag。新 Concept、混合 targets/定义/名称等修改仍保持 sql。质量、回归和同图版本的全局稳定 SQL 门禁不变；不新增 unit、check 或应用类别。

## 可推迟

- 任意多级关系继承、统一关系家族检索索引。
- metadata 专用召回/预算/展开机制。
- 新的父节点评估单元、检查键或验收阶段。
- 单独的关系组卡片、点击高亮、分类布局、继承树可视化。
- 将自由文本互斥/兼容说明转成自动逻辑规则。

第一版现有 Properties 已能显示 metadata，边仍显示具体业务谓词；类型契约中显示 extends 信息即可。

## 本次已经调整

只修改 Assessment 指引和设计记录：重排四层，删除逐类型重复展开，移除写死的核心关系数量，明确不要求非必要的 nullable/历史免责声明。保留共享 ontology 定义、全部关键最小化/action/公式/关系规则、输出 schema 和自动门禁。未实现新字段，未重跑模型，不改现有发布图。

## 本次验证边界

Assessment prompt 改写后由原审查 subagent 复读，未发现阻止落地的问题。专项 97 passed；只验证现有输入/输出及门禁兼容，不能据此宣称模型评估准确率已提高。metadata/extends 尚未实现，其结构、映射与应用差异用例列为实施测试，不伪造已经通过。

本次最终回归：专项 97 passed，全量 900 passed。未运行真实模型，未新增 relation_metadata/extends 实现，旧交互图不变。

# Ontology graph 修改记录与验证范围

日期：2026-11-05。

当前 v58：两个 subagent 完成复杂度审查，删除新增 relation_metadata_consistency 检查提案；Assessment 指引重排为内容、跨单元、SQL 应用、条件性目标/回归四层，修正核心数量写死与非必要 nullable 免责声明歧义。保留输出 schema 与全部检查键。专项 97 passed，全量 900 passed；未运行模型。尚未新增 metadata/extends 字段，实施规格收敛为局部完整性适配。见 [v58 审查结论](ontology-relation-metadata-simplified-v58.md)。

上一轮 v57：回退额外子类知识门禁与 subclass_value；保留可读 Value ID、核心 has_attribute。生成与 Assessment 强调具体业务谓词和业务优先 description，物理绑定仍保留必要字段含义。兄弟类别约束、has_attribute 关系类型层级尚为提案，未增加 schema 字段、推理行为或重跑模型。见 [修订与提案](ontology-business-semantics-v57-proposal.md)。

历史 v56（子类门禁已回退）：子类准入独立于逻辑包含，要求有证据的父类/成员条件之外的业务断言；新增 subclass_value 质量检查，普通 Concept 无额外要求。has_attribute 提升为第八个核心关系，保留可扩展业务契约；新 Value ID 采用可逆编码，复用旧已验证 ID。case60 已正式 stable_correct，六个属性/对象/类别值 Concept，无学校子类、Metric 或 Rule；两次正确 replay，18 个 Assessment 单元通过。全量 901 passed。实跑与验收记录见 [v56 设计及实施](ontology-subclass-admission-v56.md)。

上一轮 v54：将语义谓词与数据库记录措辞区分，生成/Assessment/设计规范同步，Event 示例用 occurs_on_date。正式重跑 case47，新增 case60、case77，均 stable_correct，连续两次正确 replay 且对应 Assessment 通过；无 review_only。case27 四次评估仍未接受，保留 checkpoint 和诊断图后中断，不作为通过结果。全量 883 passed。见 [业务关系修改与真实运行](ontology-business-relations-v54.md)。

状态管理澄清：正式 extraction training 不使用 `review_only`、`formal_case_acceptance` 或人工批准门禁。上轮 v53 候选由额外实验脚本保存，未闭合正式验收，不能作为正式训练输出。人工阅读/评审与训练状态独立。正式状态流转及证据约束见 [extraction training 状态管理](ontology-extraction-state-management.md)。

上一轮 v53：School 同一 Concept 的 identifier/name 表示已在真实 case47 中保留；Concept 表示绑定按语义支持评估，节点实际应用与计算/动作仍须验证。补充通用 Metric 拆分反例、占位符不能隐含计算的要求，评估优先检查可执行节点；SQL 生成按 target 的角色选择，不默认主键、DISTINCT 或第一项。全量 883 passed；最终图在当前 SQL 模板下连续两次正确，完整 22 单元评估通过。该结果是待用户 review 的 validated_proposal，原训练验收记录未被覆盖。详情见 [School 表示角色复验](ontology-case47-school-role-review.md)。

上一轮 v52：将 Concept 的 grounded_on 表示绑定按语义支持评估；应用要求根据实际源节点类型由代码生成，非 Agent 可写标签。Concept 节点仍检查请求相关含义及匹配在 SQL 中的应用，Metric 物理操作数/公式与生成动作仍需 SQL 应用。避免强迫 entity identifier 出现在 SQL 或把业务主体改名为名称属性。专项 111 passed，全量 883 passed；case47 正在新工作区真实复验。

上一轮 v51（指引更新）：补充 School 示例，并修正 v50 将 identifier/name 不同物理角色过度等同于不同 Concept 的要求。Question 使用 School 时保留 School 业务主体，可通过独立 targets 匹配 identifier 和 name；角色与限制写在 Concept/边 description 中，不强迫拆出 School name。Assessment 按请求相关角色检查应用，不要求每个 target 都出现在 SQL 中。相关指导/质量/输入回归 76 passed，Ruff 通过。此次未重跑模型，浏览器 v50 图保留原始结果。

上一轮 v50：Concept 新 extraction 写入仅允许 Column/Value targets 及相应绑定，拒绝 Table 匹配；真实联合匹配与独立匹配在 guide/Assessment 中明确，identifier/name 不混为一组。Table 记录来源、主键、唯一键及已有说明按 metadata 提供。关系 description 简洁说明具体联系与有证据的约束，区分记录/对象/时间范围，不写 profiling 数字或证明过程。历史读取保持兼容；upsert、冲突候选及显式 Table 绑定的拒绝均在持久化前执行。全量 881 passed、1 个既有 warning；case47 已完成：5 Concept＋1 GenerationRule，8 条语义出边；连续两次 SQL 正确，最终 15 个 Assessment 单元全 valid。旧 ontology/extraction 测试产物 7 个目录、7,095 个文件已清理，新结果及清单保存在 output/ontology_v50_case47_review。详细决定见 [v50 匹配与关系要求](ontology-matching-and-relations-v50.md)。

上一轮 v49（含 v47–v48）：按新设计审计 Learning / Assessment / Diagnosis / SQL generation 输入与工具。Learning 简短系统消息补充 NL 语义最小化和 action 最小化；Assessment 删除重复说明，并按需收到一份完整精简 operation registry，用于校验标签及扩展缺口；评估策略指纹包含 registry 内容。现有六个 Learning 工具及只读 Assessment 足够，未增加工具。专项 108 passed，全量 876 passed（1 个现有 warning），Ruff 通过。输入及修改说明见 [Agent 输入审计](ontology-agent-input-review-v47.md)。v48 进一步标明 Gold/Assessment 物理字段输入不是完整 catalog，避免把未展示字段当作不存在；身份、粒度及缺失声明须有相应范围证据，完整 metadata 按需读取。v49 补充候选自写定义不能证明 NL 原子性、常规筛选不能凭无关替代项获得 Rule 准入。最终精确去重补丁仅合并 Assessment 视图中相同的 description/definition，保留差异声明与原图；全量 877 passed、2 个既有 warning。case47_r3 已在全新工作区完成：5 Concept、11 条有描述的语义出边，连续两次 SQL 正确，最终 18 个 Assessment 单元全 valid；待用户 review。确切 revision、完整交互图及修订记录见 [case47 v49 review](ontology-case47-v49-review.md)。

上一轮 v46：按节点内容重写最小化。Concept / Metric 共用 NL 语义分解判据：是否能拆成分别匹配字段、取值或操作的完整 Concept / Metric / NL term，并无损组合回原义；不局限于已有 Concept＋通用计算意图。多 targets、重合 targets、公式展开均不自动证明可拆。GenerationRule 单独检查最小、完整、可复用的 action，准入价值独立评估。设计文档 §2.2、F9/F10、类型指导及 Assessment rubric 已同步；失败反馈需说明拆分及匹配证据，不新增字段或确定性 NL 分解规则。

v46 验证：test_learning_inputs、test_quality_contract、test_semantic_assessment 共 72 passed；所改 Python 文件 Ruff 通过。覆盖类型指导/上下文长度、质量门控及语义评估相关回归；此次未重新运行真实模型案例，旧 case47 图不代表 v46 验收结果。

上一轮 v45：root schema 已缩减为 7 种核心关系（belongs_to、value_of、joins_with、grounded_on、depends_on、applies_to、is_a）。九种业务谓词不再默认注册，按业务场景提交本图契约；computed_from、filters_on、expands_to、related_to、aligns_with 在 Learning 中停止新建/修改，历史定义单独保留只读解释。读取兼容不授予新写许可，不静默改写历史图。

Metric 的 metric_minimality 独立评估项已加强为“名称与定义是否完全等于 Concept 意义＋通用计算意图”，保留有证据的业务比率所需分子、分母、总体和粒度；不做词面规则，不因 rate/average 等名称直接判定，不自动为拆出的普通操作创建 GenerationRule。设计文档 §2.6 增加 F1–F9 形式化约束，分别标记代码与语义评估责任；未增加通用推理器。

上一轮 v43：Direct 与 Repair 共用完整 operation registry，移除阶段专属 intent 列表及 Direct joinability 拦截。统一规则准入：有证据的语义实现选择、最小可执行 action、必要局部 SQL pattern；允许关联业务名词，偶然请求参数仍泛化。概念 Value 匹配不再由额外 Column 筛选边重复表达。case47 新工作区实跑与最终图见 [case47 v43 review](ontology-case47-v43-review.md)：最终展示候选通过连续两次 SQL replay、28 单元全图 Assessment 与独立 subagent 复核；待用户 review。

上一轮 v44 修改：关系契约与全图完整性检查。新谓词通过 edit_graph.relation_contracts 原子提交并保存在 GraphSpace.relation_contracts，复用 EdgeType 定义持久化契约，不增加业务节点类型或复杂推理器。指导明确一般 is_a（Concept/Concept 或 Event/Event）、category_of 的取值意义、类型级角色、当前数据库/生效定义作用域，以及有限可核查的断言去冗余。Metric 直接公式边不能被间接路径替代；删边先检查支持链及消费者。允许多种合理图结构，不要求 Event 压缩或最小图。2026-11-04 完成代码整合与独立复审：全量回归 863 passed、2 个既有 Pydantic warnings；末轮接受隔离补丁另有专项复验通过。两位 subagent 独立复审均通过，明确以简洁有效为要求。该结论验证设计/实现，不把 v43 实跑结果视为 v44 模型产图验收。

指导中的旧“所有 targets refs 必须逐一 grounded_on”“自定义谓词无须登记”“reference_origin 决定依赖语义”已移除；SQL validation 按完整 targets/target_expressions 及显式边消费。v44 曾保留 21 个谓词；v45 的核心、按需业务关系及历史兼容划分见设计文档 §4.1，历史定义不被静默丢弃。指导专项检查通过：asset_contract Ruff/编译、validation YAML 解析，以及 learning_inputs 中 system/contract/schema 等选定的 9 项测试；这不是整合代码全量或真实模型验收。

本轮按 [设计规范](ontology-graph-design.md) 修改 extraction training 与必要的共享图接口。正式图由保存的节点和显式边组成，业务关系不再由通用 refs 编译。真实模型实跑已推进至学校分类、FRPM 计算、金融交易、学校开办和关闭案例；早期 HTTP 503 已通过实验配置切换至服务实际列出的模型 ID 解决。最新运行、语义缺陷、修复与独立复核见 [实跑评估记录](ontology-live-evaluation.md)，原有失败 artifact 保留。

## 1 已实现的契约

| 内容 | 当前实现 |
| --- | --- |
| Concept | targets 为非空 list[list[str]]；每个内层列表是一项联合匹配；允许跨 Concept 重合 |
| 复杂匹配 | 可选 target_expressions，以字符串组索引如 `0` 保存完整 SQL 表达式；直接表/字段/值匹配不强造谓词 |
| 连边唯一性 | 统计实际 Concept grounded_on 到同一物理端点的连接；不统计 targets 出现次数 |
| Metric | 有实际计算；保留 expression.expr 与 expression.refs；操作数可为 Concept、Metric 或允许的物理节点 |
| 存储量与恒等包装 | 存储字段对应 Concept；字段直通、单纯 CAST、乘 1/加 0 等不能用来伪装 Metric |
| GenerationRule | operation_intents 为多标签列表，允许新增标签；解释沿用 description/reason，没有新增标签审批流程 |
| Event | 本轮选择可选分组 targets；允许通过显式业务联系组织锚点；事件结构是否充分交由 Assessment 判断 |
| 业务关系 | 独立 edges，必须有 properties.description；使用具体业务谓词区分角色，不增加 qualifier/role/condition 字段 |
| 物理目录关系 | belongs_to/value_of 仍可由确定性代码构建并保存说明；不是 Agent 业务关系推断 |
| 公式漏边 | 当前实现返回 MISSING_FORMULA_RELATION，要求 Agent 补显式边；未启用可选的公式漏边自动补全 |

Event targets 可选和新字段的精确序列化是本轮采用的实施选择，不冒充此前用户逐项确定的设计共识。未新增 Constraint、Entity、Process 或复杂探索证据系统。

## 2 代码框架

节点模型位于 `src/cortex/capabilities/sql_training/models/`。共享语义检查、引用收集与匹配视图位于 `src/cortex/schema/semantic_validation.py`、`references.py`、`bindings.py`。引用收集用于检查悬空 ID、重命名和匹配，不再代表边生成指令。

`src/cortex/application/ontology_graph.py` 对同批节点及显式边做最终状态校验：固定关系端点、description、直接连边唯一性、公式与直接操作数边的双向一致性、候选分支、计算依赖循环和删除安全（类型级业务环交由带范围的语义评估）。Concept targets 中有输入不要求该 Concept 对每项直接连边。

`src/cortex/schema/relationships.py` 中原同步器已收窄为 catalog 归属构建；不生成 Concept/Event/Rule/Metric 的业务边。Agent 编辑工具明确要求提交 edge_operations；写入不再采用旧的 default/override 语义边恢复方式。

共享 Markdown 契约和类型 guides 通过 progressive disclosure 加载；Direct 与 Repair 使用相同的新节点与关系规则。Assessment 继续区分质量、SQL 应用、错误修复和回归，不增加必要知识覆盖率评分，也不要求读取新的探查证据接口。

在线投影、检索索引、merge 的最小消费接口均读取新匹配分组和已保存边。自定义业务边不会因为不在旧依赖白名单就被排除。匹配选择时同步重排 target_expressions 索引，避免表达式挂到错误匹配。

case fragment 与 ablation export 接口补充显式边输入，导出结果保留 draft_edges，消费时读取已保存关系，不再只根据节点恢复图。全局 merge 算法未重设计。

## 3 生命周期修复

- 同批新建冲突节点后可以立即提交其显式边。
- 仅对 Agent 已提交的公式边计算必要的分支元数据；不生成新的业务关系。
- 选择一个候选分支时，其他分支的边不会被转成无条件事实。
- 范围收窄后完全相同的事实可以合并来源；不同 description/范围不会仅因端点相同而静默覆盖。
- 公式和边必须一起接受；接受节点但漏掉必要公式边会被拒绝，不自动补成未经评估的新图。
- 删除普通显式业务边不会因 targets 保留而恢复旧默认边。
- incoming fragment 只校验 incoming Metric 的公式边，不把缺少 existing 边的局部视图误认为完整图。

## 4 测试与 review

新增回归覆盖重合匹配、直接边唯一性、Concept 公式操作数、缺失公式边反馈、显式业务角色、删边不恢复、分组投影、表达式索引及 catalog 可确定字段的匹配一致性。

旧夹具中自动 refs 编边、无绑定 Concept、存储量 Metric、role/condition 事件角色与旧 targets 对象格式不再是当前契约。相关测试迁移为显式关系行为；没有通过生产兼容 adapter 或跳过测试保留旧承诺。旧历史 artifact 没有被改写。

上一阶段全量回归结果为 832 passed、2 warnings（87.24 秒），日志位于 `output/ontology_explicit_v2/pytest_full.log`。独立实现 review 未发现必须修复项，另行重跑 58 项通过，报告见 `output/ontology_explicit_v2/independent_implementation_review.json`。独立审计记录位于 `output/ontology_explicit_v2/snapshot_audit.json`：对脚本 Agent/GraphEdit 实际保存的 13 份快照检查硬规则，没有发现违反项。该审计不证明外部 LLM 语义质量，也不包含 Event 质量结论。

本轮初次广范围 Ruff 扫描还报告了工作区其他既有文件的问题；本轮改动的文件单独检查，不借机改写无关代码。

## 5 BIRD 实跑

首次实跑因 `.env` 配置模型的请求返回 503 中断，原始失败见 `output/ontology_explicit_v2/live_attempts.json`。本轮只读检查服务 models 列表，并在实验配置采用可用的 `deepseek-flash`；未改用户 `.env`。

真实模型产物、SQL replay、Assessment 与独立 subagent 复核已保存在 `output/ontology_live_eval/`。独立复核将 SQL 正确与业务知识正确分开；发现错误的接受图也保留为失败历史，不人工修图冒充新输出。最新案例和测试结论见 [实跑评估记录](ontology-live-evaluation.md) 与自动生成的 `output/ontology_live_eval/review/index.html`。

本轮运行发现并修复 Metric/Rule 重复、类别与属性槽混淆、数值与集合混淆、方向/条件不一致、端点语义漂移以及工具反馈路径/停滞状态问题。数据库保持只读，没有 metadata 优化或新的探索证据子系统。此轮五例检查不能替代全 BIRD 评测或全局 merge 一致性验证。五例已各有一版通过独立复核，但中性提示重跑仍暴露 Metric 最小性和内部评估漏判，不能宣称抽取已稳定满意。最新全量回归 838 passed、2 warnings。

## 6 格式兼容

这是 schema 不兼容更新：旧语义节点 references、对象式 targets、单值 operation_intent、缺少 description 的边会被拒绝。旧训练快照与示例应重新生成，未加入运行时静默语义转换。


## 当前两例收敛状态

最新以 [两例 review 规范与结果](ontology-two-case-review.md) 为准。closed school count 不再被认可为 Metric；规则独立决策准入、抽象程度及边语义均有分项质量检查。两个最终提案通过 SQL/质量验证，尚待用户 review，未扩大测试。当前回归 846 passed、2 warnings。


## v44 整合验证与边界（2026-11-04）

- 全量测试：863 passed、2 个既有 Pydantic 弃用警告，103.27 秒；末轮契约接受隔离补丁另行通过专项回归。日志：[pytest_full.log](../../output/ontology_contract_v44_review/pytest_full.log)。
- 两位独立 subagent 最终均审查通过。审查要求整体设计简洁有效；复用 EdgeType、GraphSpace、Assessment unit 和 ChangeGroup，没有通用推理引擎、Constraint 节点、qualifier、结构最小化或 case-specific 生产规则。记录：[independent_review.json](../../output/ontology_contract_v44_review/independent_review.json)。
- 修复了首次登记旧谓词绕过边评审、同契约多边部分接受和仅接受无关节点泄漏候选契约的问题；新契约必须随使用它的边提交，相关边原子评估/接受。
- 可推导冗余只在范围、证据与消费者能力均明确时构成可安全删除的问题；不自动物化推导或删除业务边。不声称自然语言关系已获得完整形式证明。
- 本轮没有重新运行真实模型生成 case47，也没有改写历史 r8 图；浏览器里的图仍是此前产物。root schema 版本发生更新，原有严格 schema_version 检查意味着应启动新训练工作区，不静默重写旧快照。


## v45 验证（2026-11-04）

- 全量测试 **875 passed**，2 个既有 Pydantic 弃用警告，64.22 秒。日志：[pytest_full.log](../../output/ontology_core_v45_review/pytest_full.log)。
- 核心关系/历史兼容/显式写入/图校验定向 128 项通过；Metric 质量与指南、精简关系专项 65 项通过；本轮修改文件 Ruff 通过。既有 compact prompt 预算未放宽。
- 新增测试验证默认关系精确为 7 种、按需谓词须有本图契约、五种历史谓词不能补契约绕过 Learning 限制、旧边可读但不授权同谓词新写。既有 metric_minimality 失败/缺项不能被正确 SQL 覆盖的验收测试继续通过。
- 真实模型没有在本轮重新生成 case47，历史图未改写。此测试结论不代表模型已可靠解决所有 Metric 语义最小化问题，也不沿用 v44 的独立 subagent 结论作为 v45 的新审查记录。

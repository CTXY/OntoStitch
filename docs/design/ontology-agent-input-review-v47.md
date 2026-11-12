# Ontology Agent 输入与评估改动（v47–v49）

> 历史实验记录：对应旧测试产物已于 2026-11-04 按用户要求删除；保留本文作为设计与实验历史。当前结果见 [v50 匹配与关系要求](ontology-matching-and-relations-v50.md)及新版 review。

日期：2026-11-04。设计依据为 ontology-graph-design.md 的节点、关系契约及 F1–F10；此次只运行 BIRD case47，尚未得到用户对新图的批准。

## 1 修改内容

Concept 与 Metric 的最小化依据 NL 语义及独立匹配职责。GenerationRule 的最小化依据完整生成行为的 action 边界。Learning 的简短系统消息现在也说明这一区别，按类型 guide 提供详细定义和可用字段。Metric 是否具有真实计算、Rule 是否具有有证据的实现选择，分别检查，不以最小化代替准入。

Assessment 保留独立的类型/最小化/绑定/公式/动作/边语义/全图一致性等质量项。失败反馈需要指出语义单元、各自匹配及完整组合，或独立行为及适用条件；SQL 成功不能覆盖语义失败。现有反馈的 gap、required_change 和 preserve 字段足以表达这些意见，未新增响应层级或专用拆分工具。

发现并修复了一个输入缺口：Assessment 原先要求核对 operation intent，却没有收到 registry 的标签定义。现在仅在相关当前/前序知识中含 GenerationRule 时，顶层提供一份完整精简 registry；保留全部标签定义使“新增 intent 确实存在 registry 缺口”的判断有依据。候选扩展仍通过规则自身 reason 说明，不自动写入全局 registry。评估策略指纹也包含 registry 内容，修改标签定义后不会复用旧评估策略身份。

实际初稿还暴露了默认物理上下文的范围歧义：Gold 使用字段不构成完整 catalog，不能据未展示某个字段就声称数据库没有标识符。v48 为 Learning 的 physical_input_scope 和 Assessment 的 physical_context_scope 明确说明这一边界，support 评估项要求身份、粒度和字段缺失声明有相应范围的证据。完整结构仍通过现有 inspect_database(mode=metadata) 读取，不向每轮上下文灌入全部数据库 schema。该改动没有按 case、表名、字段名或字面值设置分支。

首轮 Assessment 对标识符声明给出了有效拒绝，但错误地放行请求聚合包装和常规年份筛选规则。v49 据此进一步说明：候选自行编写的定义、公式或单位不是语义不可拆分的证据；请求操作作用于可独立匹配术语时，嵌套操作或重新命名不能使其原子化。规则需要有证据的额外歧义/口径选择，常规字段、字面值或日历筛选不能仅靠列举无关替代字段获得准入。此处均为通用 prompt/语义 rubric 调整，没有确定性识别 case 或删除某类业务节点。

## 2 Prompt 与上下文

Prompt 结构查阅了 [Anthropic 官方指引](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) 中的明确约束、顺序步骤和区分指令/示例的建议，以及 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 对明确 JSON 输出及格式示例的说明。保留 Markdown 分节与类型 guide 内 JSON 示例；没有照搬其他模型的专用参数或声称格式能保证语义正确。

Prompt 延续 Markdown 的 Task、Inputs、Semantic definitions、Review、Feedback、Output 结构。共享语义定义保留一次；删减 Assessment 单元说明中与这些定义及 quality_rubric 重复的段落，保留评估流程、应用归因、逐次 SQL 判断、删除安全及输出要求。

| 项目 | 检查结果 |
| --- | --- |
| Assessment prompt | 23,988 → 20,784 字符，减少约 13%；不含单例证据及响应 schema |
| Learning 完整 system | Direct 4,539 字符；Repair 4,502 字符 |
| 精简 operation registry | 1,803 字符，按需 guide/Assessment 顶层各自提供 |
| 详细 taxonomy | 5,692 字符，仅显式要求详细实现示例时读取 |
| 每轮 case context / system | 不重复注入完整 registry 或所有类型 schema |
| Relation guide | 提供当前允许关系及端点；按需登记业务谓词契约 |

这里的精简 registry 与可选详细 taxonomy 是同一套定义的两种读取深度。没有发现系统消息或每轮 case context 重复注入 registry，因此未增加对话缓存、强制去重或复杂失效机制。指令要求复用仍在上下文中的 guide；上下文预算仍保留完整模型/工具批次的边界，超长结果保存证据并分页读取。

## 3 核心 Agent 的能力边界

| 环节 | 所需输入 / 能力 | 本次结论 |
| --- | --- | --- |
| Direct / Repair Learning | NLQ、Gold、metadata、当前节点与边、类型指导、operation registry、固定反馈；图检查、只读数据库探索、证据读取、原子编辑、工作笔记和交接 | 六个现有领域工具可支持新设计；未新增业务专用工具 |
| Assessment | 当前/前序节点、相关语义组件、匹配、公式、显式边及关系契约、相关物理 metadata、全部 SQL attempts、上下文归因、固定 targets 和此前证据 | 补 registry；继续只读结构化评估。证据不足明确指出缺口，由 Learning 调查和修订 |
| Diagnosis | 实际错误 SQL / Gold、执行差异、当前知识及匹配上下文 | 定位错误及正确行为，不创建或编辑 ontology 节点；不需要 operation registry |
| SQL generation | 保存的节点和显式边、targets/target_expressions、expression/refs、局部 generation behavior 与连接路径 | 已按显式图消费，未引入额外 refs 推断业务边 |

Assessment 不要求独立数据库探索或新增探索证据系统；不因图形有环、targets 重合、存在多个分类、Event 有另一种表示方式，就判为错误。断言去冗余仍需同作用域的重复/明确推导证据及消费者安全，不扩张为全图最小化。

## 4 验证与真实案例

针对输入、上下文预算、质量门控和语义评估的专项回归：108 passed。最终全量回归：877 passed、2 个既有 Pydantic 弃用 warning；所改 Python 文件 Ruff 通过。日志位于 output/ontology_v47_case47_review/。

v48 范围补丁专项回归仍为 108 passed，Ruff 通过；后续精确去重补丁：仅当节点 description 与 properties.definition 完全相等时，在 Assessment 输入视图保留一份；内容不同时保留两者，原图不变。最终全量 877 passed、2 个既有 warning，日志为 pytest_full_final_current.log。

新 case47 从空白语义图开始，不加载旧图或旧 Agent 对话。配置、源 case、逐步 progress、工具交互、候选快照及 Assessment 证据均保留。实际结果、确切 revision、交互图和仍需 review 的问题在运行完成后记录，不用历史 case47 的通过结论替代本轮验收。


最终 case47 已完成，确切 revision、完整图、业务视图、节点与关系说明、修订过程及用户 review 点见 [case47 v49 实跑记录](ontology-case47-v49-review.md)。Pipeline 连续两次 SQL 正确，最终 18 个 Assessment 单元全部 valid。

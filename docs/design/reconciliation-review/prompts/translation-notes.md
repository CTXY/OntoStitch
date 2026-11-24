# 译文范围与原文注意事项

本目录是**当前运行 Prompt 的完整中文对照**，不是新的精简方案。保留原有标题、段落顺序、项目列表、条件强度及限制；不借翻译修改契约或纠正潜在问题。原文本来为中文的段落照录；节点类型、字段、工具名、JSON 形状、SQL、ID 和公式保持原样。

## 源文与译文

| 当前运行源文件 | 中文文件 | 本次翻译范围 |
| --- | --- | --- |
| incremental_relation_write.md | writer-role.zh.md | 角色名称中的 Graph Reconciliation；其余原文已为中文 |
| incremental_relation_assess.md | assessment-role.zh.md | 角色名称中的 Graph Reconciliation；其余原文已为中文 |
| incremental_relation_shared.md | shared.zh.md | “Node-stage relation obligations”标题及其下两个英文段落；其余照录 |
| conflict_contract.md | conflict.zh.md | 原文已为中文，逐字保留 |
| incremental_protocol.md | protocol.zh.md | 原文已为中文，逐字保留 |

`translation-manifest.json` 保存每个源文件和译文的 SHA256、字符数、行数及逐段对应。段落以空行分隔的连续块计数，包含标题、列表和公式；每段给出源/译文行号、字符数与哈希，发生翻译的段落另保存完整原文和译文。字符数不等于 token 数。

运行时 System 的实际顺序由上层报告说明：当前角色 → 关系共用要求 → conflict_contract → incremental_protocol。这里不更改顺序。

## 忠实保留、未在译文中改正的问题

以下是原文的定位说明，**不是已实施修复**；需结合具体输出操作与实际代码解释，不能据此推定所有情况都会失败。

| 位置 | 原文现状 | 阅读时需区分的边界 |
| --- | --- | --- |
| conflict_contract.md:8 | 冲突混搭示例写为 action 与另一分支 rationale | 当前规则内容通常使用 reason；本译文保留 rationale，不擅自更名。 |
| incremental_relation_write.md:29；incremental_protocol.md:25 | 概括“省略对象字段保留 before 值”“结构化属性整体替换” | 普通更新的按字段/按 property 键语义，与 integrate_groups/conflicts 中完整 NodeContent 的整块替换不同。原文此处没有明确列出完整内容例外，也未在该句明确“结构化属性”指整个 properties 还是单个属性值。 |
| incremental_relation_shared.md:88；conflict_contract.md:63 | 冗余删除要求由最终图推出原断言；有据错误断言允许无 after 弃用 | 二者分别涉及冗余保义与错误纠正，不应机械地把前者当成后一种操作的绝对前提；原文的通用措辞可能需要读者自行辨别。 |
| conflict_contract.md:17、56 | 当前无 scope 出边在当前分支均成立；继承的旧无 scope 边又不能广播到新增 alternative | 前者说同一快照内的有效范围，后者说版本变更后的继承边界；不是简单互相否定，必须保留快照/新增分支这一条件。 |
| incremental_relation_shared.md:33 | Event 要求行为、参与对象及至少时间或地点之一 | 本文件前文明示其仅适用于新建或实质重构，不应拿来重新审计全部已接受 Event；译文不把该条件改成节点字段的硬必填清单。 |
| conflict_contract.md:49 | 解释了 common_evidence_ids 的语义 | 这里没有像节点窄契约那样逐项说明新分支支持资格，不能只凭该段推断 common_evidence_ids 已满足编译器的支持检查；实际字段规则需看工具 schema 与代码。 |
| incremental_relation_assess.md:28–31 | 要求准确的 claim、真实引用和字段路径 | 本角色文件没有展开完整 claim 样例，也没有分支 ReviewReference 的完整填写示例；上层应将实际公开 schema 一并呈现，不能认为这些文字已独立覆盖所有格式约束。 |

## 必须完整保留的英文段落含义

关系共用文件末尾的两段明确区分：普通 signals 可以只定位问题；节点阶段交来的 relation obligations / structural diagnostics 是**必须完成的保义义务**。同时要求读取原关系和映射、保留配对与来源、不得用已退役 ID 或无操作证明问题消失，并明确 alternative_ids 仅限定起点分支。这些强制要求已完整翻译，未简化成一般建议。

本轮仅生成文档，没有修改源 Prompt、工具、运行代码或原测试产物，也没有模型调用。

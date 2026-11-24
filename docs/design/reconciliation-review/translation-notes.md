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


---

# 按需 guidance 的翻译范围

这些材料通过当前 `build_read_tools(...)["read_guidance"]` 的本地纯读取获得；本轮没有模型/API 调用。它们是工具返回数据，**不是初始 System 默认拼入的六份文件**；是否进入某轮上下文取决于真实读取调用。

- concept、metric、event、generation_rule：完整翻译返回的 guidance 字符串，包括重复出现的资产契约标题、版本与前言；fields 结构保留在对应 `*.fields.actual.json`，完整原始返回保留在 `*.actual.json`。
- relations：完整翻译 guidance，并另译同一次返回的 relation_metadata 补充字符串。
- operations：按实际顺序保留全部 14 个 intent ID，完整翻译各项自然语言定义；导读中“不是必须创建的清单”来自 read_guidance 工具自身说明，不改变 registry 定义。
- conflicts：当前 task_tools 的该分支直接返回 conflict_contract.md，已在 `../prompts/conflict.zh.md` 翻译，不另抄一份。不要把 integration/tools.py 中另一路旧 set_conflict 文案误当作当前 task_tools 实际返回。

`translation-manifest.json` 保存实际 JSON/规范化 JSON 哈希、原文与译文字符数、生成代码 SHA、段落/intent 对照及字段 schema 路径。所有内联反引号字面量已逐项核对，保持一致。Concept 的 School 示意保留 targets 语法、ID 和谓词，仅翻译边后面的自然语言说明；没有改写 JSON 或 SQL。

## 原文可能引起误解的边界（未修正）

1. **提取准入与关系协调的阶段区别。** 类型 guidance 来自 database_ontology_assets_v73_case_semantics。其文本包含“每个 case-level Concept 的 Column/Value targets”“可拆分则拆分”“通用请求聚合不是新 Metric”等准入要求；GenerationRule 还包含 Direct/Repair 和 Direct SQL-side triggers 为空的说明。当前关系 System 则要求已接受节点/分支作为基线，不重新审计其准入、公式、理由或原子性。译文完整保留两者；不能把按需读到的准入规则自动用于回审全部已接受内容。
2. **Metric 示例不是重审已接受 Metric 的许可。** guidance 将某些普通请求聚合归为操作，也列出 NL 可拆分的说明；关系阶段应如何判断“新建/实质重构”的范围，仍取决于 System 阶段边界。译文不把这些句子弱化，也不把它们扩成对所有已有 COUNT Metric 的否定。
3. **Event 内容与字段形状。** 原文同时要求有时间或地点证据、非空 description，并允许在显式关系建立结构时 targets 为空；这不是“targets 为空即可创建事件”。这些准入句与实际 fields 的必填项是不同层次，不能把其中一个替代另一个。
4. **可选字段的 default:null。** 实际返回的 fields 原样包含 required/type/default 等机械元数据。default:null 与显式填 null 的区别不能仅靠示例猜测；本翻译保留原始字段 JSON，不擅自修改或扩充字段类型。
5. **Registry 定义不等于 Rule 已有准入依据。** operations 只说明 intent 的作用域；类型 guidance 明确还需要有证据、会产生实质结果差别的实现决策。不能因为某个 SQL 操作能匹配 registry，就要求新增 Rule。
6. **重复前言是当前真实返回。** 四类 guidance 各自重复完整资产契约前言，这是当前工具的原始行为；译文没有为了缩短而删除，也没有假定 controller 会把这些前言合并。

以上仅记录原文边界，不代表运行代码、Prompt 或历史结果已经在本轮被修改。

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

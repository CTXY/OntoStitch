# Alternatives：完整版本、来源与关系的一致处理（拟议语义契约）

本页规定冲突内容在 Reconciliation 中应如何理解、保留与评估，不新增工具字段、不改变存储格式。canonical draft 的操作字段以主设计为准；已有身份的完整内容重写统一使用拟议 `node_rewrite`，下文明确其语义边界。示例仅使用当前已有的 `NodeContent` / `GraphEdge`；它们不是一个新的工具 envelope。

## 1. 先区分相容表达、匹配方法与真实冲突

| 情况 | 合理处理 | 不能做的处理 |
| --- | --- | --- |
| 同义名称/等价措辞 | 统一名称，保留真实 aliases；去除重复措辞与完整重复版本，归并实际支持来源 | 仅因措辞不同制造冲突 |
| 相容互补知识 | 合成完整一致定义，保留不同角色、条件、匹配方法和关系 | 把较弱但相容的说明判为矛盾；把未写内容当否定 |
| 同一 Concept 的多个相容匹配方法 | 在 targets 外层保留每套完整方法，内层联合结构不变；同步表达式索引 | 每种物理表示都做一个 alternative，或将所有组拼成全必选联合组 |
| 有证据的不同条件 | 在现有字段中明确可识别条件及完整行为；原条件下可还原输入知识 | 用 case ID、问题编号或任意假设编造条件边界 |
| 在重叠范围内实际不兼容 | 同一身份保留完整 alternatives，分别保存其内容、来源和关系范围 | 投票、按当前查询偏好选一个，或把不兼容字符串拼成含糊的“X/Y” |
| 兼容性/身份前提不足 | 读取具体缺失内容/来源；仍无法判断就报告具体前提缺口 | 默认“未知=冲突”或“未知=可以合并” |
| 真正不同身份 | 回到 Identity/身份设计处理 | 强行塞进一个 owner 的 alternatives |

判冲突需给出**相同/重叠条件下不能同时成立的具体断言**。不同来源使用不同参数值不自动冲突；但只有证据确认参数角色与合法范围，才能泛化。固定业务阈值、计数对象、单位或粒度不能被当成偶然参数删除。

## 2. 一个 owner、多份完整版本

一个 conflict owner 仍是一个业务身份。每个 alternative 是该身份的一份完整候选含义：名称、description、aliases、tags、properties，以及它适用的关系/消费者解释必须共同自洽。

“完整”意味着独立读一个分支就能正确理解其定义、计算或行为，不需要从另一个互斥分支借半段公式、reason、角色或适用条件。不机械求多个来源 alternatives 的笛卡尔积；也不把一个分支的 action 和另一个分支的 reason 拼成新版本。

不同类型各自保留：

| 类型 | 每个完整版本至少保留的业务含义 |
| --- | --- |
| Concept | 该版本的定义/表示角色、完整 targets 组、各组匹配表达式、必要关系组合 |
| Metric | 完整 expression/refs、操作数含义及原先承诺的范围/粒度/单位/NULL 行为；适用计算计划与该口径一致 |
| GenerationRule | 配对的 condition/action/reason、原检索信号和 intent；条件、操作数、并列/去重策略不跨版本混搭 |
| Event | 该发生类型的定义、参与角色、发生条件，以及角色属于同一发生的配对事实 |

### 例：同一“发热”身份的两套竞争定义

这是**合成的来源冲突示例**：来源 A 在相同适用范围把阈值定义为 38.0℃，来源 B 定义为 37.5℃；没有证据允许按病种、case ID 或其他条件区分。以下两块分别是完整 `NodeContent`，应保留为两个完整版本，不把阈值泛化成任意请求参数。

版本 `threshold_38`：

```json
{
  "name": "发热",
  "description": "来源 A 的完整定义：同一适用范围内，记录的摄氏体温达到 38.0 或以上判为发热。",
  "aliases": ["Fever"],
  "tags": [],
  "properties": {
    "definition": "发热定义采用 38.0℃ 阈值，不以其他版本的阈值替换。",
    "targets": [["column.Laboratory.temperature_c"]],
    "target_expressions": {"0": "Laboratory.temperature_c >= 38.0"}
  }
}
```

版本 `threshold_37_5`：

```json
{
  "name": "发热",
  "description": "来源 B 的完整定义：同一适用范围内，记录的摄氏体温达到 37.5 或以上判为发热。",
  "aliases": ["Fever"],
  "tags": [],
  "properties": {
    "definition": "发热定义采用 37.5℃ 阈值，不以其他版本的阈值替换。",
    "targets": [["column.Laboratory.temperature_c"]],
    "target_expressions": {"0": "Laboratory.temperature_c >= 37.5"}
  }
}
```

两版本 targets 相同，不代表含义相同。不能把它们作为普通 Concept 中无条件可替换的两种发热定义；也不能把 description 写成“用 37.5 或 38，按情况选择”而不说明来源实际支持的条件。

## 3. 完整解析内容与存储压缩严格隔离

| 层次 | 正确理解 |
| --- | --- |
| Agent 评估/提交语义 | 使用完整 NodeContent；比较每个受影响完整版本及适用关系 |
| 当前 graph 存储 | owner 保存公共字段，alternative 保存整个字段的 overrides；由代码压缩和解析 |
| 占位字段 | owner 的 name 可能是其 ID，某些公共 action/reason 可以为 null；这不表示完整分支缺知识 |
| 公共字段 | 字段刚好相同是压缩事实，不自动产生新的来源单元或证明各版本语义全部相同 |

Agent 不直接拼写 overrides，也不从 owner 公共占位推断“还存在一个普通版本”。当完整内容已经提供时不必重复读取；只有摘要不足以决定关系/修订时，才读取必要完整字段或 alternatives。

**语义重复与正文相同不同。** 两个版本 NodeContent 相同，但作用对象、配对角色或出边范围不同，未必是同一完整版本。当前存储层按 overrides 正文判重，不把“仅边不同”自动纳入该判重。可将来源已经明确的角色/条件准确写回现有 description 等，使完整版本差异可见；不得编造文本差别凑分支。若必须支持“正文完全相同、只靠关系区分的 alternatives”，那是待 review 的存储/编译能力变化，不在本方案中暗改。

## 4. 拟议 `node_rewrite`：统一已有身份的完整重写路径

**这是新 canonical 契约的拟议能力，尚未接入生产 compiler。** 它保持同一个已有 node_id / 业务身份，不重新做 Identity；不提供 member_ids，不创建节点 alias，不用退役 owner 再造新 ID 来回避来源约束。

| 语义变化 | 同一路径中的完整结果 | 原知识/来源去向 |
| --- | --- | --- |
| 普通 → 冲突 | 提供至少两个完整 alternatives，明确真实竞争差异与 conflict_points | 原 normal 单元完整保留在其实际支持的指定版本/所有版本中，不可 discard；新差异需实际支持 |
| 冲突 → 冲突 | 完整重写保留后的全部版本 | 每个真实旧单元恰好一个保义去向；不隐式遗漏旧分支，不把一段字段省略当作撤销知识 |
| 相容分支 → 新的综合普通内容 | 提供一份完整普通 content，保留各分支相容互补的全部知识 | 所有这些旧 alternative 单元都走 result 保留；不能仅选旧 A 后丢掉旧 B 的有效补充 |
| 选择已有分支 → 普通内容 | 将被保留旧分支的完整内容作为普通 content | 保留单元走 result；每个丢弃旧分支的具体纠正/冗余依据写入其 route.explanation；仍有依据的竞争分支不能按偏好丢弃 |

主契约中的 value 包含 node_id、完整 content **或**完整 alternatives、必要 conflict_points、common_evidence_ids、普通结果增补支持用的evidence_ids、source_routes、explanation。普通结果与冲突结果互斥；普通化也不能略去来源处置。来源单元语义见下节。

这条路径替代草案中原本分开的 conflict_rewrite / conflict_resolve，消除“只能选旧分支、不能合成新普通内容”的缺口。新建身份另走 node_create；本路径不合并多个不同 node ID。

### 与当前 compiler 的差距

当前公开 MergePatch 的 conflicts 仍要求至少两个 alternatives；旧 resolutions 只能选择已有分支；公开 IdentityIntegration 要求至少两个 member。因而不能把 `node_rewrite` 直接原样塞给旧工具，也不能让模型伪造 `member_ids=[owner]` 或串联 resolve＋node_update。

生产接入需要 controller 提供**受信任的单 owner adapter**：确认 owner 真实存在且身份不变，从固定 before 枚举该 owner 的实际 source units，将完整普通/冲突结果与真实路由送入保义编译；再检查候选关系、来源与消费者。适配只是操作翻译，不替模型选择定义、造来源或判断等价。当前代码已有旧 conflicts 的单 owner 翻译片段可作为实现参考，但这不表示新普通化路径已实现。

### 普通化不能把旧分支边无条件广播

普通结果没有旧 alternative IDs。旧分支限定边不能只机械去掉 scope 就变成普通共有关系：必须判断原条件、作用对象、依赖与消费者是否在新完整内容下仍成立。

如果普通综合内容仍通过来源支持的条件/参数区别行为，关系也须在现有表达能力内准确保留相同区别；不能因为正文合成一句话，就让原先二选一的作用对象变为“同时作用于全部对象”。无法可靠表达或消费这些条件时，应保留完整版本或报告能力缺口，而不是强行普通化。

## 5. 来源单元、证据与来源去向

### 5.1 三种对象不要混淆

| 对象 | 含义 | 不能代替什么 |
| --- | --- | --- |
| source unit | controller 从固定 before 枚举的实际知识保留单元 | 不是随意拆出来的句子片段，也不是证据 ID |
| evidence ID | 按需读取 Question/Gold SQL/evidence 的句柄，指向原始证据 | 不是节点 ID、分支 ID或 source unit ID |
| 来源/lineage | 代码管理的实际来源关联与语义后继记录 | 不由 Agent 重建内部 SourceReference、哈希或账本 |

当前实际 source unit 包括：普通成员的 `normal_content`、旧 conflict 中的每个 `alternative`，以及**仅在原 common_sources 存在时**出现的 `common`。即使旧知识没有 evidence 句柄，也不因此允许删除；完整内容与实际来源仍是保留基线。公共字段相同、公共占位、压缩后的空对象，都不会额外产生 common 或 owner/null 单元。

### 5.2 路由表达知识在哪里保留

下面描述的是既有来源语义，不为新 canonical draft 发明操作参数。controller 提供的每个真实 source unit 必须有唯一、可审计去向，不能遗漏、重复、复用其他基线的句柄或虚构来源单元。

| 保留/处置语义 | 必须成立的事实 |
| --- | --- |
| 普通结果中保留 | 原单元的完整知识在普通结果中保留 |
| 指定输出版本中保留 | 原单元的完整知识在指定完整版本中保留；只附它实际支持的版本 |
| 在所有输出版本中保留 | 原单元知识在每个完整版本均保留，不只是在某些字段中有相似文字 |
| 丢弃旧版本 | 仅在有具体证据的纠正或完整冗余依据下处置旧 alternative；不能按来源数量/当前偏好舍弃仍成立的竞争知识 |

**现行边界**：normal/common 单元不能走 discard；遇到确需撤回 accepted 普通知识的真实契约/来源冲突，必须显式报告并保持未发布，不能通过假路由偷换为“已经保留”。是否扩大可纠正的操作能力，另由主契约 review，不能靠 Prompt 暗中放开。

### 5.3 公共证据不是对所有更强断言的背书

假设公共来源仅说明“温度列记录摄氏体温”，而 A/B 分别说明 38.0/37.5 阈值：公共来源可以在两个完整版本中保留这个单位事实，**不能因此声称它支持两个阈值**。A 的阈值证据仍只附 A，B 的阈值证据仍只附 B。

若某个输入单元同时包含共同事实与分支独有断言，不能因为部分相同就把整个单元标为公共；应保留其完整知识在实际支持的分支，不能临时拆造两个来源句柄。存储层把共同字段压缩一次，也不改变该来源边界。

结构上具有 evidence ID 或完整 route，只是支持资格，不证明完整版本真实成立。当前编译器还特别区分：单填 common_evidence_ids 不自动满足任意新分支的支持资格；来源应实际落在该分支或真实公共路由中。独立 Assessment 仍须比较断言内容与其依据。

## 6. 起点分支范围：边不是分支选择器

当前边的 `properties.alternative_ids` 只表示：**该边在起点 owner 的哪些完整版本中成立**。它不决定选用哪个版本，不激活 Rule，也不表示“同时执行所有竞争 action”。

| 表示 | 当前快照含义 |
| --- | --- |
| 边不带 alternative_ids | 当前起点所有版本共有的关系；普通节点也不填分支范围 |
| 带非空、无重复的起点分支 ID 列表 | 只在这些完整版本中成立；每个 ID 必须是该起点实际存在的分支 |
| 列出当前全部分支 | 在**当前固定快照**中与无 scope 关系等价；未来新增版本时不能据此自动推导其适用性 |

以下是当前合法 `GraphEdge` 示例。完整图中起点为 `concept.fever`，包含上面的两个版本，终点是真实体温列；两条边都来自同一个 Concept，因此不违反“不同 Concept 单直接 owner”政策。

```json
[
  {
    "id": "edge.fever_38_temperature",
    "edge_type": "grounded_on",
    "from_node_id": "concept.fever",
    "to_node_id": "column.Laboratory.temperature_c",
    "properties": {
      "description": "38.0℃ 定义版本通过该摄氏体温列及其阈值匹配发热记录。",
      "alternative_ids": ["threshold_38"]
    }
  },
  {
    "id": "edge.fever_37_5_temperature",
    "edge_type": "grounded_on",
    "from_node_id": "concept.fever",
    "to_node_id": "column.Laboratory.temperature_c",
    "properties": {
      "description": "37.5℃ 定义版本通过该摄氏体温列及其阈值匹配发热记录。",
      "alternative_ids": ["threshold_37_5"]
    }
  }
]
```

合并/改写版本时，继承边仍保留原先支持的版本。旧 owner 的无 scope 边只覆盖旧完整版本，**新增 alternative 不自动继承旧来源或出边**。代码可以重定向已确认节点 ID、映射确切旧分支去向和严格重复边；无法从来源路由唯一确定的语义范围必须由关系协调判断。

两个不同邻居还未合并，不要求当前 Agent 预测身份或提前删边。未来确认身份后才重定向；属性、角色或 scope 不同的边仍不是简单重复。

## 7. 终点分支：当前没有机器可执行选择器

如果原来 `N -> T1`，而 T1 后来与另一个含义整合为 conflict owner T，那么重定向成 `N -> T` 并不能自行证明原断言适用于 T 的每个版本。

- `alternative_ids` 属于 **N 的起点版本**，不能把 T 的分支 ID 填进去表示“只指向 T 的版本 A”。
- 当前节点 Writer 默认不读全量入边，因此不能要求它自己发现全部这类问题；controller 检查目标变为/改写 conflict，保留原边及原目标完整内容，交关系 Agent 处理。
- 若确认映射前后完整目标版本没有实质变化，可以复用；未知则不能猜。相同名字/物理 targets 不足以证明所有目标版本等价。
- 关系 Agent 必须比较原目标含义、新完整版本与关系本身，说明旧断言如何保留。仅改终点 ID、把所有候选都列出来或删除原边，不是自动保义证明。
- 现有 description 能记录限制，但自然语言说明不是机器可执行的目标分支绑定。消费者若确实需要可计算的目标版本选择而当前结构不能表达，应报告能力缺口，保留未发布状态；不能私造新的终点分支字段或假装可执行。

**待 review 的能力扩展（尚未实施）**：如果要正式支持目标版本约束，需要共同定义节点/边表示、跨 owner 分支相容性、读取/检索、SQL 消费者、编译与验证、来源/lineage、checkpoint 与迁移。不能仅给 Prompt 增加一句“支持终点分支”。

## 8. 修改、合并、删除版本时要一起保留什么

| 变化 | 必须核查的语义 | 不能机械处理的情形 |
| --- | --- | --- |
| 改分支正文/公式/条件 | 完整内容与来源；原条件下行为是否仍可还原；公式的真实直接输入 | 只改文字而不核查依赖，或沿用另一分支 refs/action/reason |
| 合并等价分支 | 完整内容、每个分支携带的关系、角色/配对及消费者能力都等价或可相容保留 | 正文相同但出边不同不自动等价；合并后把原本二选一的作用对象变成“同时作用” |
| 删除旧分支 | 明确有据纠正或完整冗余；所有仍有效知识及来源有后继；仅属于确实无效分支的边可随之处置 | 仅按当前问题不用、Gold未展示、来源较少或组内正文重复就 discard |
| 增加分支 | 新版本完整、有支持，关系作用范围明确；旧公共来源不为新独有断言背书 | 默认广播旧无 scope 边、来源、标签或消费者前提 |
| 改分支 ID | 旧→新版本语义对应明确，边 scope、来源去向、元数据和消费者引用同步 | 只重命名列表字符串，遗失旧知识或借 ID 重用冒充旧版本 |
| 结果转普通内容 | 所有留下的完整知识真正相容；来源、出边范围及消费者解释在普通结果中成立；使用拟议 node_rewrite 的完整普通结果及真实来源路由；普通化关系条件需独立核查 | 选择几个字段拼成没有任何来源承诺的新定义；把仍有依据的竞争分支偷偷丢掉；仅选旧分支却假装已合成新普通内容 |

**冗余必须连关系一起判断。** 旧分支正文重复，但还有需要保留的独有关系或来源时，应映射到保留结果，而不是 discard。当前代码可能随被 discard 的分支删除仅属该分支的边，因此“正文重复即可丢弃”会造成实质知识丢失。

### 消费者与 metadata 核查清单

1. 起点的出边范围是否仍只覆盖受支持版本；终点版本含义是否被扩大。
2. Metric 的 refs 与实际直接依赖是否逐版本一致；间接路径不冒充直接输入，未确认等价的操作数不被替换。
3. Rule 的作用对象、条件与完整行为是否配对；Event 的参与者/时间/对象关联是否仍属于同一发生。
4. Concept 的完整匹配组及表达式索引是否仍一致；若现有消费者保存组下标，需同步核对，不能假定重排完全无影响。
5. `properties.relation_metadata` 仍引用真实相邻、同谓词、同方向、可共同成立的边；共同字段和各分支中的版本都要检查。合并边后组缩为一条时，不留无效 ID，也不静默丢组级含义。
6. 即使外部消费者字段没变，只要它依赖的含义/版本变了，仍应评估其受影响解释。读取必要消费者是为了具体决定，不授权重审所有邻居。
7. 名称、说明、SQL 模式中出现的文字不做无差别字符串替换；代码只重映射声明的引用槽位，必要语义修改由 Agent 明确保义。

## 9. 必须读取的前提与独立候选评估

- 已给完整、与当前版本绑定的内容可直接使用；仅有邻居摘要、旧候选片段或存储占位时，不能据此判不存在/缺字段。
- 修改完整版本前，读到其完整原内容、必要关系和真实来源单元；判断终点 conflict 时还需原目标完整含义与当前版本映射。证据足够时不重复读原文，缺少决定性前提时才按 ID 读。
- 写出候选后，Assessment 比较实际编译结果，不只读 Writer 自述。检查输入知识、来源去向、每个适用版本的关系、受影响消费者及 metadata。
- 格式/来源单元校验与语义判断分开：结构 route 满足不代表事实保留；semantic explanation 看似正确也不能绕过无效引用或不支持字段。
- 已确认的纯 ID 映射、精确去重不要求模型重复创造；真正需要语义决定的关系义务必须闭合。存在 mandatory 义务时，即使 Writer no-op，也需独立 Assessment，不能靠无变化跳过。
- 未解决前保持未发布和可恢复记录。节点内容通过不等于关系协调通过，更不等于最终 merged graph 可以发布。

## 10. 现有实现边界，避免把设计意图误写成代码保证

| 边界 | 当前事实 | review 关注 |
| --- | --- | --- |
| Concept 单直接 owner | 按物理 ID 全局最多一个不同 Concept 来源，不按业务条件或分支分范围判断 | 只在 Concept binding 内作为项目政策说明；是否放宽要单独改全链路 |
| Concept grounding 的逐分支合法性 | 当前目标检查对适用分支 targets 取并集，某目标在其中一个版本出现即可通过该检查 | 业务契约仍要求边在每个适用版本成立；可能需补强逐分支验证，不把结构通过当语义通过 |
| 同正文、关系不同的分支 | 当前存储按正文 overrides 判重，不自动把关系差异算入版本身份 | 不因正文相同就删关系；需要明示已有角色差异，或单独 review 表示能力 |
| 公共来源 | 只有实际 common_sources 对应真实 common 单元；公共字段相同不产生单元 | 不以压缩表示或来源覆盖率替代断言支持 |
| 冲突普通化 | 拟议 node_rewrite 统一完整普通/冲突结果；旧 compiler 尚无完整公开对应路径 | 需要受信任的单 owner adapter 和逐关系/消费者评估；不让模型拼接冲突操作，不机械去 scope 广播 |
| 终点版本 | 没有正式机器可执行终点分支选择器 | 不误用起点 alternative_ids；需要表达能力扩展时明确提出 |
| 自然语言 scope | description 中的限制不是跨 owner 可计算条件系统 | 不声称 validator 已经证明自然语言条件互斥或所有消费者兼容 |

源码依据：[完整内容与分支 graph.py:28](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/schema/graph.py:28)、[实际来源单元 source_units.py:36](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/source_units.py:36)、[历史边范围继承 patch.py:133](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/patch.py:133)、[目标 conflict 义务 node_relations.py:170](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/capabilities/graph_merge/integration/node_relations.py:170)、[metadata relation_metadata.py:41](/Users/yichendezaizai/autoonto-nl2sql-release/src/cortex/schema/relation_metadata.py:41)。

节点整体退役须有非空真实保义后继，并以node_unit_routes覆盖固定before的每个来源单元；空after仅可用于有据错误边弃用，不能绕过分支/normal/common保留义务。

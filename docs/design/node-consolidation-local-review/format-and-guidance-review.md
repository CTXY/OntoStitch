# 字段契约与类型 guidance：已同步第三版组装顺序

**上一版仍不够清楚。** 它说明了职责，但没有完整说明“模型应填什么、代码为何拒绝、该怎样修正”。本版补齐格式契约，并按 group 类型注入详细 guidance；仍是待 review 文档，没有切换生产代码或后台 runtime。

## 1. Prompt 怎样组装

每个 group 必须有一个明确的 `node_type`。代码应先验证成员同类，再选择 guidance，不能让 Writer 自己猜类型。

| 角色 | 本轮完整 System 的组成 |
| --- | --- |
| Writer | 共用字段与整合决策 → Writer 角色/局部输入说明 → submit_patch 填写契约 → **仅本组类型 guidance（最后）** |
| Assessment | 同一份共用字段与整合决策 → Assessment 角色/局部比较说明 → Review 填写契约 → **同一份类型 guidance（最后）** |

例如 Concept group 不注入 Metric、GenerationRule、Event 的说明。工具 JSON schema 独立提供机器字段定义；自然语言契约解释 schema 无法充分说明的互斥、替换、来源覆盖与保义要求。

User 只包含任务材料和实际 `node_schema`，不再次复述整份系统契约，不注入 evidence 正文。修订仅增加当前草稿、仍有效问题与其版本绑定，不累积历史草稿全文。

## 2. 逐字段说明，而不是一句“保留内容”

| 内容 | 本版明确的做法 |
| --- | --- |
| 名称不同 | 同义名称合并为代表 name + aliases；角色/定义不同不能靠 alias 假装等价；分支独有名称不无条件广播 |
| 描述重复 | 重新组织相容事实，删重复句子，不删范围、角色或知识；description 不代替公式、targets 或 action |
| tags | 去重、相容并集与有依据的归一；不能用更宽的标签取代具体知识，也不能用标签证明行为相容 |
| Concept | definition、targets、target_expressions、mapping_type、relation_metadata 分别说明，尤其明确匹配组角色和索引重建 |
| GenerationRule | condition、action、reason、triggers、operation_intents 分别说明，明确检索信号格式及参数如何使用 |
| Metric | expr/refs 精确格式、计划字段及长度约束、口径与泛化边界，不能把参数当作节点引用 |
| Event | 明确整合的是发生类型，保留定义、角色、完整匹配与关联，不合并成含糊的“相关事件” |

你提到的 General Tags 在这里分别覆盖为两件事：普通 `tags` 如何合并，以及 GenerationRule 的行为如何参数化。两者都需要明确，但标签归并不能替代生成行为的泛化。

## 3. alternatives 不只是“有冲突就保留”

先把所有输入普通内容和旧 alternatives 当作完整版本比较，再决定输出，不照抄原分支数量：

1. 完全等价版本：统一内容与有意义的别名/标签，归并来源；不保留纯措辞冲突。
2. 相容互补版本：合成一个完整版本，保留各自角色、条件及 relations。
3. 可安全泛化版本：在现有字段表达共同含义和参数绑定；代入原输入后能还原原行为。
4. 来源明确区分的条件版本：条件事前可识别、字段和消费者能表达时，组织成完整条件化内容。
5. 同一范围内不兼容版本：输出至少两个完整 alternatives，真实 conflict_points、来源路由和出边范围。
6. 兼容性还不清楚：读取对应 evidence；仍不足则报告具体缺失前提，不能把不确定自动认定为语义冲突。

输出 alternative 的 `content` 必须完整填写 name、description、aliases、tags、properties。不能输出 `overrides`，不能只给差异字段；代码会把完整内容压缩成存储表示。来源 unit 对应哪一个分支、哪些分支或所有分支必须显式说明。

**有些字段相同，不等于可以把整个来源单元路由为 common。** 比如两份发热定义都指向温度列，但阈值不同，完整输入单元各自属于对应分支；不能因为 targets 相同就广播该来源。真正共享的是字段值，是否存在公共来源单元由输入 manifest 决定，不由模型重建。

## 4. generalize 的具体标准

泛化不只是把常量换成 `<参数>`，也不只是缩短文字。必须保留：共同决策、参数角色、取值条件、绑定来源、所有非参数语义，以及用原参数还原原输入行为的能力。

| 输入差异 | 可能的正确处理 | 为什么 |
| --- | --- | --- |
| 同义中文/英文名称 | 代表名称 + aliases | 只有表述差异，不需要冲突 |
| 学校的代码表示与名称表示 | 相容角色明确时保留两个独立 targets group | 可共存，但不能据此假定可互换计数或现实唯一性 |
| 请求前 3 / 前 5，排序、粒度、并列策略相同 | 同一 Rule 中明确 K 来自请求 | K 是偶然请求值，原行为可还原 |
| 发热固定阈值 37.5 / 38.0 | 若同一范围内确实不兼容，保留完整 Concept alternatives | 固定业务定义不是任意请求参数 |
| COUNT(*) / COUNT(DISTINCT patient_id) | 确认粒度和唯一性，不能简单参数化抹平 | 计数对象与去重语义可能不同 |
| RANK / DENSE_RANK / LIMIT | 明确来源条件与并列策略，或保留竞争版本 | 结果集合不同，不是可忽略的实现细节 |
| 相同 Concept targets，不同匹配条件 | 连同 target_expressions 比较 | 仅对 ID 列表求并集可能丢掉条件 |

参数必须能由当前类型表达。GenerationRule 的自然语言 condition/action 可以声明 K 的角色与替换方式；Metric `${key}` 只绑定节点，不能拿来绑定任意请求数值；Concept 没有通用请求上下文选择器，不能用一句“按上下文选 target”补出不存在的执行能力。

## 5. 已对照代码的关键校验：向模型明确什么

下表区分“现有代码已校验”与“局部方案需要新增校验”。不把文档要求冒充成已经部署的约束。

| 约束 | 本版告诉模型什么 | 现有依据/状态 |
| --- | --- | --- |
| 未知字段与错误包装 | 直接提交公开字段；不加 patch/result 包装，不填来源对象 | Pydantic Model / NodeContent 的 extra=forbid；调用预检 |
| 成功与阻塞 | success 无 failure 或 JSON null；阻塞仅 failure + explanation，不能字符串 null | RelationPatch.failure、BatchResult.concrete_failure；互斥操作限制属于拟议窄 schema |
| 完整 group | owner 精确、成员无重复且含 owner、至少 2 个、计划集合完整 | IdentityIntegration、_integrate |
| 普通/冲突互斥 | content 与非空 alternatives 二选一；冲突至少两个完整分支和 conflict_points | IdentityIntegration.complete_result |
| NodeContent 必填 | name、description、aliases、tags、properties 都要填；description 可 null，其他语义须保留 | NodeContent；内容保义由 Assessment 判断 |
| 结果完整替换 | content 是完整结果；edge_updates.properties 按键更新；整个草稿每次完整替换 | _integrate、submit_patch、任务草稿协议 |
| 每个来源单元一次 | 复制真实 unit_id；无 missing / extra / duplicate | validate_source_routes |
| 路由形态 | 普通 result；冲突 common/alternatives；合法旧分支可 discard；其他 kind 不带 alternative_ids | SourceDestination、validate_source_routes |
| normal/common 不丢弃 | 不能通过 discard 绕过保义 | validate_source_routes；语义仍由 Assessment 判断 |
| 分支有效性 | 非空唯一 ID、完整不同版本、有来源支持 | _integrate、GraphManager.compact_conflict、NodeMergeConflict |
| conflict_points | ID 唯一、至少两个 choices、引用现存输出分支、实际区分至少两个分支 | ConflictPoint、NodeMergeConflict |
| Concept 匹配 | 二维非空匹配组、组内去重、组+表达式不重复；表达式下标有效 | validate_matching_targets |
| 表达式物理引用 | 使用实际 SQL 名称；不把组外列混入表达式 | validate_target_expression_columns；部分动态 SQL 情况仍需语义评估 |
| Rule 字段与 trigger | reason/operation_intents/triggers 的准确拼写、嵌套键与非空检索信号 | cortex_root.json、validate_semantic_properties、validate_rule_triggers |
| Metric 格式 | expr/refs 键、占位符与 refs 集合一致；非完整 SELECT、计划数量/长度限制 | validate_semantic_properties、validate_metric_calculation |
| 边角色和条件 | 放在 description，不创建 role/condition/qualifier 字段 | validate_graph_space |
| 边分支作用域 | 使用起点实际 alternatives，非空、无重复；不是终点版本 | validate_graph_space |
| metadata | 固定字段、至少两条相邻同谓词同方向边、作用域可共存 | validate_metadata_items、validate_relation_metadata |
| Review 一致性 | accept 无问题；其他有问题；revise/revisit_identity 每项有 basis；真实 ID 与路径 | Review.coherent、validate_review |
| 一轮唯一结果 | 结果不能与读取或第二个提交混用 | 现有任务调用预检 |
| 仅一个 group、仅出边 | 本轮不能写外部节点/入边/新谓词 | **拟议局部 packet、窄工具 schema 与 scope 校验，尚未接入** |
| evidence 只读本任务 IDs | 不遍历全库来源，不使用未授权 ID | **需要新增任务白名单；当前通用 reader 不等同于该边界** |
| 稳定出边句柄 | 引用输入 ID，controller 在机械合并后解析别名 | **需要补齐 adapter 与歧义反馈，不能只靠 prompt** |

这个表覆盖本局部提案涉及的关键前置约束；不是声称整个 graph 系统所有全局规则都已复制进模型上下文。全局约束应由 controller 在调用前转为本任务的必要约束，或在编译后提供可行动的具体反馈，不把全部全图规范堆进每个 group。

## 6. 发现的契约问题，需要代码一同解决

### 6.1 target schema 与旧 guidance 的口径并不完全一致

root 的 Concept targets 描述允许 Table/Column/DimensionValue；extraction 的 Concept guide 又明确不允许 Table。它们属于不同阶段，但若不区分地拼进 merge prompt，会产生冲突。

本版不把另一阶段的限制硬贴进 merge。当前局部 packet 应提供**实际 node_schema 和端点约束**；同一份约束用于生成工具说明与代码验证。历史输入不符合当前约束时，应显式迁移或报告，不能要求模型静默删除。

### 6.2 参数化规则只有自然语言语义支持

GenerationRule 允许在 condition/action 中描述 K 等参数，但当前 schema 没有统一机器可执行的参数绑定对象。因此本版要求写清楚含义和替换方式，不新增 parameters 字段，并明确这不是新的 SQL 模板执行器。

如果未来需要严格的类型化参数、取值域和自动实例化，需要另外修改 schema、消费者与验证器；不能仅在 prompt 中宣布已经支持。

### 6.3 出边 ID 可能在节点整合之后被机械去重

编译顺序是先节点整合/ID 重定向/边去重，再执行 RelationPatch。输入里看到的一条原边可能此时已被别名合并。Writer 没有整图查询权限后，不能要求它猜 survivor ID。

应由 controller/adapter 把局部输出的旧边句柄映射到确定性阶段的当前边；若多个操作映射后冲突，返回具体别名映射与操作冲突，而不是“未知 edge ID”让模型反复重试。初始输入也可提供确定部分的映射；依赖当前分支路由的部分必须在编译后绑定。

### 6.4 错误反馈不能只报告“invalid patch”

每个拒绝应携带：稳定 code、具体 path、相关 owner/对象 ID、expected/actual，以及适用的 missing/extra/duplicates、绑定的 draft/candidate ID。例：

```json
{
  "code": "source_routes_mismatch",
  "path": "integrate_groups[0].source_routes",
  "owner_id": "concept-A",
  "missing": ["source::actual-missing-unit"],
  "extra": [],
  "duplicates": [],
  "required_action": "补齐该真实来源单元的一条合法路由；不要删除其他仍有效路由。"
}
```

这是期望的反馈形态示例，不声称当前每一个错误路径都已输出这些字段。已有结构化错误应保留完整内容；剩余 ValueError 类路径要逐步补齐。重试必须有可执行信息，不能只有“再试一次”。

### 6.5 Prompt 与校验应来自同一份契约

后续实现应把字段 schema 与关键跨字段约束集中登记：从同一份定义生成工具字段、供模型阅读的填写表，以及编译错误的 expected/action。类型 guidance 负责语义规则与正反例，不重复抄写可能漂移的字段枚举。

`node_schema` 是实际执行标准；类型指导的示例必须经过同一验证器。若两者冲突，controller 在调用前阻塞；不能把矛盾留给模型自行仲裁。

## 7. 本版材料与验证范围

- 共用的字段整合原则，以及 Concept、GenerationRule、Metric、Event 四份类型 guidance。
- 两个角色各自的输出填写契约，分别说明模型应填什么、哪些属于完整替换。
- 每类 group 的 Writer / Assessment **完整拼接 System**，在 HTML 中按类型单独展开，不让复阅者自己猜模块拼接后的样子。
- 普通合并、完整冲突分支、Rule 参数化、阻塞结果和 Assessment 的公开格式示例。
- 三份完整填写的 Concept 教学 JSON：Writer User、实际 submit_patch、Assessment User。局部节点、边与来源句柄均经过代码构造和验证，不只提供文字占位模板。
- 对示例调用现有 schema、语义约束和局部整合函数验证；另以典型错误示例确认相应代码会拒绝。验证结果不等于真实模型调用或两库 merge 已通过。

本次保持文档 review 范围，生产 Prompt、工具注册、compiler、checkpoint 与后台 runtime 均未因本版修改。

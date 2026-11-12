# Case47：新 ontology 设计下的实跑 review

> 历史实验记录：对应旧测试产物已于 2026-11-04 按用户要求删除；保留本文作为设计与实验历史。当前结果见 [v50 匹配与关系要求](ontology-matching-and-relations-v50.md)及新版 review。

日期：2026-11-04。单例 BIRD california_schools case47，extraction training；语义规则版本 `database_ontology_assets_v49_semantic_boundaries`。从全新语义工作区开始，不载入旧语义图或旧对话。模型为 deepseek-flash，Direct 后进行了两次基于质量反馈的 Repair。

## Review 入口

- [汇总、逐节点/逐边检查和逐轮反馈](http://127.0.0.1:8769/index.html)
- [完整交互图](http://127.0.0.1:8769/case47_r3/graph.html)
- [业务交互图](http://127.0.0.1:8769/case47_r3/business_graph.html)：保存快照的展示投影，包含全部语义节点、匹配/边端点和必要物理归属，不改写源图。
- 原始证据：`output/ontology_v47_case47_review/final_review_result.json`、`review/case47_r3/assessment.json`、`review/case47_r3/graph.json`。

确切接受 revision：`0a40abb9b7174ebcb2e6cf188268fc6a`。完整图 99 节点、104 边；业务视图 12 节点、17 边。包含 5 Concept、11 条语义出边，全部有具体 description；无 Metric、Event 或 GenerationRule。结果仍待用户 review。

## 1 生成与修订过程

Direct 初稿创建了请求月均 Metric，以及将 district 标签解析为 DOC 值的 GenerationRule。初稿 SQL 连续两次正确，但更新后的 Assessment 拒绝：

- 月均节点可以拆为分别匹配的学校/开设日期含义、计数及月均操作；候选自行编写的定义或公式不能证明 NL 原子性。
- School 不能把混合目录的所有行都声明为实体学校；记录主键、名称和实际学校群体必须区分。
- 值匹配规则对可用字段的排他声明与 metadata 不符。

第一次 Repair 删除月均 Metric并修正 School 和规则作用域。第二轮 SQL 再次正确，但 Assessment 继续拒绝：

- 规则仍重复现有 Concept→DOC 值绑定，没有额外的有证据生成决策。
- County 列为非空，边不应暗示当前记录允许缺失 County。

第二次 Repair 删除重复规则、修正 County 描述，保留业务匹配及查询计算。最终 SQL 连续两次正确，最终 18 个 Assessment 单元的全部 quality_checks 为 valid；Pipeline 完成 `stable_correct`。额外的局部复核针对早期候选，仅作诊断，未代替最终整图 Assessment。

前两次诊断运行在修订通用指导时停止并保留，明确标为 interrupted；最终展示只对应 case47_r3。没有人工编辑最终图，也没有按 case ID、表列名或业务字面值编写确定性生成规则。

## 2 最终节点与匹配

| Concept | targets 中的一项匹配 | 含义及边界 |
| --- | --- | --- |
| school | `[table.schools, column.schools.CDSCode, column.schools.School]` | Table 提供记录来源，CDSCode 提供记录身份，School 提供名称/实际计数值。描述明确目录含非学校记录，不能默认每行都是学校；当前群体由请求限制确定 |
| school opening date | `[column.schools.OpenDate]` | 记录的开设日期；没有记录日期不能推出学校从未开设 |
| school county | `[column.schools.County]` | 记录的县名称，按文本匹配；当前元数据为非空 |
| district ownership code | `[column.schools.DOC]` | District Ownership Code 编码维度；代码意义依据 metadata/evidence，不凭缩写猜测 |
| elementary school district | `[dimension_value.schools.DOC…52]` | Evidence 支持的完整类别标签对应 DOC 值 '52'；节点指向 governed Value，不再同时直接绑定 DOC 列来重复表达等值条件 |

最后一行省略的 Value ID 可在交互图、原始 JSON 中查看；它是 DOC 字段作用域中的真实文本值，不是裸字符串。

没有为了增加 Event 或 Rule 数量而增加节点。该 case 的计数与月均操作没有被包装成 Metric；这不等于禁用含聚合的业务公式或所有时间归一化规则，后者仍按 NL 最小化及动作决策准入评估。

## 3 关系与组织

- School 使用三条 `grounded_on` 解释来源 Table、记录 identifier 和名称/计数值；其余四个 Concept 各有一条正确的物理绑定。
- School 使用三条按需声明的 `has_attribute` 连接 opening date、county 和 district ownership code。
- Elementary school district 使用 `category_of` 连接 district ownership code；该关系表示类别意义到编码维度，不是属性与实体之间的 is_a。
- DOC 值 '52' 的 `value_of` 指向 DOC 列；Column 的 `belongs_to` 属于物理目录关系。

`has_attribute`、`category_of` 的契约随图保存。没有无契约业务谓词，没有通过通用 refs 推断业务边；没有旧的 opened-school / event / date 多路径重复表达。这里接受此种有证据组织，不将全图节点数或成环与否作为独立优化目标。

## 4 最终 SQL

```sql
SELECT CAST(COUNT("School") AS REAL) / 12 AS monthly_average_schools
FROM "schools"
WHERE "County" = 'Alameda'
  AND "DOC" = '52'
  AND "OpenDate" >= '1980-01-01'
  AND "OpenDate" < '1981-01-01';
```

保留 COUNT(School) 的非 DISTINCT 与 NULL 语义、分数除法和十二个月分母。日期区间是本 case 的实际生成写法；没有将其固定为所有数据库的通用时间政策。

## 5 修改与验证

核心输入、工具、prompt、registry 的改动见 [Agent 输入审计](ontology-agent-input-review-v47.md)。Assessment 按需获得一次完整精简 registry，默认物理输入明确标注作用域；相同 description/definition 在输入视图精确去重，不同声明及原图完整保留。Learning 六个既有工具足够，新评估没有增加工具或复杂推理模块。

最终本地全量回归：877 passed、2 个既有 Pydantic 弃用 warnings，修改文件 Ruff 通过。真实案例执行的是 v49 语义指导；后续精确视图去重由回归验证，未将其冒充额外一轮模型产图。源工作区、progress、模型交互、失败候选及最终评估均保留。

## 6 用户 review 的具体关注点

1. School 的联合匹配把记录来源、identifier 和 name 的职责写在 description 中；可 review 是否更偏好独立的 School name Concept。当前并未将名称当作唯一身份。
2. 一些边仍带有观察到的行数/缺值数量，作为当前快照的证据性说明；这些不是普遍业务常量。后续可讨论是否统一将这种细节移入 evidence，使关系描述更紧凑。

以上是供用户选择表示和描述粒度的 review 点，不把模型通过当作用户已认可设计，也不据单例推断跨 case merge 或所有 Metric 类型均已验证。

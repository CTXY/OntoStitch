# Ontology 匹配与关系描述要求（v50）

日期：2026-11-04。本文记录 identifier/name/Table 与关系 constraint 的新决定及实施边界，完整规范见 ontology-graph-design.md。

## 1 匹配的职责

新的 extraction 中，Concept targets 和 Concept grounded_on 只允许 Column 或 governed DimensionValue；不直接匹配 Table。表的记录来源、主键、唯一键、行含义及覆盖范围由静态 metadata 说明。没有提供业务粒度时保持未知，不由主键猜测每行都是什么对象，也不在训练中更新 metadata。

每个外层 target item 独立匹配同一个 Concept 含义。内层多个字段共同完成一个不可分割的匹配，例如复合标识或联合条件。字段同表、描述同一对象，都不是联合匹配的理由。v51 补充修正：identifier 和 name 可以是同一对象 Concept 的不同匹配表示，分别放入外层 item 并说明角色；不强制拆成不同 Concept，不视为联合标识或自动可互换。命名优先采用 Question 的业务术语；School 示例见完整规范 §3.2。

一个 Concept 可以有多个已核实的身份匹配，例如不同表中指向同一种对象的 identifier。必须明确来源作用域；不自动授权 JOIN、源替换或断言相同记录粒度。现有直接绑定唯一性、完整条件及最小化要求继续适用。

## 2 关系说明及约束

每条业务边的 description 表达“具体语义联系＋必要且有证据的约束”，通常一到两句话，不设置机械字数上限。约束可以涉及基数、唯一性、可选性、条件或推断限制；没有必要时不强造。

- identifier：说明标识什么、在哪个记录/业务对象/时间范围中成立。记录主键不自动证明现实对象在所有时期只有一个代码。
- 日期或状态：如证据支持缺失，说明允许未记录以及不能由缺失推出什么。整表存在 NULL 不足以证明特定业务群体中存在 NULL，约束须对应正确群体。
- 角色：用谓词及 description 说明具体联系，不重复整段节点定义。

字段类型、值列表、profiling 行数/缺值数、探索 SQL、证明过程放在 metadata/evidence。边保留得到支持的语义结论，不复制统计摘要，也不追加无关免责声明。继续使用 properties.description，不增加 Constraint 节点、qualifier 或约束字段。

## 3 实施

- ConceptAuthoringProperties 提供 Column/Value-only JSON schema；历史 ConceptProperties 保留读取已有 Table targets 的能力。
- Learning 的写入边界在保存前检查新 upsert、冲突分支/解决后的定义及显式 Concept→Table 绑定，拒绝时不保存部分图。历史读取和受控 revert 不充当新写许可。
- 相应类型 guide、共享 contract、relation guide 与 Assessment rubric 已统一；身份/name 的职责及联合匹配合理性由语义评估检查，不按字段名编写规则。
- Learning 默认上下文按需提供 Table metadata 的主键/唯一键和已有说明；完整 metadata 仍通过现有工具读取。Assessment 也接收这些既有表级事实，未增加探索或编辑工具。
- Assessment 的 grounding 检查匹配职责与分组；scope/support 检查关系语义、约束证据、范围及描述中的冗余 metadata。

## 4 验证与产物清理

全量回归：881 passed、1 个既有 Pydantic 弃用 warning。新增检查覆盖旧图可读、新 Table targets 拒绝、Concept→Table 新边拒绝、冲突分支不能绕过限制，以及拒绝时 revision 不变；没有业务字面值判定或 case-specific 生成代码。

本轮只重新运行 BIRD case47，从空白语义图开始。运行配置、日志、快照及评估保存在 output/ontology_v50_case47_review。旧 ontology/extraction review 产物在新版可用后删除；清理限于明确列出的旧测试目录，不删除 src、tests、docs、datasets 或其他项目输出。最终清单记录在新输出目录的 cleanup_manifest.json。

## 5 本轮 case47 结果

最终 revision：`1384f03a54f64897a4d043705df7590c`。从空白语义图完成 Direct 和一次质量 Repair；连续两次 SQL 正确，最终 15 个 Assessment 单元全 valid。

- [Review 汇总与逐轮反馈](http://127.0.0.1:8769/index.html)
- [完整图](http://127.0.0.1:8769/case47_r1/graph.html)：100 节点、101 边，含完整物理目录。
- [业务图](http://127.0.0.1:8769/case47_r1/business_graph.html)：12 节点、13 边，为实际快照的展示投影。
- [旧产物清理清单](http://127.0.0.1:8769/cleanup_manifest.json)

语义节点为 5 Concept＋1 GenerationRule，8 条语义出边。Concept 分别匹配：

| 当前节点名称 | 唯一匹配项 |
| --- | --- |
| school name | `[column.schools.School]` |
| school opening date | `[column.schools.OpenDate]` |
| school county | `[column.schools.County]` |
| district ownership code | `[column.schools.DOC]` |
| elementary school district ownership designation | `[DOC 的 governed Value '52']` |

本例没有强制创建 School identifier Concept；所需名称含义独立表示，Table 主键信息仍在 metadata。若后续 case 需要独立对象身份匹配，可以用 identifier 字段锚定 Concept；不能把 name、identifier 和 Table 混成一个 item。本轮也不以创建 School 主体、Event 或完整业务知识覆盖作为硬性指标。

生成规则表达日历年内的月均计数，操作数与 opening-date Concept 通过 applies_to 相连。首轮评估拒绝“每个符合条件的记录贡献一次”的表述，因为 COUNT(School) 实际排除 NULL 名称；Repair 同步动作与边描述，明确计数非空名称值，并保留固定十二个月分母。没有把这一请求计算包装成 Metric。

最终 SQL：

```sql
SELECT CAST(COUNT("School") AS REAL) / 12 AS monthly_average_schools_opened
FROM schools
WHERE "County" = 'Alameda'
  AND "DOC" = '52'
  AND strftime('%Y', "OpenDate") = '1980';
```

内部评估通过仍需用户 review，尤其是该规则动作的抽象程度及实现措辞；模型通过不代表所有语义等价 SQL 实现已被穷尽核查。

旧产物已清理：7 个目录、7,095 个文件，累计文件大小约 712.7 MiB；停止了指向旧 review 的服务器，并用同一端口展示新版。仅保留本轮 ontology 输出目录；其他项目输出和设计/源代码/数据集均保留。清理操作不修改新图或伪造旧实验成功记录。

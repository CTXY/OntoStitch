# Case47：School 表示角色复验

日期：2026-11-04。最终图 revision 为 `73050adb06a24283ab36944ba94999f2`。这是通过正常 Learning Agent、SQL 重放及整图 Assessment 验证的 review 提案，等待用户 review；不是对原训练验收记录的覆盖，也没有执行 merge。

## Review 入口

- [汇总及逐轮评估](http://127.0.0.1:8769/index.html)
- [完整图](http://127.0.0.1:8769/case47_r1/graph.html)：102 节点、106 边，包含物理目录。
- [业务图](http://127.0.0.1:8769/case47_r1/business_graph.html)：15 节点、19 边，为保存快照的展示投影。

## 1 最终组织

5 个 Concept：School、District ownership code、Elementary School District、County、School opening date。另有 School opening Event 和两条 GenerationRule，没有 Metric，也没有被 SQL 名称字段替代出来的 School name Concept。

```text
School
  grounded_on → schools.CDSCode  [当前来源的记录标识]
  grounded_on → schools.School   [名称表示及本例计数操作数]
  school_recorded_county → County
  school_recorded_ownership_code → District ownership code

School opening
  school_opening_participant → School
  school_opening_occurrence_date → School opening date

Elementary School District
  category_of → District ownership code
```

School 的匹配项：

```json
"targets": [
  ["column.schools.CDSCode"],
  ["column.schools.School"]
]
```

这是同一业务对象的两个表示角色，不是一个联合标识，也不授权互换操作数。identifier 的唯一性限定在来源记录范围，不推断现实学校在所有时期只有一个代码。名称可能缺失或不唯一。Table 的作用保留在物理目录/metadata 中，没有 Concept→Table 绑定。

Event 通过 School 与记录日期得到结构支持；不声称学校记录存在即可确定事件实例数，也不把记录的 County 当作历史开设地点。所有关系均为 Agent 显式保存并带具体 description，业务谓词契约随图保存。

两条生成行为为：

1. **Count schools by school name**：采用被证据指定的名称表示，保留 COUNT(School) 的非 DISTINCT 与 NULL 语义；identifier 仍用于身份支持。
2. **Monthly average over calendar months**：以日历年内计数为输入，采用固定十二个月时间基进行月均归一化，而非仅按有活动的月份平均。

动作、适用条件、标签及局部 SQL pattern 完整保留在交互图中，供用户进一步 review 抽象与复用范围。

## 2 自动运行中发现并修复的问题

v52 从全新工作区 Direct 抽取时已正确保留 School 与两个独立 targets，但仍生成了可拆分的请求月均 Metric。首轮模型 Assessment 错误放行该 Metric；原始接受与模型响应均保留，没有事后改写为拒绝。

只读质量复核指出两点：该节点仍是 School/开设含义＋计数＋月均操作的组合；其表达式把 School 对象绑定到名为 school_opening_count 的变量，却将实际 COUNT 只放在 plan 中。变量名不能改变被绑定对象的含义或代替计算。通用指导补充了跨领域反例及公式完整性要求，Assessment 先检查 Metric/Rule，再检查其他节点与关系；没有删除、隐藏评估单元或编写按 case 判断的生产规则。

正常 Repair 删除 Metric 及其相关边，保留 School 和受支持的业务结构。重放一度生成 COUNT(DISTINCT CDSCode)，虽然当前 case 结果正确，但不满足知识中明确的名称操作数、NULL 和 DISTINCT 语义。Assessment 将 School 应用判为 partially_applied，并驱动下一次正常 Repair 明确计数与时间基两项行为。

SQL 生成模板随后同步修正：按匹配角色选择 target，不把所有外层 item 当作可互换来源，不默认取主键、DISTINCT 或第一项。对同一确切候选，重新构建验证上下文，用最终模板执行 SQL 重放和完整 Assessment；没有复用旧模板的验证作为最终证明。

## 3 评估与执行的边界

Concept grounded_on 表示绑定由代码根据实际源节点类型分配 semantic 应用要求，仍需通过端点、角色、约束及证据检查；这不是 Agent 可填写的豁免标签。Concept 节点本身继续要求请求相关含义在 SQL 中被应用，不能因存在于上下文就通过。

最终评估中：School 节点 valid/applied；identifier/name 两条表示绑定均 valid/not_applicable。它们被语义支持，不要求两列都出现在 SQL。Metric 物理操作数、计算公式和生成动作仍按各自 SQL 应用要求检查。

## 4 最终验证

- 相同 revision 在最终 SQL 模板下连续 2 次结果正确。
- 最终 22 个 Assessment 单元全部 quality valid，应用/分组要求通过。
- 全量回归 883 passed；最终 SQL 模板另经过 YAML 解析及真实 SQL/Assessment 复验。
- 专项回归 111 passed，修改 Python 文件 Ruff 通过。
- 最终提案由 Learning Agent 写入；展示投影不改写源图。原始 v52 接受快照、模型误判及质量反馈保存在本轮提案的 source/evidence 记录中。

最终 SQL：

```sql
SELECT CAST(COUNT(School) AS REAL) / 12
FROM schools
WHERE County = 'Alameda'
  AND DOC = '52'
  AND strftime('%Y', OpenDate) = '1980';
```

不因 identifier 表示存在而加入 identifier 投影、额外 JOIN 或 DISTINCT。SQL 正确与语义质量分别记录；本例通过不代表对所有数据库、所有 Metric 或全局 merge 的验证。

已清理被替代的 v50/v52 顶层测试输出；需要解释本轮修订的源快照、证据和源 case result 副本保留在当前输出中。清单见 `output/ontology_v53_case47_review/cleanup_manifest.json`。

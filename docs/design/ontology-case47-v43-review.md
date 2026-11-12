# Case47：统一 operation registry 后的实跑 review

> 历史实验记录：对应旧测试产物已于 2026-11-04 按用户要求删除；保留本文作为设计与实验历史。当前结果见 [v50 匹配与关系要求](ontology-matching-and-relations-v50.md)及新版 review。

本次从全新工作区运行 BIRD california_schools case47；未载入旧语义图或旧 Agent 对话。模型配置为 deepseek-flash，最多三个训练轮次，SQL 稳定性验证要求连续两次正确。只运行 case47，不扩大数据集实验。

## 实现调整

- `llm/generation_rule_taxonomy.py`：保留 temporal_alignment 等所有 intent；取消 Direct/Repair 专属 intent 列表。意图分类不等于创建许可。
- `learning/inputs.py`、`prompt_rendering.py`、`tools.py`：guide 和 taxonomy 入口返回同一份 registry；删除 direct_rule_registry 及 Direct joinability 写入拦截。正常 schema、关系和语义质量校验保留。
- `llm/prompts/asset_contract.py`、Direct/GenerationRule/Assessment Markdown：统一规则准入与最小 action，允许关联业务名称，泛化偶然参数；需要时提供有解释的局部 SQL pattern。新增 intent 必须说明含义、registry 缺口与有依据的实现差异。
- `training/quality_contract.py`：decision_value 检查有证据的实现决策；abstraction 不再禁止业务名词；condition_action 检查可执行性和局部 pattern 一致性。
- Concept guide：类别匹配指向 governed Value；不通过重复 Column/filter 边再表达同一个等值匹配。完整业务类别不机械拆词。
- 通用交互图 renderer：节点详情中的关系属性不再截断为 260 字符，保留完整 description。

未引入 case-specific 生成代码、SQL 改写或验收绕过。业务语义图由实际 LearningAgent 写入；实验输入包含固定 case 是评测范围选择，不是生产生成逻辑。

## 验证与产物

运行：`output/ontology_two_case_review/case47_r6/`。原始 progress、日志、快照、Agent 交互与评估证据均保留。

完整交互图、最终评估与初稿差异见下文。Pipeline 自动验收和用户 review 分开记录。

## 已观察到的修复过程

Direct 初稿连续两次 SQL replay 正确，但首次 Assessment 拒绝将请求月均包装为 Metric，并拒绝把普通日期字段/年份匹配当作独立规则。第一次 Repair 删除这些节点，新增固定全年时间基归一化 Rule，解释现有 registry 缺口，保留 count 的精确操作数。

第二次 SQL replay 也连续两次正确。第二次 Assessment 进一步拒绝混合目录的学校身份假设、重复 Concept→Value 的代码匹配 Rule、计数理由中的名称/身份混淆；这些结论与独立只读 subagent 的主要发现一致。Pipeline 因此进入下一轮 Repair。不能把任何前一轮 SQL 成功称为全图语义通过。

## Registry 与回归检查

实际 Direct `inspect_graph(mode=guide,node_type=generation_rule)` 的保存响应包含完整 `operation_registry`，其中有 `temporal_alignment`，且没有 `direct_rule_registry`。因此本次使用统一 registry 有工具调用证据，不依赖历史会话记忆。

全量回归：846 passed、2 条既有 Pydantic 弃用警告，日志 `output/ontology_two_case_review/tests_v43_full_final.log`。修改文件 Ruff 通过。交互图与导出相关额外回归 10 passed。

独立复核指出的 predicate/condition 缺口在 `case47_r7_feedback.json` 保存；该文件是明确标注的质量反馈，不伪装成 SQL 错误证据。真实 LearningAgent 在独立复制工作区中生成修订提案，随后执行连续 SQL replay 和全图 Assessment；原 `case47_r6` 快照及正式结果不被改写。

## 最终候选的内容

候选 `36397aef7b8e4174a2bfb3a1a226047f` 保存于 `case47_r8/ontology/`。包含 7 个 Concept、1 个 Event、2 个 GenerationRule、0 个 Metric。完整快照另含数据库物理目录，总计 104 个节点、110 条边。

| 节点 | 含义与匹配 |
| --- | --- |
| school | 被目录记录描述的学校；Table 为数据来源，不断言所有目录行都是一所学校 |
| school name | schools.School，保留 nullable 名称语义，不把名称缺失当身份不存在 |
| school county | schools.County；记录属性不自动证明事件历史地点 |
| district ownership code | schools.DOC，行政管理类别维度 |
| elementary school district | DOC 的 governed Value '52'，不是 SOC 的小学类别；category_of 指向上项 |
| school opening date | schools.OpenDate，记录的开设日期 |
| opened school | 当前表示有记录开设日期的学校记录群体；联合匹配条件为 OpenDate IS NOT NULL，不能推断缺日期即从未开设 |
| school opening | 开设事件类型，通过学校参与对象和记录日期关联锚定，不推断完整重开历史或每行一个事件 |

两条生成规则分别管理以下决策：

1. 年内开校计数的操作数：在已说明的学校开设月均计算场景中使用 `COUNT(School)`，保留非 DISTINCT 和非 NULL 名称计数；不泛化成所有学校计数都必须用 School，也不负责除法。
2. 全年计数转月均：消费独立取得的 `annual_count`，局部模式为 `CAST(annual_count AS REAL) / 12`。固定全年月份分母，不用观测到开校的月份数。`period_average_normalization` 是规则解释的新 intent，reason 说明已有 registry 不覆盖该归一化决策；没有自动写入全局 registry。

两条规则保留 Question 检索短语，SQL columns/operations 均清空。最后一次 Agent 编辑只改变这两个 Rule 的 SQL triggers，全部边和其余节点与前轮一致。

业务关系包含 school 的名称/管理类别/县属性、opened school 的 is_a 与日期属性、district 类别的 category_of、事件的 opens 与 recorded_at_date，以及三条 Rule applies_to。每条边均保留 description，物理匹配由实际 grounded_on/value_of 表达，没有再次用 Column/filter 边重复 DOC='52'。

## 交互图与复核入口

- 完整图：`http://127.0.0.1:8768/case47_r8/graph.html`，保留整个已保存物理目录和语义图。
- 业务视图：`http://127.0.0.1:8768/case47_r8/business_graph.html`，保留全部 10 个语义节点及其匹配端点和物理归属，共 16 节点、22 边；这是明确标注的展示投影，不改写原始图。
- 原始候选：`output/ontology_two_case_review/case47_r8/ontology/36397aef7b8e4174a2bfb3a1a226047f.json`。
- 独立 subagent 复核：`output/ontology_two_case_review/case47_r8_independent_review.json`，针对同一 revision 为 pass，无阻断语义缺陷；不将此结论冒充 SQL/Assessment 验收。

最终 replay/Assessment 已完成，结果见下节。未执行全局 merge，用户 review 尚未通过。

## 最终验证结论

- 原始全新训练 `case47_r6` 正常完成，Pipeline `stable_correct`，passed=1、failed=0；保留全部 Direct、三次 Repair 与 Assessment 历史。
- 展示 `case47_r8` 的确切候选 revision 为 `36397aef7b8e4174a2bfb3a1a226047f`。它是额外质量复核后的提案，`review_only=true`、`formal_case_acceptance=false`，没有改写 r6 的正式接受记录。
- 此候选的稳定 SQL replay 为 `stable_correct`，连续 2 次结果正确。
- 此候选全图 Assessment 的 28 个评估单元均为 valid，quality_acceptable=true；没有以 SQL 正确覆盖质量失败。
- 独立 subagent 对同一 revision 的语义复核为 pass，无阻断缺陷。
- 全量测试 846 passed，修改文件 Ruff 通过。没有扩展到新 case，也没有针对业务字面值的确定性生成修补。

最终实际生成 SQL：

```sql
SELECT CAST(COUNT(School) AS REAL) / 12 AS monthly_average
FROM schools
WHERE County = 'Alameda'
  AND DOC = '52'
  AND OpenDate IS NOT NULL
  AND strftime('%Y', OpenDate) = '1980';
```

评估证据：`output/ontology_two_case_review/case47_r8/review_result.json`；逐单元质量结果和原始图另导出至 `review/case47_r8/assessment.json`、`graph.json`。交互图支持搜索、类型筛选、全部节点、节点邻域及完整关系说明；全部数据直接来自保存快照。

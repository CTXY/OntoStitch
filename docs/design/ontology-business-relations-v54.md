# v54：业务谓词与数据库记录范围

## 修改原因

`school_recorded_county`、`school_recorded_ownership_code` 主要表达某条记录存有某字段，未清楚命名业务语义关系。此前强调来源、当前记录与历史发生范围的指引，没有充分区分这些限制应放在哪里。

## 生成与评估要求

- 语义节点之间的谓词应使 `source + predicate + target` 成为有依据的业务陈述。
- 谓词按实际端点含义命名；code/category 不因为自然语言更顺就变成 district 等实体。
- 字段绑定继续使用 grounded_on；物理目录关系继续使用 belongs_to、value_of、joins_with，不统一改成业务关系。
- 数据库如何记录事实、证据来源、可缺失性及当前/历史适用范围放入 description。约束仍需有证据，不因使用业务谓词而增加强度。
- Agent 可复用 has_attribute 等已有本地契约，或提出更具体的业务谓词。没有固定学校谓词表、字符串替换、专用 SQL 改写或特定 case 的分支。

例如 School → County 可表达 located_in，记录支持的所在地不证明开校发生地；School → District ownership code 可表达按学区管辖类别分类，不能写成隶属于某个 code。具体谓词由 Agent 按证据、端点含义与保存的契约选择。

## 实现范围

1. `llm/prompts/asset_contract.py`：共享关系指引，版本升级至 v54。
2. `training/quality_contract.py`：predicate_precision 检查业务含义与存储含义是否混淆。
3. `llm/prompts/learning/assessment.md`：关系评估落实同一要求。
4. `llm/prompts/learning/types/event.md`：示例改用 occurs_on_date，避免示例把记录措辞引入业务谓词。
5. `ontology-graph-design.md`：同步规范。

未增加节点/关系字段，未扩大核心谓词清单。正式 extraction workflow 自动执行 SQL 验证、Assessment、accept、最终验证和 seal，不使用 review_only。

## 真实运行与验证

当前运行目录为 `output/ontology_v54_business_relations_review`。case47 重新抽取；另选 BIRD case27（开校/关校日期、学校联系方式及 SAT writing score）与 case77（年级范围、FRPM 百分比）。三个 case 独立运行，使用只读数据库与正式 sql-extract 入口；数据库探索由 Agent 决定。

运行摘要、精确 accepted_revision、最终 SQL、各轮 Assessment 及交互图由真实结果生成。完整图保持原始 Accepted snapshot；业务视图仅保留语义节点、关联物理端点与必要目录/连接键，以便展示，不修改训练图。

全量回归：883 passed；专项指引/质量/Assessment：74 passed；Ruff 通过。模型实跑结果以同目录下正式 result.json 为准，不将实验标签当作验收证据。

## 最终结果

| Case | 正式状态 | 语义节点 | Assessment 单元 | 连续正确 replay |
|---|---|---|---|---|
| 47 | stable_correct | {'concept': 6, 'event': 1, 'generation_rule': 1} | 23 | 2 |
| 60 | stable_correct | {'concept': 5} | 16 | 2 |
| 77 | stable_correct | {'concept': 5, 'metric': 1, 'generation_rule': 1} | 22 | 2 |

以上均有匹配版本的正式 acceptance evidence，导出 Accepted 图，无 review_only。精确版本、SQL 与结果路径见运行目录 verified_run_summary.json。

case27 作为额外诊断保留：四次 Assessment 后仍有描述支持/可缺失性问题，未接受语义候选；在下一轮扩展前由进程控制器中断，checkpoint 未删除，未伪造训练完成结果。最后已评估候选另存 diagnostic 视图，明确不是 Accepted 图。随后补跑 case60，使新增主要 review 案例为 case60、case77。

展示页质量结论改为读取与展示 revision 匹配的 Assessment，不再由整条 SQL/流水线 correct 字段推断质量通过。

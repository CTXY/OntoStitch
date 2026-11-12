# Extraction training 的 ontology graph 状态管理

## 完成条件

正式训练自动验收，不使用 `review_only`、`formal_case_acceptance` 或“等待用户 review”状态。用户查看结果不阻塞训练，也不改变验收结果。

`stable_correct` 必须来自正式 workflow 的验证与验收；不能通过更改展示标签获得。SQL 稳定正确与 ontology 变更被接受是不同事实：SQL 正确不能代替 Assessment。

## 版本与指针

每次图变更保存不可变 snapshot。GraphStore 维护 Working、Accepted 两个 revision 指针；Baseline 和 Trial 是 workflow 引用的 snapshot，不是额外的人工审批状态。

| 名称 | 含义 | 更新规则 |
| --- | --- | --- |
| Working | Agent 当前编辑图 | 保存修改后移动；不自动成为最终输出 |
| Baseline | 当前学习阶段的对照版本 | 用于 diff、Assessment 和修改分组 |
| Trial | 本次被验证、评估的精确版本 | 验证时固定 revision；后续改图使旧证据不能直接验收新图 |
| Accepted | 已被自动接受的图；初始化时为物理事实基线 | 仅正式 accept 门禁允许推进 |

Accepted 是 case 内部接受，不表示已经执行全局 merge。`merge_performed=false` 不意味着 case 未通过。

## 自动流转

1. Direct/Repair 只修改 Working。
2. SQL 验证针对确定的 Trial 进行规定次数的连续正确 replay。
3. Assessment 检查该 Trial 的节点、关系、应用和修改分组。
4. 若只接受部分修改，先构造选中的新图，再重新验证、评估；不能沿用原 Trial 的验收结论。
5. accept 要求 SQL 稳定正确、Assessment 接受全部待提交修改、接受成员与实际 diff 一致，并保存 acceptance evidence 后推进 Accepted。
6. retain 将 Working 指向 Accepted；未接受的 snapshot 可以保留作审计，不进入最终图。
7. validate_final 验证 Accepted。Direct 完成且最终 replay 正确时标记 `stable_correct`；正式成功输出还要求没有 operational failure。
8. seal 对含语义资产的 Accepted 检查匹配版本的验收、SQL 与 Assessment 证据，然后导出该版本。

不需要为用户 review 新增 ontology graph 状态。失败、轮数耗尽或基础设施异常使用既有 workflow 结果，不以人工确认代替自动门禁。

现有 `case_status=needs_review` 是未通过且非 operational failure 的结果分类，并非等待人工输入的运行状态；workflow 已经结束。它不能用于把通过的结果挂起，也不等同于实验脚本的 `review_only`。

## 不变量

设 W、A、T 分别为 Working、Accepted、Trial，D 为 A 到 W 的实际变更成员，M 为 Assessment 接受成员：

```text
accept ⇒ stable_sql(T) ∧ assessment_acceptable(T) ∧ M = D
         ∧ T.revision = W.revision
         ∧ validation.fingerprint = fingerprint(T)
exported_revision = A.revision
final_revision = accepted_revision
retain ⇒ W.revision = A.revision
```

所有证据必须与被验收版本匹配；同 case、相同 SQL 文本或相同结果值不能替代图版本匹配。checkpoint 恢复保存的版本指针与执行位置，不通过重新解释展示状态恢复。

## v53 实验记录的边界

`output/ontology_v53_case47_review/refine_review.py`、`verify_review.py` 是上一轮独立实验驱动，绕开了正式 accept/seal，故保存了候选及独立验证结果。其 `review_only` 等字段不属于生产状态契约，也不能作为以后实际训练的入口。保留这些记录仅为审计；若要形成正式结果，须执行正式 workflow 并产生匹配的验收证据，而非覆盖历史候选标签。

生产实现位于 `training/workflow/{state,graph,nodes,runtime}.py`、`graph/snapshots.py` 和 `artifacts/cases.py`。回归测试 `test_case_workflow.py` 检查自动成功输出、部分选择重新验收、拒绝候选不推进 Accepted；`test_workflow_recovery.py` 检查 checkpoint 恢复。

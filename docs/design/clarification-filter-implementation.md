# Clarification Filter Agent：基础字段属性优先

日期：2026-11-14

本轮实现只增加提问前过滤。现有三阶段 discovery、候选 SQL fragments、多选／自由说明、来源评分和提问预算继续沿用。风险收益排序保留为独立探索方案。

## 流程

    retrieve → discover → filter_questions → 原来源排序 → plan_questions
                                      ↘ 自动指导 → finalize

Filter Agent 接替默认装配中的旧 relevance audit，同时保留它对无关、互补和明显非局部问题的基本审核。过滤步骤只执行一次；用户回答后的动态重过滤不在本轮范围内。

## 明确约束

question/evidence 自动确定需要四项同时满足：原文锚点、与争议维度一致、无需新业务假设、能够说明其他选项为什么违反该约束。

程序校验原文片段真实存在、选项 ID 合法、所有其他选项都有排除说明；语义判断由 Agent 完成。模型偏好或来源更强不能替代明确约束。证据不充分就 keep。

## 数据库分支

优先定位基础表、字段以及候选操作，再检查 NULL、重复、类型、格式和值域。COUNT(*) 统计行，不拥有列参数。

例如 Agent 可以生成：

    SELECT COUNT(*), COUNT(TransactionID), COUNT(DISTINCT TransactionID)
    FROM transactions_1k

不把 initial SQL 的币种、日期、时间等业务过滤或 JOIN 作为可信前提复制到探查中。允许 WHERE col IS NULL 等直接检查属性的谓词。

非空可消除同一行来源上的 COUNT(*) / COUNT(col) 差异；无重复的非 NULL 值可消除基础行来源上的 COUNT(col) / COUNT(DISTINCT col) 差异。

必须同时说明基础属性为何适用于这些候选。基础表唯一不能证明一对多 JOIN 后仍唯一；外连接可能引入 NULL；复杂表达式或未决来源可能使属性不再适用。依据不足就保留，不通过构造更复杂的答案查询强行消解。

发现 NULL／重复只能说明基础属性不足以证明等效，不代表最终查询结果一定不同。

## Agent 与工具

沿用项目已有 run_llm_extraction 结构化调用协议。模型每轮返回工具请求或最终结论；宿主分派真实工具并把观察回传到下一轮。无需供应商特有的 native tool-calling 支持。

- inspect_schema：查看明确指定表的实际元数据。
- query_database：执行单条只读 SQL；SQL 由 Agent 生成。

数据库由应用绑定，不从模型响应选择连接。CLI／评估使用配置数据库；Web 使用当前会话的数据库副本，避免探查默认演示数据库。

默认最多 6 个 Agent 轮次、8 次工具请求，SQL timeout 8 秒、最多返回 200 行。失败请求也计入探查预算。元数据工具有宿主等待期限；数据库线程遵循适配器自身的取消／超时能力。

SQLite 复用下沉后的 QueryDatabase：只读文件连接、query_only、执行授权、单查询检查和 SQLite progress timeout。原 nl2sql.database 导入路径保持兼容。MySQL 使用单条只读 SQL 检查、read-only transaction 与已有查询执行超时。

## 结论与保守边界

| 结论 | 要求 |
|---|---|
| keep | 不同、未知或证据不足；原候选和评分属性保留 |
| question_resolved | 明确需求选定现有 option，引用原文并解释全部其他 options |
| database_equivalent | 全部 options 被基础属性消除差异；需 data_property、applicability_reason 和真实成功探查引用 |
| not_relevant | 沿用旧审核的无关／互补／明显非局部问题判断 |

当前版本的 database_equivalent 要求至少一次成功、未截断的 SQL 观察；单独元数据不能触发删除。全部 options 都须覆盖；A=B、C 未知／不同保留整题，不缩减候选，不改变来源评分的含义。

超时、SQL 解析／执行错误、数据库不可访问、方言不匹配、行／单元格截断、缺失或伪造 probe ID、跨 slot 引用、未覆盖全部选项、缺少属性适用说明、模型输出异常均不能证明等效。不能拿少量样本、任意成功 SELECT 或相同空结果当作全列属性证明；这些语义边界由 prompt 约束及审计评估保证，不宣称宿主程序证明了 SQL 等价。

## 指导与恢复

自动结论以 question_resolved / database_equivalent 来源写入 InteractionGuidance，保留原选项 SQL 行为；不伪造用户 InteractionAnswer。

过滤后没有问题也会生成最终 guidance。数据库结论写明基础属性、适用性与本次使用边界，不提升为永久用户政策。原图不根据自动代表选项裁剪。

决策、自动指导及工具观察保存在 LangGraph filter_questions 节点 checkpoint；重建引擎恢复时不重复运行已提交的过滤步骤，也不消耗额外用户问题预算。过滤节点完成前崩溃可能重新执行有界的只读探查。工作流版本升级，旧 pending 需要新 run。

本轮针对绑定的数据源与一次运行。SQLite 文件路径／大小／mtime 纳入恢复身份；这不等同于动态数据库的一致性快照，也没有保证跨长时间等待的数据稳定性。

## 配置与对照

    services:
      online_context:
        clarification_filter:
          enabled: true
          max_rounds: 6
          max_probes: 8
          sql_timeout: 8
          max_rows: 200

设 enabled: false 恢复旧 audit + 原来源排序，便于实验对照。没有可用数据库时仍能判断明确约束；数据库分支保留问题。

现有 discovery/prioritization.py 没有修改，来源权重、评分公式和 select_questions 的预算语义保持原样。新的工具预算与用户问题预算分开。

## Review 入口

- [Agent 与结论校验](../../src/cortex/capabilities/online_context/filtering.py)
- [Filter prompt](../../src/cortex/capabilities/online_context/llm/prompts/filter_clarifications.md)
- [数据库适配器](../../src/cortex/infrastructure/online_context/probes.py)
- [共享 SQLite 执行工具](../../src/cortex/infrastructure/database/read_only_sqlite.py)
- [默认装配](../../src/cortex/bootstrap/online_context.py)
- [工作流](../../src/cortex/capabilities/online_context/workflow/graph.py)
- [指导交接](../../src/cortex/capabilities/online_context/engine.py)
- [回归测试](../../tests/test_clarification_filter.py)

测试使用脚本化模型输出与实际临时 SQLite 数据库，覆盖工具循环、结论检查、失败保留、guidance、恢复与原评分一致性；它们不证明真实模型的语义判断精度。真实模型误过滤率需要后续使用冻结案例单独审计。

## 本轮验证结果

- 全量：`.venv/bin/python -m pytest -q` → **1235 passed, 1 warning**。警告来自已有测试中的 Pydantic class-based config 弃用提示。
- 相关模块回归：Filter、online workflow、projection、retrieval handoff、NL2SQL、portal → **86 passed**。
- 新增 Filter 测试 → **41 passed**。
- 修改范围的 Ruff 检查与 git diff --check 通过；来源排序模块无 diff。
- 真实 BIRD 基础字段只读探查：transactions_1k 上 COUNT(*)／COUNT(TransactionID)／COUNT(DISTINCT TransactionID) 均为 **1000**；financial.card 上三种计数均为 **892**，均未附加业务过滤，结果未截断。

未调用真实 LLM 评估误过滤率；MySQL 适配器以离线测试验证 SQL 边界和截断标记，未连接真实 MySQL 实例。

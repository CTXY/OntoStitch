# Clarification Filter Agent：基础字段属性优先

更新：2026-11-15（末尾定向评测记录保留其历史版本结论）

本轮实现只增加提问前过滤。现有三阶段 discovery、候选 SQL fragments、多选／自由说明、来源评分和提问预算继续沿用。风险收益排序保留为独立探索方案。

## 流程

    retrieve → discover → filter_questions → 原来源排序 → plan_questions
                                      ↘ 过滤审计记录（不生成指导）

Filter 只决定问题是否可过滤，不负责 not_relevant；问题是否合理由 Organizer 判断。过滤步骤只执行一次；用户回答后的动态重过滤不在本轮范围内。

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

当前版本的 database_equivalent 要求至少一次成功、未截断的 SQL 观察；单独元数据不能触发删除。全部 options 都须覆盖；A=B、C 未知／不同保留整题，不缩减候选，不改变来源评分的含义。

超时、SQL 解析／执行错误、数据库不可访问、方言不匹配、行／单元格截断、缺失或伪造 probe ID、跨 slot 引用、未覆盖全部选项、缺少属性适用说明、模型输出异常均不能证明等效。不能拿少量样本、任意成功 SELECT 或相同空结果当作全列属性证明；这些语义边界由 prompt 约束及审计评估保证，不宣称宿主程序证明了 SQL 等价。

## 候选所需的 memory 输入

Filter 的程序接口仍接收完整检索状态，LLM 输入改由 `filter_evidence.build_filter_evidence` 按当前候选的 `graph_refs` 构造。每轮只重发这份候选证据和累计探查观察，不再重发完整检索子图。

序列化顺序为：system 规则与输出契约 → dialect/工具说明 → 必要 memory → question/initial SQL/候选/数据库可用状态 → observations/轮次控制。把可复用前缀放在任务变量前面，观测变化不会破坏之前的前缀；是否实际命中缓存仍由供应商决定。

- 引用普通节点时保留完整定义；引用 alternative 时用已有 `alternative_content` 展开公共字段和该分支的覆盖内容，保留条件、公式和粒度，不附带其他分支。
- 引用普通节点的 target 时只取该完整绑定及必要的所属定义；有冲突的父节点 target 不猜测分支。笼统引用冲突父节点时保留全部分支。
- 递归补充定义明确声明的 references、expression 操作数、绑定和表列归属等依赖，并保留已需表之间的已知 JOIN 契约；不按语义邻居扩张，不使用另一个 LLM 做摘要或相关性筛选。
- 多个候选或 slot 共用的证据按引用 ID 去重。question/evidence、initial SQL、候选 SQL fragments、探查结果、过滤 prompt、来源排序和阈值保持原样。
- 继续排除原流程不信任的 dimension values。必要引用缺失或无法可靠展开时，宿主强制保留受影响的 slot；不将缺少条件的证据当成完整支持，不自动回退整图。

该视图只用于模型输入，不作为新 GraphState 保存，也不裁剪最终交给后续 SQL 生成流程的图。上下文身份版本升至 3，避免把旧输入构造下的过滤运行当成当前运行恢复。

## 指导与恢复

Filter 仅保存 question_resolved / database_equivalent 审计结论，不生成 InteractionGuidance、不选择代表 option，也不伪造用户 InteractionAnswer。

过滤后没有问题也可由 Guidance Agent 判断当前任务的 GenerationRule 适用性；Filter 观察不成为 SQL 指令。原图不根据自动代表选项裁剪。

决策及工具观察保存在 LangGraph filter_questions 节点 checkpoint；重建引擎恢复时不重复运行已提交的过滤步骤，也不消耗额外用户问题预算。过滤节点完成前崩溃可能重新执行有界的只读探查。工作流版本升级，旧 pending 需要新 run。

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
- [候选证据输入构造](../../src/cortex/capabilities/online_context/filter_evidence.py)
- [Filter prompt](../../src/cortex/capabilities/online_context/llm/prompts/filter_clarifications.md)
- [数据库适配器](../../src/cortex/infrastructure/online_context/probes.py)
- [共享 SQLite 执行工具](../../src/cortex/infrastructure/database/read_only_sqlite.py)
- [默认装配](../../src/cortex/bootstrap/online_context.py)
- [工作流](../../src/cortex/capabilities/online_context/workflow/graph.py)
- [指导交接](../../src/cortex/capabilities/online_context/engine.py)
- [回归测试](../../tests/test_clarification_filter.py)
- [证据裁剪边界测试](../../tests/test_filter_evidence.py)

测试使用脚本化模型输出与实际临时 SQLite 数据库，覆盖工具循环、结论检查、失败保留、guidance、恢复与原评分一致性；它们不证明真实模型的语义判断精度。真实模型误过滤率需要后续使用冻结案例单独审计。

## 本轮验证结果

- 全量：`.venv/bin/python -m pytest -q` → **1235 passed, 1 warning**。警告来自已有测试中的 Pydantic class-based config 弃用提示。
- 相关模块回归：Filter、online workflow、projection、retrieval handoff、NL2SQL、portal → **86 passed**。
- 新增 Filter 测试 → **41 passed**。
- 修改范围的 Ruff 检查与 git diff --check 通过；来源排序模块无 diff。
- 真实 BIRD 基础字段只读探查：transactions_1k 上 COUNT(*)／COUNT(TransactionID)／COUNT(DISTINCT TransactionID) 均为 **1000**；financial.card 上三种计数均为 **892**，均未附加业务过滤，结果未截断。

已追加真实 LLM 定向检查，结果见下文；总体误过滤率仍未估计。MySQL 适配器以离线测试验证 SQL 边界和截断标记，未连接真实 MySQL 实例。

## 历史版本：真实 LLM 定向检查补充

使用配置的 deepseek-v4-pro 完成主评估 28 次 Filter 执行、30 次 slot 判断：7 次明确约束过滤、8 次基础属性过滤、15 次保留。人工审阅未发现应保留问题被误删；1 次可接受的保守保留未据此修改 prompt。该结果来自小规模定向集，不代表总体误过滤率。

详见[真实 LLM 检查报告](../../artifacts/filter_agent_live/2026-11-14-review.md)，包含原始响应、SQL 探查、反例复测、耗时及 token 统计。

## 历史版本：候选上下文与固定前缀 A/B 补充

使用同样的冻结 cases，整图组与候选上下文组各重新执行 28 次、30 个判断。两组都将固定 memory 放在 question、initial SQL、候选和 observations 之前。模型配置、过滤 prompt、来源排序和阈值不变。

候选版 prompt tokens 由 1,061,000 降至 186,488（−82.4%），总 tokens 由 1,183,240 降至 301,334（−74.5%）；平均耗时 78.79 秒降至 75.00 秒（−4.8%）。耗时改善较小，不能按输入体积推算加速。

候选版过滤 10 次，保留 20 次；14 次必须保留的边界判断全部保留。整图版过滤 16 次，其中两次忽略一对多 trans JOIN、依据不足；其余四处差异是候选版的保守漏过滤。没有为增加过滤数量而修改规则。SQL 代表选项在已证明等效的情况下可以不同。

最终相关模块回归 **112 passed**，Ruff 和 diff 检查通过。并行评测期间发生过恢复测试的一秒时序断言超时，单独及评测结束后的组合复测通过，详细记录保留。

详见[候选上下文 A/B 报告](../../artifacts/filter_agent_live/2026-11-14-context-ab-prefix/review.md)，包含每处差异、原始请求、缓存输入 tokens、独立反例检查及复现命令。此前旧顺序的探索记录单独保留，未混入主统计。

## 当前职责与验证

当前完整职责规范见 [Merged graph Online 验证](online-merged-graph-validation.md)。
等价性依赖完整数据库性质，不采用业务过滤子集的偶然相等；近似不等于严格等价。
显式文字值与数据库值证据冲突时保留问题，由用户/Simulator 消歧，Filter 不自行改值。
当前测试与历史记录分开保存在 output/california_online_v83。

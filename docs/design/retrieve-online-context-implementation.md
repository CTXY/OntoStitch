# Retrieve DB 与 Online Context 实现说明

本次修改将 online_context 原有的表示级检索、相关性判断和图扩展迁入 retrieve_db。retrieve_db 是唯一运行时检索实现；online_context 调用该服务，再负责歧义发现、分轮提问、回答后的任务投影和 guidance。内外统一采用节点内联冲突结构，merge、sql-train、检索和在线交互使用同一个GraphNode模型。旧sidecar格式仅保留导入兼容。

## Review 入口

建议依次查看：

1. `src/cortex/schema/graph_io.py`：内联冲突图导入、导出及校验。
2. `src/cortex/application/retrieval.py` 与 `application/retrieve_db.py`：统一证据及服务契约。
3. `src/cortex/capabilities/retrieve_db/workflow/`：唯一检索工作流。
4. `src/cortex/capabilities/online_context/workflow/`：持久化交互与恢复。
5. `src/cortex/capabilities/online_context/output/projection.py` 中 `project_graph_state`：选择后的候选裁剪。
6. `tests/test_retrieval_online_handoff.py`：实际讨论图上的检索、回答和投影端到端测试。

工作区在本轮开始前已有大量未提交修改；没有执行 reset、checkout、commit 或暂存。Git 相对 HEAD 的整个 diff 包含先前工作，不能直接等同于本轮变更。

## 代码组织

```text
src/cortex/
  schema/
    graph.py                     # 唯一内联节点模型，GraphState仅保存graph
    graph_io.py                  # 历史格式导入与统一格式校验/导出
  application/
    retrieval.py                 # 检索表示、列语义、相关性结果
    retrieve_db.py               # 检索服务与结果验证
    online_context.py            # 交互契约、pending、终态与公开输出
  capabilities/
    query_signals/
      signals.py                 # 初始SQL、schema呈现、别名处理
      llm/prompts/initial_sql.md
    retrieve_db/
      engine.py                  # 装配检索节点与恢复身份
      retrieval/
        index.py                 # 节点、target、plan、alternative表示
        recall.py                # TypedRecall、精确匹配保护、embedding
        selection.py             # 语义资产及物理grounding相关性判断
        normalization.py
      graph/context.py           # 邻域扩展、必要引用闭包、子图构建
      workflow/
        graph.py                 # LangGraph路由
        nodes.py                 # signals、recall、select、assemble
        runtime.py               # SQLite恢复、请求版本校验、单写
      llm/prompts/               # 相关性prompt与本地registry
      legacy/                    # 旧图索引/闭包的测试与导入兼容；非主流程
    online_context/
      engine.py                  # 调用retrieve-db并组织交互结果
      discovery/                 # 节点内、跨节点、SQL与memory歧义
      interaction/               # 原评估用户等交互支持
      output/
        projection.py            # 完整GraphState任务投影
        guidance.py              # 确认行为渲染与校验
        query_examples.py
      workflow/
        state.py                 # JSON可序列化状态
        graph.py                 # retrieve→discover→plan→interrupt→finalize
        runtime.py               # async checkpoint、恢复、幂等
      llm/prompts/               # 交互prompt、共享规则与版本
      retrieval/                 # 旧导入路径的薄re-export，无第二份算法
      graph/context.py           # 同上
  bootstrap/
    retrieve_db.py               # 唯一检索配置与模型/缓存装配
    online_context.py            # 注入同一个检索服务
  infrastructure/
    workflow/locking.py          # 两个工作流共用单写锁
    llm/embedding_cache.py       # 共用向量缓存及embedding空间身份
    retrieve/graph.py            # 完整GraphState加载
    retrieve/artifact.py         # inline图、结果及HTML产物
```

旧 retrieve-db 的 selection、semantic、signals 主算法已移除。保留的 legacy 模块仅供既有直接导入与图契约测试；新的 engine 不调用它们。online_context 原检索模块仅转发到 retrieve_db，避免重复维护算法或prompt。

## 公开图与内部模型

公开 merged graph、相关图和检索产物使用纯 `{nodes, edges}`。冲突节点主体包含公共字段和以下字段：

```json
{
  "id": "generation_rule.example",
  "node_type": "generation_rule",
  "merge_status": "conflict",
  "consolidation_status": "semantic_conflict",
  "preservation_reasons": [],
  "alternatives": [
    {"alternative_id": "a", "overrides": {}, "sources": []}
  ],
  "conflict_points": []
}
```

以上只说明字段位置，省略了有效节点内容；不是可直接加载的样本。可加载实例见 domain_graph/graph_state.json，其中每个候选来源非空，至少两个候选，overrides 包含完整差异字段。

`GraphState.model_validate()` 接受纯内联图、`{graph: inline}`、旧的 `{graph, node_conflicts}`。混合输入必须完全一致，否则拒绝。`export_inline_graph(state)` 仅导出内联图，不附带另一份 node_conflicts。

Python内部也只保存 `GraphState.graph`，每个冲突GraphNode自身持有alternatives、来源和conflict_points；默认model_dump和checkpoint都使用相同内联节点。`state.conflict_nodes`是节点对象的只读筛选，`node_conflicts`历史属性仅按需派生兼容DTO，不存储、不序列化。`result.subgraph`现在也是完整内联节点；检索结果只存一个retrieved_graph_state，投影结果只存processed_graph_state，旧字段仅在输入边界归一化或提供派生视图。

内联导入/导出检查候选来源、owner来源与候选来源union、状态对应、公共字段与overrides分区、候选完整还原。业务内容、引用和边继续通过 GraphManager 校验。不编造来源，不静默选择候选。

图仓库新写入 `graph-state-v3-inline`，只有nodes.jsonl与edges.jsonl，无conflicts.jsonl。v2旧bundle读取后立即成为内联模型，下一保存写v3。HTML/Web也直接从nodes读取候选，没有单独冲突payload。merge、训练、retrieve-db与online_context工作流版本已升级，旧checkpoint不继续恢复。

## 唯一检索链路

LangGraph 检索工作流执行 signals → recall → select → assemble。

- signals：使用调用方SQL，或调用共享 InitialSqlGenerator；只把可解析的只读SQL作为信号，不执行SQL。模型生成失败会报错并允许持久恢复，不伪装成无匹配。
- recall：沿用原 online_context 的 RepresentationIndex 与 TypedRecall。普通节点、Concept target、Metric formula/plan 和冲突候选都可进入表示。精确命中优先保留，非精确候选按类型预算召回。
- embedding：冲突候选的向量文本来自完整还原内容；默认证据仍为公共字段一次加overrides，不重复铺开全部正文。候选来源、状态与冲突点贯穿结果。
- select：模型分别判断语义资产与物理grounding，输出 relevant/uncertain/irrelevant。保留 relevant、uncertain 和受保护精确命中；相关不表示冲突已解决。
- assemble：按原邻域策略扩展，再补齐所有候选的必要引用、物理所有者及复合JOIN列。必要依赖不因两跳限制被丢弃。原160节点限制是邻域启发式边界，不是最终必要依赖图的硬上限。

历史推断值不作为可信的直接值召回，但如果已保留资产明确引用它，闭包会保留其原来源以保证完整性；不会把它伪装成数据库确认值。

`RetrieveDbService.run()` 标准输入是 graph_state，兼容旧 graph 参数。结果保留训练需要的 subgraph 等Python字段；公开payload只输出一个 inline relevant_graph。retrieve-db不再向用户提问；旧clarification选项只是兼容入口。

新召回预算归属 `services.retrieve_db`，默认 concept=12、metric=10、generation_rule=10、column=20、dimension_value=12。online_context中旧limit字段保留解析兼容，不再控制主检索。旧 selection_policy 不再选择另一套运行时算法。sql-train接口兼容不代表历史检索输出逐字相同。

## Online Context 工作流

```text
retrieve → discover → plan_questions
                         ├─ 有问题 → await_answers(interrupt) → plan_questions
                         └─ 无问题 → finalize → END
```

retrieve直接调用RetrieveDbService。discover沿用原三类歧义分析与证据审计，conflict标记和conflict_points只是分析证据，不自动成为用户问题。提示用户关注业务后果，内部ID保留在证据引用里。

本版从固定检索快照发现一组独立choice points，再分轮处理；不会在每次回答后重新调用召回或重新生成所有问题。回答引入新的业务范围时，当前应开启新run；尚未实现自动query改写与重新检索。

默认每轮一个问题，由 questions_per_round 配置；question_budget限制整个会话展示的问题总数。已回答问题按稳定ID跳过。预算不足的choice points保留在 unresolved_choice_point_ids，终态为 completed_with_unresolved，不生成伪确认guidance。

等待节点仅调用interrupt和校验回答，不在interrupt之前调用模型或写文件。回答必须覆盖当前待答问题，选项ID必须存在。保留既有“有选择时以选择为准；无选择时以解释为准”的契约；同时选择并附说明不会偷偷修改已选语义。

## 图投影与 guidance

`project_graph_state()` 复用原安全映射检查，只有选项完整、唯一地覆盖同一owner的候选时才裁剪。

- 未回答或映射不安全：保留完整原冲突。
- 安全单选：还原该候选为普通节点，来源采用该候选sources。
- 安全多选：保留完整候选子集，重新提取common/overrides，更新来源union和仍有区分作用的conflict_points。
- Concept target选择：沿用原target投影规则。
- 最后用GraphManager同步引用边与alternative归属，并严格校验。

原始图和检索快照不变。ProjectionDecision记录保留和排除引用。Guidance只接收已回答的决策；未决choice points单独标记。仍保留多个完整定义时，图中仍然是conflict，并不意味着系统宣布这些定义完全等价。

## 恢复与观测

retrieve-db每个run保存独立SQLite checkpoint。online_context默认保存在 `workspace/checkpoints/online_context.sqlite`，按run_id隔离会话。两者共用非阻塞文件锁，防止同run并发执行。

online恢复身份包括workflow版本、生产prompt bundle、choice/answer/guidance schema、ontology schema、检索实现身份和相关配置；源图fingerprint覆盖冲突内容。变化时拒绝恢复。已回答内容、representation/relevance、检索快照和预算都在checkpoint中保存。

重复回答在已结束或等待下一轮时返回既有结果。若回答已保存、但finalize暂时失败，重试恢复剩余节点，不能返回旧pending，也不能重复消耗回答。`run --run-id` 还支持在提问之前失败后的节点恢复。

LangSmith通过已有native_tracing与LangChain模型调用记录原生span，携带稳定session/run元数据；沿用统一凭据脱敏。未启用tracing时流程照常工作。本次验证没有调用真实模型或向远端LangSmith上传演示数据。

## Prompt 管理

retrieve-db拥有select_semantic_assets与select_schema_grounding；online_context拥有歧义发现、问题审计和guidance；共享InitialSQL prompt在query_signals包内。各目录registry与README定义加载角色和维护规则，文件随Git版本化并通过importlib.resources加载。

沿用既有Markdown业务约束，新增用户可理解的局部决策和历史冲突适用性要求。prompt hash覆盖拼接正文与共享片段，工作流身份另外包含输出schema及行为配置。当前角色继续使用LangChain chat model加Pydantic JSON验证和有界修复，不同时混用ToolStrategy与 `<result>` 文本协议。simulated-user及Gold审阅prompt仅供评估，不由生产工作流装配。

## 人工整理的 domain 图

主要输入：`artifacts/online_context/codebase_community/domain_graph/graph_state.json`，167节点、206边、1个冲突节点、2个候选、1个原始dp_count_method。

来源是外部mini_clean_BIRD/codebase_community的merged graph。subagent人工审阅资产与结构，保留两种完整计数定义与独立来源；没有重新运行merge或用批量迁移算法替代语义判断。

确定性修改和边界：

- 合并一个相同column和值的重复节点 comments.Score=0，保留来源并记录ID映射。
- 适配Concept target、规则、Metric旧字段和物理JOIN结构；完整记录变更。
- 原 posts.ParentId=posts.Id 自连接不受当前schema支持，从可加载图中排除，完整原边保存在review.excluded_legacy_edges；其余12个JOIN保留。
- 隔离日期格式规则和一个比例Metric：前者将单字段格式要求泛化到所有DATETIME，后者的结构化公式未表达分子分母独立作用域。完整节点、来源和关联边保存在 `domain_graph/review/quarantined_assets.json`，没有编造替代定义。
- users多target保留：不同source-local binding不是可互换的唯一键，使用时仍需按任务判断。结构校验不能证明全图业务正确性。
- 原533是users单表计数，672是users/posts连接去重计数。泛化后动作有差异，但原任务并不互相矛盾，不能据历史conflict强迫用户回答。

这张图标记为reviewed demo candidate。来源case的SQL只用于溯源和评分，不作为生产检索输入；后续证明下游收益需要独立问题和固定模型/生成设置的对照评估。

## 本地运行

在仓库根目录、已配置模型凭据后运行。以下是实际CLI接口示例，不代表本次已调用真实模型。

```bash
.venv/bin/cortex retrieve db \
  "How many users have qualifying posts?" \
  --run artifacts/online_context/codebase_community/domain_graph/graph_state.json \
  --run-id reviewretrieval01 --dialect sqlite

.venv/bin/cortex online-context run \
  "How many users have qualifying posts?" \
  --graph-state artifacts/online_context/codebase_community/domain_graph/graph_state.json \
  --run-id reviewonline01 --question-budget 3 --output workspace/online-result.json

.venv/bin/cortex online-context resume \
  --graph-state artifacts/online_context/codebase_community/domain_graph/graph_state.json \
  --pending workspace/online-result.json \
  --answers workspace/answers.json --output workspace/online-result.json
```

answers.json为数组，每项包含实际返回的question_id和selected_option_ids；不选选项时填写explanation。不要使用示意ID替代运行结果。保持问题、图、配置和run ID相同才能恢复。

## 验证与已知限制

测试覆盖inline导入导出、来源和混合格式拒绝、完整冲突召回、同run回放、失败节点恢复、必要value引用保留、跨engine多轮恢复、非法和stale回答、重复提交、预算耗尽、安全单选/多选/不裁剪，以及真实讨论图从检索到回答后的端到端投影。

新端到端测试使用真实讨论图和确定性模型替身。内联模型迁移新增仓库存储、v2导入后写v3、状态原子切换及checkpoint版本检查测试。最终全量测试结果：654 passed，2 warnings；核心变更范围Ruff与git diff --check通过。

真实LLM生成质量、LangSmith远端展示及下游准确率提升尚未通过本轮实验验证。文件锁依赖当前macOS/Linux的fcntl，未新增Windows运行支持。检索启发式预算是软边界，必要依赖闭包可能超过预算。旧legacy导入兼容仍存在，但不参与新的运行时主流程。

# Extract LangGraph 实施说明

当前唯一架构见 [单 case 设计](extract-case-agent-loop.md)。默认训练入口和 repair-only 实验都执行同一张同步 LangGraph；旧的手写 Controller、phase 分发和手工 checkpoint 文件已删除。

## 1. 运行链路

```text
CLI → SqlTrainingService.run → SqlTrainingCapability.train
    → 扫描全量范围内 Table/Column、条件 Value
    → TrainPipeline.run → QA 标准化、Gold metadata/JOIN/Value 准备
    → 每个 case：CaseWorkflow.run → LangGraph 十个业务节点
    → ready semantic fragments + verified Value 增量 → Incoming → Graph Merge
```

训练和 Merge 的 Python API 保持同步，case 线程池和 Agent 只读工具并发保留。在线 RetrieveDB 有实际并行检索，训练在专门适配器内为每个 case 复用惰性 Runner，退出前关闭；共享注入模型通过同步客户端适配避免原生异步连接跨循环。Runner 关闭不等于显式 aclose 所有 HTTP 客户端。

## 物理图关系约定（2026-10-23）

`joins_with` 仅表示可复用的完整等值连接键，不记录默认 SQL JOIN 类型。
`properties.column_pairs` 为非空列表，每项只有 `from_column_ref` 和
`to_column_ref`，隐含等号，多项按 AND 组合。可选 `cardinality` 为
`1:1`、`1:N` 或 `N:1`，方向相对于存储的 from/to；未知时省略。
Table 的 `unique_keys` 保留数据库声明的完整唯一键，和 `primary_keys`
共同决定基数，不从 Gold SQL、列名、样本或当前数据唯一性猜测。

目录构建只发布声明外键，不再将同名列推断为正式关系。Gold 阶段只处理
显式 JOIN ON/USING；ON 的每个合取项都必须是可解析的直接物理列等式，
按来源实例收集完整列对，限定列和裸列不会被拆成不同关系。复杂 ON、
派生来源、自关联、WHERE 中补充的跨表约束整体跳过；WHERE 连接、IN、
EXISTS 不生成关系，其子查询内的连接也不展开。跳过原因写入准备 trace。
该步骤不依赖 LLM，已删除 JOIN 提议模型、prompt 注册和 provider 分发。

共享关系合并按端点和列对去重并合并来源，忽略 SQL JOIN 类型；反向遍历
翻转列对和 `1:N` / `N:1`。未知基数不覆盖已知约束，互相矛盾的已知
基数明确报错。Graph Merge 同步重写嵌套列引用，并使用相同关系合并规则。
检索闭包包含完整连接键及其归属列，路径说明展示键和已知基数，不生成
默认 INNER/LEFT 等查询指令。`aligns_with` 的聚合对齐契约不属于此变更。

DimensionValue ID 统一根据查证后的存储值生成 SHA-256 摘要；列归属保留
在 ID 前缀中，值中的空白、标点和长文本不会被 slug 清洗折叠。SQL
字面量与存储值不同时（如整数 1 对应 '01'），原始字面量保存为经数据库
查证的 Value alias，保留精确大小写和空白；语义引用归一到存储值的 ID，
SQL 表达式保持原样。图校验和恢复使用同一份已查证 Value/alias 目录，
不生成第二个物理身份。重复查证按列和值缓存，来源逐 case 保留。
检索以节点类型和列归属识别重映射后的 ID，不依赖 table./column. 前缀。

这是物理图和维度值身份的格式升级。旧 `conditions/default_join_type`
关系、旧维度值资产和旧 case 快照应从数据库/QA 重新构建；旧关系在合并
入口明确拒绝，旧快照因 schema 指纹变化拒绝恢复，不静默混用两种格式。

## 2. 目录与接口

```text
sql_training/
  pipeline/
    train.py                    run 调度、独立 case 资源、失败报告与 Value 汇总
    physical.py                 Gold metadata、JOIN、bootstrap
    grounding/                  活跃共享的物理展开、SQL literal 和 Concept Value 查证
  training/
    workflow/
      state.py                  可序列化 CaseState、workflow version
      graph.py                  唯一节点图与路由
      nodes.py                  Direct/Repair/反馈/接受/收尾领域步骤
      runtime.py                同步运行、SQLite saver、writer 锁、恢复校验
    services.py                 稳定验证、精确 gate 缓存、Diagnosis 适配
    feedback.py                 独立 Assessment
    evidence.py                 不可变证据、完成节点回执、验证索引
    observability.py            原生 tracing 与业务证据展开
  learning/                     create_agent、hooks、session、tools、只读调查
  graph/                        完整快照、mutation、Value 合并、GraphSpace 投影
  models/ contracts/            领域 schema、身份/引用/表达式规则
  error_analysis/               有限 Diagnosis 修复/消融流程
  artifacts/                    workspace、正式结果与唯一语义导出
  llm/prompts/                  集中注册、角色 Markdown、共享领域规则
```

保留服务与领域层，删除已退休的 CompilePipeline、按节点类型 initial/repair/optimizer runtime、对应 provider 分发、extractor 和 prompts。共享 grounding 逻辑迁移而非复制。配置中的旧 extraction_max_workers/max_trial_attempts 已删除。

```python
pipeline.run(qa_path=..., dataset=...)
pipeline.resume_case(case_root)
```

resume_case 是单 case 恢复接口，不恢复整个 run 调度器。新的调用使用同一 case 的源输入和配置；不存在旧 checkpoint 格式兼容或备用 Controller。

## 3. 持久化布局

```text
train_runs/<run>/cases/<index>_<source>/
  case.json                       冻结 source、run_id、case_index
  checkpoints.sqlite              当前 case 唯一持久化流程状态
  .run.lock                       本地单 writer
  ontology/<revision>.json         不可变完整 authoring 图
  evidence/<id>.json               对话、工具、验证、反馈、接受和完成节点回执
  evidence/index.json              精确 gate 缓存和有限 infra 尝试
  result/
    result.json                   状态、版本、验证引用和输出路径
    ontology.json                 唯一正式语义资产列表
```

result 和报告中的 checkpoint_path 指向 SQLite，thread_id 标识唯一 case thread。完整历史图在 ontology 中；正式 ontology.json 只含最终语义资产 envelope，供 Merge 和实验工具统一读取。报告不再嵌入一份 draft_nodes。case.json 合并原来的两份输入文件；执行进度直接查询 checkpoint，不另写 progress.json。

普通证据使用随机 ID；已完成节点回执使用节点和完整输入的 SHA256 指纹作为 ID，两者共用证据读写接口。回执只保存输出字段变化，不保存流程位置。验证索引是证据目录中的查询缓存，不是执行状态。没有独立 operations 或 edits 目录，也没有旧格式回退读取路径。

## 4. 状态和重放约定

- 每个节点从 State 恢复 GraphStore W/L；State 不含连接、锁、模型或长期存活的服务对象。
- 节点通过 StateGraph 返回更新。下一执行位置由框架持久化。
- 已完成结果收据先于 checkpoint；重放相同输入返回相同结果，不能扫描最新文件决定 head。
- episode 内部暂不持久化；未完成 episode 从输入重新开始，新 session 使用空的内存编辑收据，模型 edit_id 不与旧尝试冲突。
- Assessment、筛选和接受各为明确节点。Direct/Repair 共同执行 assess → select → accept；select 改变图时走 validate_selected → assess，L 不变。accept 仅在匹配完整图的 SQL 稳定验证及全部组 Assessment 通过后推进 L，再保存接受及完成收据。
- semantic acceptance 已要求完整图 SQL 稳定正确与 Assessment 同时通过；final gate 核对最终 L，内容和 context 一致时精确复用同一证据。
- close_resources 在 seal 操作之前执行，ExitStack 在所有退出路径兜底。故障只生成失败报告，不覆盖已完成 result。

## 5. 验证方式

```bash
python -m pip install -r requirements.txt -e . -e ./baseline
python -m pip install -e '.[dev]'
python -m pytest -q
python -m pip check
python -m compileall -q src scripts ablation_study
```

恢复测试使用真实 SQLite saver、fresh GraphStore/runtime 和故障注入，覆盖接受结果与 checkpoint 间的崩溃、episode 中断、结果重放、单 writer 和不兼容恢复拒绝。既有完整图 schema/Value、并行读、SQL scope/read-only、m2/m3 回退、最终筛选验证、默认入口和 LangSmith 原生 I/O、usage、父子层级与凭据脱敏测试继续保留。仅测试旧 runtime 的用例随删除的实现退休。

本次迁移未重新运行真实模型数据集质量/泛化实验。已有真实模型 Direct/Assessment 小规模冒烟结果不是本次持久化和框架迁移的质量保证。

## 6. 历史实现验证（以下 6–13 节为 v4 前实验记录）

- 迁移基线全量 **76 项测试通过**；淘汰只测试已删除旧 runtime 的测试，同时增加真实 checkpoint 故障恢复测试。
- 三路 subagent review 覆盖恢复协议、集成/旧实现清理及独立故障注入。已修复 schema 恢复指纹、直接 workflow tracing、编辑 session 隔离和空 SQL 路由等问题。
- 实际业务图为 10 节点、18 条边，所有条件分支显式声明。
- 默认应用 → 真实 case LangGraph → Incoming → 真实 Graph Merge 串联通过，Learning 决策及 SQL generator 使用可控模型/验证器，执行比较使用真实 SQLite。
- pip check、编译、相关新模块 lint、训练/pre-merge/merge/evaluation 命令 help 入口和 diff whitespace 检查通过。
- 仍需真实模型数据集上的质量、耗时和成本评测；本次结果不等价于语义泛化质量保证。

## 7. 存储简化验证

- 全量 80 项测试通过，覆盖 session 内去重、跨 episode 重新开始、完成节点回执重放、正式输出完整性和消费者串联。
- workflow version 升为 case-graph-v2；旧布局不能直接恢复，不提供双格式兼容。
- 删除独立 operations.py；不再写 source.json、run_context.json、progress.json、磁盘编辑收据及重复语义导出。

## 8. 简化复查

三路 review 覆盖重复存储与消费者、接受回退、故障恢复。接受和回退协议保持不变；补上两个读取边界检查：已定位 case 目录的消费者从本地 result/ontology.json 读取，避免搬移 collection 后读取旧绝对路径；公开 resume 在返回成功前也验证正式导出的内容指纹，缺失或损坏时报运行故障，保留原 checkpoint 和 accepted revision。旧 checkpoint 在解析当前 State 之前检查原始版本标识，缺失版本或旧版本直接拒绝，不因默认值被误认成当前版本。没有新增状态层、存储文件或旧格式兼容。

验证证据已写、索引尚未写时的崩溃，通过读取当前 context 的完成验证证据补回内存索引；不重新抽样，不从证据恢复流程位置。已有完成结果冲突时拒绝，unavailable 仍按有限重试窗口处理，兼容性检查前不写磁盘。

本轮复查后全量 91 项测试通过；变更模块 lint 和 diff whitespace 检查通过。

## 9. 真实模型与 LangSmith 验证（2026-10-22）

california_schools 的 california-62 case 用 deepseek-flash 和真实数据库验证。Gold 结果为 1；误用存储的比例列时为 201。实验配置使用每 episode 20 次模型决策 / 600 秒，生产默认配置保持原值。

- 正常路径：Direct 10 次决策生成 6 个语义节点；3 次 SQL 验证正确，最终验证精确复用，封存 stable_correct / ready。
- 修复路径：同 case 使用已有 repair-only 入口。真实初始 SQL 错误 → Diagnosis → 16 次决策交付候选 → Trial 三次正确 → 独立 Assessment → 只接受 Metric → 筛选后的不同图重新三次验证 → 封存 stable_correct / ready，正式导出 1 个 Metric。
- 两次独立运行覆盖正常路径和修复路径，没有把正常已正确的 case 人为标错。修复前的失败尝试验证了非法编辑不会被接受。
- case 内 Diagnosis 本轮为 analysis_incomplete，但提供可用反馈；独立真实模型复核完成 1 轮最小修复与 1 次消融，得到 complete。原 case 证据未被复核结果覆盖。
- 本轮修复：非法工具 JSON 整批拒绝后有限重试；最后一次决策保留给显式 handoff；分页约束进入工具 schema；字段校验错误不重复整个大请求；不完整 Diagnosis 保留动作失败原因。
- 最初验证使用摘要 trace；该批历史 trace 不含原始输入输出。随后根据 review 恢复原生追踪，见下一节。
- 全量 95 项测试通过；此次真实验证截止 case 封存和 Incoming，不包含全局 Merge。

在本机 `.env` 配置 LANGSMITH_API_KEY、LANGSMITH_TRACING=true、LANGSMITH_PROJECT=autoonto-extract。运行前将 `.env` 导入当前进程；config loader 不会自动加载它。Tracing Only key 足以写入 trace，网页使用登录会话查看结果；不需要给训练程序读取工作区的权限。

```sh
set -a
source .env
set +a
.venv/bin/cortex sql-train --config <config> --training-set <qa-sql.jsonl> --output <output>
```

本地实测报告位于 workspace/california_case_live_20261022/review.md，原始结果和证据保留在对应实验目录中。


## 10. LangSmith 原生追踪与业务内容

开启 `LANGSMITH_TRACING=true`，配置现有连接变量。`infrastructure/observability.py` 统一提供原生 tracing context 和带凭据脱敏的 SDK client。关闭 tracing 时流程仍可本地运行。不要在图或 Agent 周围再设置 `enabled=False`。多 case 调度使用 SDK 的 `ContextThreadPoolExecutor`，每个 worker 继承独立的上下文副本，使 case 仍属于同一 run，并保留原有线程内资源归属与关闭规则。

追踪层级为 `extract.run → extract.case → extract.case_graph → 原生节点 → extract.<stage> → learning.direct/repair → 原生 model/tool`。原生节点展示 checkpoint State 的输入输出；`extract.<stage>` 展开本地引用指向的业务证据。这一层用于阅读实际内容，不是另一个执行器或状态机；不会向 checkpoint 增加图、SQL 或对话副本。

- prepare 输入包含 source IDs/case 数量，输出包含物理初始化统计；case 输入是 NLQ、gold SQL 和 source evidence，输出是最终 case result。
- stage 输入包含当前 State、语义节点及已有反馈；输出包含更新后的 State、语义节点和该阶段证据。物理节点仅计数，具体内容在按需工具输出中可见。
- validate 输出包含每次生成 SQL、执行比较和 stable replay 结果；Diagnosis、Assessment、accept 分别展示报告、逐组评判和接受成员/版本记录；seal 展示正式提交结果。
- 原生 LLM span 展示实际发送的 system/user/tool 消息、模型响应、tool calls、provider/model metadata 和 provider 返回的 usage。同步调度 adapter 转发 `_get_ls_params`，不增加嵌套模型调用。
- 框架统计每次模型调用及 trace 的 token、耗时、失败等；stage metadata 另外记录 Learning Agent 调用数、工具/数据库调用数、round/iteration、正确性、验证状态和 replay 次数。case metadata 包含终止原因、轮数、总 trial 数和保留/接受的语义节点数。stage 的 `validation_correct` 指向关联 gate 的判定；新接受图是否正确仍以最终验证为准。
- `replayed` 表示已完成节点从证据重放，重放时本次调用数为零；`validation_reused` 标记复用相同验证引用。原生模型 usage 不因缓存重放重复计费。模型未返回 usage 时不能伪造 token；自定义模型的费用显示依赖 LangSmith 的模型价格配置。

SDK egress anonymizer 保留业务内容和 usage，递归过滤凭据字段、已知 key 格式、Bearer、数据库 URL 中的用户名密码，以及当前环境中的凭据值。不会修改本地 State/证据。上传内容已从早期“仅摘要”调整为可读的完整模型/工具 I/O；业务数据可见是当前明确选择，凭据仍应过滤。旧 trace 不会回填，新运行才会包含这些信息。

自动化测试通过真实 StateGraph/create_agent、脚本模型和 recording client 验证 I/O、usage 与父子层级，并保留接受/回退/故障恢复测试。真实模型验证产物位于主目录 `workspace/california_case_live_20261022/runs-native-io/`，浏览器证明与核对说明见同目录 `review.md`。

实际原生追踪复核：repair-only 完成 Diagnosis complete → 修复（16 次模型决策、25 次工具、4 次数据库调查）→ Trial 三次正确 → Assessment 接受 1 组/2 节点 → 最终验证精确复用 → stable_correct。另一次 Direct 的非法工具 JSON 失败被真实记录，没有接受任何语义改动。全量 99 项和主目录相关 71 项通过。


## 11. Direct 工具 JSON 根因与编辑接口简化

从原始 LangSmith 模型 span 与本地原样保存的 invalid_tool_request 交叉核对，四次非法参数都在多节点 operations 的相邻元素处出现对象结束括号错位，响应 finish_reason 为 tool_calls，不能归因于输出上限截断。最初工具只暴露 `edit: dict`，operation/asset 的结构、必需 node_id 和完整节点结构未进入函数 schema；错误反馈也未提供 JSON 位置。后续检查还发现重复的 edit → operations → operation → asset 包装增加了构造出错点。

最终唯一的模型编辑接口为 `edit_graph(edit_id, expected_revision, upserts, revert_ids, proposed_groups)`。upserts 直接包含完整 NodeAsset。工具从 NodeAsset 自动生成精简语义 envelope schema；类型 properties 继续使用现有 guide 按需读取。WithJsonSchema 只调整模型可见 schema，实际仍使用完整 NodeAsset 验证，不复制属性验证规则或接受任意 properties。

代码根据当前 ID 是否存在，生成内部 GraphOperation(create/update)，并把 revert_ids 转成 revert；所有操作仍统一交给原有 GraphEdit/apply_edit 做版本、依赖、物理权限、组与原子发布验证。请求去重指纹基于模型原始规范化请求，不依赖重试时 ID 是否已存在，避免 create 重放变成 update 后误判。旧的模型 edit/operations 包装已从工具与调用方移除，没有增加兼容执行分支。

一次调用仍可原子发布多节点；推荐先依赖、后引用，只有必须一起合法化的变更才放同一个批次。JSON 非法时整批拒绝，反馈具体工具名、行列和短片段；不自动修补模型 JSON，不执行半个批次。错误上限、模型调用次数和 episode 时间预算均保持原值。接受、版本、checkpoint 和最终验证规则保持不变。

验证覆盖模型实际接收的 schema、历史括号错误的明确反馈、字段错误、多个节点原子增改/回退、失败批次无部分发布、重放去重、恢复与上下文边界。真实运行与失败的中间尝试保留在主目录 workspace/california_case_live_20261022，最终验证目录为 runs-direct-flat；详细核对记录写入该目录现有 review.md。

### edit_graph 简化后的多方面复核

三个 subagent 分别复核调用方/提示词、原子修改/版本/幂等、接受/回退/恢复。未发现可执行的旧工具调用；内部 GraphEdit.operations 和历史非法 JSON 负例有意保留。

发现并修复一个执行边界问题：LangChain 从函数生成的参数模型默认忽略未知字段，混合合法 upserts 和旧 edit/operations 或拼错的 revert_ids 时，可能静默只执行 upserts。现在只给 edit_graph 的参数模型设置 extra=forbid 并重建验证器，在执行前整批拒绝未知参数，不创建快照、receipt 或 groups。未引入兼容解析器、自定义工具子类或额外事务层。模型可见工具 schema 仍由 LangChain 生成，严格拒绝由运行时参数校验保证。

回归覆盖混合旧字段/拼错字段零发布，以及真实 Agent ToolNode 拒绝后用相同 edit_id 重新提交合法请求。其他验证确认多节点原子操作、稳定请求指纹、版本检查、旧 receipt 不恢复旧 head、最近接受快照回退及 checkpoint 恢复均保持原有行为。

本次复核后，隔离 worktree 全量测试 108 项通过；同步主目录后的抽取、可观测性、恢复及同步训练相关测试 96 项通过，Ruff 检查通过。

## 12. Prompt 职责与动态上下文分离

SystemMessage 只包含 Learning 的稳定职责、证据优先级、跨工具策略、Working/接受边界、working memory 原则和 Direct/Repair 阶段任务。参数结构由原生 LangChain 工具签名/schema 表达；工具 docstring 负责版本、幂等重放、原子编辑、物理权限、notes 和 handoff 语义。启动只提供类型职责、图的两层结构、写入与指南入口；定义、抽取边界、完整 schema、相关关系映射和 Direct registry 在所选类型 guide 中一次返回。系统和阶段 prompt 不再逐项重复工具参数、资产字段或旧包装格式。

每次模型请求在保留的对话前注入一条新的 HumanMessage，以 case_context 包含 source、当前 Working/accepted/baseline revision、语义节点概览、固定 targets、最新 feedback、working notes 和预算（含并行读上限）。该消息只修改本次模型请求，不写入 Agent 对话历史。上下文预算先扣除系统指令、工具 schema 和独立 context 消息，剩余空间用于历史；因此刷新不会堆积旧 context，历史裁剪也不能移除当前 case。原生模型 trace 可以查看完整的实际 system/context/tool 输入。

共享规则统一为当前 Working graph、semantic node、Learning Agent、Diagnosis 和 Assessment；移除固定 Candidate 集合、dependency_assets、退休 Extractor/Optimizer 以及旧的单次 Metric 输出契约。保留物理 grounding、Concept/Metric/Rule 所有权、依赖闭包、语言、Gold 实现证据和最近接受快照回退规则。领域契约版本更新为 v27，与 prompt 指纹一起进入既有恢复上下文检查。Diagnosis 和 Assessment 原本已将输入数据与系统指令分离，继续使用现有结构化输出和调用链。

新增回归检查 Direct/Repair 的系统与数据分离、当前版本和预算逐轮刷新、context 不累积进历史，以及裁剪旧历史/外置大工具结果后仍保留当前 context 和完整工具消息配对。

## 13. 真实模型发现的修复反馈路由问题

真实 california_schools 分支验证引出了路由复核。两次原始实验都只有一次 Trial 预算，实际停止符合上限，不能据此归因于提前退出；另用仍有剩余预算的回归用例确认了独立的反馈利用缺口：原 round_finished 优先依据 Trial SQL 正确或 targets resolved 退出当前 round，即使 Assessment 仍有未接受组。筛选和最终重验会正确阻止误报，但剩余的本轮预算没有用于落实明确的评审修改建议。现改为先检查迭代上限；未到上限且任一组不是 acceptable，则回到同一个 repair 节点，让 Learning 使用当前 Working、最近接受快照及原样评审反馈。没有新增状态、控制器或模型重试层。达到上限后的筛选、重新验证和封存保持原样。workflow_version 更新到 case-graph-v3，恢复时拒绝混用旧路由。

repair prompt 同时明确只修改固定 target 必需的知识、不要重抽整个 Question，并保留 Concept 对存储类别绑定的所有权。回归覆盖 Trial 连续正确且 targets resolved 时的 revise/reject/inconclusive 三种反馈，确认下一 episode 收到反馈、修正后在同一 round 接受完成；已有上限、部分接受、回退和恢复测试继续适用。

## 14. 统一接受门槛（case-graph-v4，当前规则）

当前以 extract-case-agent-loop.md 第 4、8、9 节为准，替代历史记录中 Direct 先保留、失败 SQL 也可接受部分组、筛选后才检查正确性的旧行为。GraphStore 删除 retain_direct/promote 入口，select 只构造候选不推进指针。初始图只能包含物理事实，语义不能从初始化绕过接受；低层测试需要显式建立既有接受快照。

accept 对匹配的 B/L/Trial/验证/分组/目标执行检查；SQL 必须 stable_correct，全部变更组必须 acceptable，才将当前 W 设为 L。接受证据记录两个 gate 的布尔值及精确 revision、SQL/Assessment 引用，未通过记录不包含接受成员。封存前核对 L 的双重通过凭据及完整图 SQL 指纹。达到修复上限也不能导出 tentative semantic nodes 或 relations。

select 投影一个合法的合格真子集后，重取该完整图的 SQL 证据并再次 Assessment；这属于验证工作，不增加 Learning 修复预算。select、validate_selected、assess、accept 均沿用不可变节点收据和同步 SQLite checkpoint，恢复复用已完成的操作，不重复推进 L。workflow_version 升级后拒绝恢复旧接受语义的 checkpoint，避免混用两套规则。

retain 保留 L 语义和已查证物理事实，但只更新 W；最终验证与正式语义导出针对 L。物理事实增量在 seal 单独汇总，不能改变 L 的已证明内容。无需新增存储目录、状态机层或兼容 runtime。

当前回归：158 项测试通过。新增/更新覆盖 Direct handoff 和稳定 SQL 之后仍不得提前接受，Direct/Repair 的 revise/reject/inconclusive 反馈，SQL 失败但评审合格且达到上限，筛选后 SQL 与 Assessment 的四种组合，错误版本/证据拒绝，筛选/重验/评审边界恢复，以及接受完成收据写入后的崩溃重放。

真实模型验证还暴露并修复了两个反馈/episode 边界问题：Metric 占位符错误现在明确提示 `${key}`，不再让模型猜测 `{key}` 或 `<key>`；无进展按完整模型/工具批次计数，而非逐个并行读调用计数。阈值仍为连续三次无进展，模型调用、错误、时间和修复预算未增加。新增回归覆盖一个并行重读批次后仍可正常编辑和 handoff、连续三次重复决策仍停止，以及错误 Metric 不发布任何图版本。

### 统一门槛的真实模型验证（2026-10-22/23）

使用真实 california_schools 数据库、deepseek-v4-flash 和原配置 3 rounds / 3 trials / 连续 3 次正确 SQL。复杂 california-62 的独立 Repair 分支沿用生产 TrainPipeline，仅以 skip_direct 进入初始验证，没有替代控制器：初始 SQL 失败 → Diagnosis → Repair → Trial 稳定正确 → Assessment 部分 revise → 筛选 → 新 SQL 失败及新 Assessment → 不推进 accepted → 第二次 Repair → Trial 三次正确且全部 Assessment acceptable → accepted 精确推进到 Trial → 最终 gate 复用 → seal stable_correct。最终导出 2 Concepts、1 Metric、1 Generation Rule。

真实普通 Direct 尝试也覆盖 SQL 三次正确但 Assessment revise、筛选后 SQL 失败却 Assessment acceptable 的拒绝路径，accepted 均未推进；部分尝试因模型编辑错误或无进展而结束，产物为空。不能把这些尝试描述为正常 Direct 成功。所有记录保留在 workspace/california_unified_acceptance_live；verification.json 检查每次推进都有匹配的完整图 SQL 和 Assessment 凭据，未推进不含接受成员。

成功 Repair trace：https://smith.langchain.com/o/7844122d-c062-5314-a978-b1e9a7dbc093/a/autoonto-extract/projects/p/9b344917-7c5c-40d2-9550-e75df90c1b8a/trace/01a1644c-6cf1-7178-9b46-55f9489cb1b8/run/01a1644c-6cf1-7178-9b46-55f9489cb1b8 。远端 extract.accept 输出已复制核对两个 gate 均 true，接受版本与 Trial 一致；截图为同目录 langsmith-accepted.png。

最终代码另用同一 california_schools 数据库的完整简单 case（source_id=california-count-schools，Question: How many schools are there?，Gold: SELECT COUNT(*) FROM schools）验证 Direct 起始路径：Direct → SQL 三次正确 → Assessment revise → 接受拒绝 → Repair → SQL 三次正确但再次 revise → 接受拒绝 → 第二次 Repair → SQL 与 Assessment 同时通过 → 接受唯一 count Rule → 最终证据精确复用 → stable_correct。使用相同生产配置，无注入模型答案或改写验证结论。该运行同时覆盖修复后的批次无进展计数与最新紧凑反馈。trace_id=01a16459-a236-74ff-b411-72fa8355bd00，产物位于 workspace/california_unified_acceptance_live/direct-smoke。复杂原 case 的普通 Direct 尝试仍有模型编辑质量失败，记录保留且未接受语义，不把 Repair 成功等同于所有 Direct 尝试都成功。

## 15. 最小启动输入与按类型披露（2026-10-23）

之前的方案把三类指导、四个完整 schema、全关系及其字段约束一次注入，约 31 KB；虽然用了 Markdown，仍然没有突出任务与交付。现改为一个 Learning Agent + 三份按需类型指南。每次 system 只包含任务背景、图模型、阶段步骤、按需指南和交付说明。删除独立 Shared extraction rules 层，移除全量 contract 渲染；这次替换原方案，不保留两套运行方式。

交付合同：从 Question–Gold SQL 抽取有据、可复用的语义知识，通过 edit_graph.upserts 提交完整节点，再 handoff。输出不是 SQL，也不是 assistant 正文里的另一套 graph JSON。启动提供三类一句话定义，允许某类为空，附完整 handoff 参数例子。Question 确定含义，Gold 确定实现；当前阈值、K、报告日期保留为运行时输入。

| 内容 | 何时提供 | 原因 |
| --- | --- | --- |
| 当前任务、三类职责、交付方式、工具入口 | 启动及后续请求 | 必须知道要做什么、交什么、如何取得详情 |
| Question、Gold、必要 evidence、database dialect、Working revision、语义概览、相关 Gold 物理绑定 | 独立 case context，每次刷新 | 支持判断与精确引用，不依赖全图/数据库扫描 |
| 某类定义、必需字段、完整节点例子、精确 properties schema、相关 refs 关系 | 准备写该类型时读取其 guide | 避免不需要的类型占用上下文；一次返回即可开始写作 |
| Direct registry | Rule guide | 与 Concept/Metric 无关 |
| 具体节点定义、依赖或数据库事实 | 有明确缺口时 | 限定调查对象，保留 Agent 自主性 |
| 长篇例外和更多 registry 示例 | guide_section=contract/taxonomy 或 evidence 引用 | 只在具体歧义下阅读 |

三份 learning/types/*.md 分别管理该类抽取语义与一个完整可验证例子；learning/prompt_rendering.py 只装配一个类型的返回，不重复拼接全局材料。schema 继续来自验证模型，refs 关系来自当前 ontology。恢复指纹覆盖每类最终 guide 返回内容，包含 Markdown、schema 和组装结果。示例必须经过真实 edit_graph 测试。指南已经在当前对话中时可以复用；历史被裁剪、缺少字段依据时重新读取，不增加持久化的“已读指南”状态。

## 16. Learning 的统一图写入合同

Learning 通过同一个 edit_graph 原子提交三种内容：upserts 写完整 semantic nodes；values 提交 column_id/value，由代码查证、生成并返回 Value ID；edge_operations 显式 create_edge/update_edge/delete_edge。Tables/Columns 和 catalog 关系保持物理事实约束。Value 与显式边都已进入 snapshot、fingerprint、graph_diff、Assessment、筛选、接受、回退和恢复，不增加第二个 writer 或第二套提交状态机。

启动说明物理层 Table、Column、Value 的关系与三个类型指南入口；详细规则按需提供。读取类型指南同时得到 reference_relations：具体 refs 字段、目标类型、默认关系名和必要边属性。映射与关系编译器共享 reference_relation_type，避免 prompt 与实际生成规则漂移。Concept targets[].references → Column/Value 生成带 target_id 的 grounded_on；Metric 到 Metric 生成 depends_on，到物理对象生成 grounded_on；Rule 到 Concept/Metric 生成 applies_to，到物理对象生成 grounded_on；Value 到所属 Column 生成 value_of。SQL trigger columns 不生成依赖边。

显式关系是通用写入能力，refs 是自动生成默认关系的便捷输入。同一 source/target（Concept 另含 target_id）anchor 的显式边替代默认边；删除 override 后，anchor 仍存在则恢复默认。独立显式事实不应反向塞入公式或绑定 refs。GraphManager 校验端点、字段、重复关系、物理归属与依赖循环等结构约束；语义证据与合理性由 Assessment 审核，不静默保存结构冲突。

生成 Direct Rule 前读取 inspect_graph(mode="guide", node_type="generation_rule")。返回内容已包含 direct_rule_registry，必须从中选择 operation_intent，不存在额外的必需 registry 调用。只有需要更详细的操作例子时才读取 guide_section="taxonomy"。Value 与边的指南分别由 dimension_value guide 和 mode="relations" 提供，避免默认注入全图关系 schema。

## 17. 单 Agent 与分类型 Agent 的选择

| 选择 | 收益 | 成本 |
| --- | --- | --- |
| 一个 Agent + 按需类型指南（当前默认） | 共享 case、Working 和 memory，能判断跨类型归属与原子修改 | 需要清楚的类型入口和输出合同 |
| 三个独立 Agent | 每个 prompt/schema 更窄，可按类型调优 | 重复读 case，可能重复或遗漏同一含义，增加依赖与修改协调 |
| 三个 typed extractor，统一 writer | 首次抽取可隔离各类输出与评测，较独立写图 Agent 简单 | 固定调用成本，仍需统一 ownership 与依赖汇总 |

先采用第一项；“按类型组织指引”不等于“按类型运行 Agent”。只有在相同模型、预算与标注样本上比较后，证明分类型提取显著提升质量，才替换运行策略。不要为缩短 prompt 引入三方写图和新的状态协调。

## 18. 验收标准与尚未证实的质量目标

- 入图格式、必需字段和图合法性：所有成功编辑都必须通过验证；失败原子拒绝。测试覆盖三类完整例子、未知参数、非法字段、版本、接受与恢复。
- 首次工具调用合法率：分别记录非法 JSON、参数形状、缺必需字段与引用错误，不用重试后的成功掩盖。以 100% 为验收目标，prompt 和工具设计都承担责任；提示词本身不能保证生成模型永不出错。Direct 新增 Question trigger 必须来自原文连续短语（只忽略大小写/空白），由编辑入口确定性检查；同 ID accepted Rule 的历史短语允许保留，Repair 不套此限制，失败整批拒绝。
- 语义质量：按 Concept 绑定、Metric 公式/计划、Rule 条件/行为/理由分别评 precision、recall 与关键字段正确性；独立报告 Direct 和 Repair 后结果。SQL 正确和 Assessment 通过不是人工抽取正确率的替代指标。

80–90% 是待评测目标，不是已取得的结果。先用 20–30 个有人工预期的开发 case 覆盖 stored/calculated average、COUNT、稳定类别、运行时阈值、多表计算与 ties 等边界；再用独立留出 case 评估。没有人工 gold 或有争议的类型归属时，先记录为待裁定，不用模型自评冒充 gold。当前真实 case 仅用于流程与输入合同验证。

## 19. 任务主线与 Value 先查图复用（2026-10-23）

Learning 启动正文按任务背景、图模型、阶段步骤、按需指南、交付组织。先交代图服务未来 NLQ→SQL 的目标：从给定 NLQ、参考 SQL 和 evidence 积累全部有据、可复用的语义知识，建立其与物理事实和其他知识的联系。物理层为 Table/Column/Value，语义层为 Concept/Metric/Generation Rule；仅枚举类型不足以定义任务。

共同 system Markdown 的阶段槽位插入 Direct 或 Repair 步骤，避免在背景之前先讲操作。Direct 的默认路线为对齐含义与实现、判断类型并复用知识、补齐物理绑定与 Value、构建节点与关系、检查覆盖并交付；可以跳过无关调查并按缺口往返。Repair 保持固定 targets/Assessment 的范围，不重抽整个 case。

原生 tools 继续单独注入参数与协议；启动正文删除完整 handoff JSON 和 conflict CRUD 字段教程，只保留工具使用的时机与交付语义。完整 schema、例子、refs 映射、Rule registry 和 Value 操作留在对应指南。

Value guide 先检查图：已知精确 Value ID/owner/typed scalar 时直接复用，否则读取所属 Column 的完整节点与双向 relations，定位并读取候选 Value，核对 owner、存储值与类型。相关结果不完整时不能判定缺失。确认没有匹配节点才提交 values 提案；仅对不明的编码、类型或拼写主动查数据库，写入服务仍负责必要验证。复用现有 inspect_graph/GraphManager 接口，不新增 lookup 或第二套查询/写入逻辑。

真实验证发现 Assessment 原先只收到 explicit_edges，可能把已由 refs 生成的关系误判为缺失。适配层现在从 Trial 的已保存 graph.edges 读取当前语义知识及 Value 的出边，并保留全部显式边；同时提供相关关系定义，不注入无关物理拓扑，不重新推导关系。Assessment 指南明确默认边与显式边均为关系证据；该 prompt 变化进入既有恢复/评审指纹。另修正最后一次模型决策的工具选择：仅暴露 handoff 并使用 auto，兼容拒绝强制 named-tool 的推理模型；模型未交付仍按既有资源上限退出，不合成成功，也不增加预算或重试。

真实验证还发现 Concept 规范化只使用自动发现数值类别的启发式，会删除 Agent 已经查证并纳入图中的数值 Value 引用。规范化现接收 Working 中已有的 Value catalog；SQL 谓词精确匹配已有 Value 时保留其引用。未查证数值仍遵循原有自动物化限制，GraphManager 接口和结构校验保持不变。

验证：全量 442 项测试通过。最终真实 california-62 case 的 Direct 交付成功，无非法工具 JSON；Charter=0 Value 与 Concept 绑定及默认边均保留，SQL 连续三次正确且 Assessment acceptable 后才接受。final_revision 与 accepted_revision 相同，最终内容指纹与验证指纹一致。另一个真实 County Value 复用 case 经 Assessment revise、Repair 和重新评审后通过。实验完成 case 提交，未执行全局 merge；记录见 workspace/task_graph_prompt_review/verification.json。

## 20. 收紧 Learning case context（2026-10-24）

删除 graph_info：schema 版本、节点总数和物理管理策略不参与 Agent 的当前决策；稳定图模型仍由 system prompt 解释。source 仅传 question、sql 和存在的 evidence，不再将内部 record（含 gold_column_metadata、路由路径等）整体注入。内部数据保留给 Diagnosis 和控制器。

Gold bindings 只包含当前 SQL 使用的表、列与相关已知 join。Column 保留精确 ID、所属 Table ID、SQL 列名、类型和非空描述，不重复 Table 名称，不默认提供样例值、nullable 等详情；需要时读取节点或数据库。

semantic_nodes 是发现索引，不是全量图：按 Gold 物理绑定与名称/alias 在 NLQ 中的匹配选出最多 20 条 ID/type/name。实际关系边和 refs 均参与物理绑定匹配。本轮未接受修改及 episode 变更组内的节点额外保留，避免上限隐藏当前工作。未列出不能判定不存在；可通过已有 node、dependencies 和按类型分页的 nodes 接口调查，不新增检索服务或第二套图管理。

仅 Repair 注入非空 targets/latest_feedback；working notes 只传非空字段；baseline_revision 不再暴露给 Learning。working_revision、accepted_revision、dialect 和预算仍保留以支持正确调用与资源控制。该调整不改变接受门槛、图存储、证据存储或回退规则；prompt 变更通过既有 context_id 指纹使旧 checkpoint 失效。

验证：445 项测试通过。使用此前真实 california-62 的初始快照重新构建输入，完整 context 为 2,171 字符；source 从 2,415 缩为 621 字符，物理绑定从 1,522 缩为 1,227 字符。此次未重新调用真实模型；以上是输入构建与回归测试结果。

后续真实模型验证：以历史已验收的 Los Angeles Concept 与物理事实为测试 baseline，运行未修改的生产 CaseWorkflow；这不代表常规 train 已支持加载历史语义图，常规初始化仍仅含物理节点。初始 context 为 2,262 字符，LangSmith 中确认 system、独立 context、native tools 分开传入。Agent 按需读取旧 Concept、Charter Column 和 Value，未重复写入旧知识或 Value，也无需数据库调查。首次实验暴露 SQL 名称与节点 ID 混淆，system 现明确 refs/端点复制节点 ID，SQL 表达式使用 table_name/column_name，补充特殊列名测试；58 项相关测试通过。修正后的 Direct 9 次模型调用，随后经两次 Repair，最终 SQL 三次正确且 Assessment acceptable 后接受提交；原 Concept/Value 保持不变，验证和评审版本匹配最终版本。Repair 中一次 sources:null 参数被原子拒绝后由模型改正，没有放宽工具校验或增加重试。记录见 workspace/compact_context_live_20261024/verified/verification.json；未执行全局 merge。

## 21. 独立 Assessment checklist 与证据范围（2026-10-24）

Assessment 不再拼接旧版完整抽取合同。system 只包含职责、证据解释、Concept/Metric/Rule/Value/关系的验收 checklist、决策规则和自动生成的 AssessmentFeedback schema；保留 SQL 与 Assessment 双门槛，以及同版本证据要求。正文按 Markdown 管理，每段不手动折行。

动态 source 与 Learning 共用 question/sql/evidence 白名单。当前知识从变更组、实际变更及各次 SQL 使用的 IDs 出发，沿相关语义依赖双向展开以检查依赖与回归；纳入物理端点但不从 Table/Column 展开整个 catalog。旧定义和被删除/修改的旧边仍提供；baseline 与 accepted 相同则显式标记并复用 prior 内容。节点和边移除代码维护的 provenance；保留每次 SQL、应用 IDs 和结果摘要，删除 raw retrieval IDs。超过上下文预算仍失败关闭，不静默截断。

使用上次真实 case 的三个快照重建实际消息，不调用真实模型也不重新运行接受流程。第一轮 system 从 37,792 缩为 5,912 字符，动态输入从 20,676 缩为 17,550 字符，合计减少约 60%。完整渲染消息位于 workspace/assessment_prompt_review/，供 review；历史 verdict 仅作为测试响应，不是新 prompt 的质量验证。全量 449 项测试通过，另覆盖无关知识排除、依赖及依赖方、旧边删除证据和不同 baseline/accepted 的保留。

## 22. Assessment 独立评估协议（2026-10-24，替代第 21 节的单一 verdict）

Assessment 的任务是评估候选知识及其在 SQL 生成中的效果，并向 Learning Agent 提供具体缺口。模型不直接输出 group verdict。training/assessment_models.py 定义唯一结果协议：unit_results 分别包含标量 quality、汇总 application 和 kind/location/gap/required_change/preserve 反馈；target_results 汇总判断固定错误解决并提供 feedback；regressions 区分退化与可证明的归因；coupled_unit_ids 表达不可分的候选关系。旧 group_results/merge_groups 协议已移除，无兼容解析器。

评估单元以变更节点或独立关系的 ID 标识。输入 units 将当前候选、最近接受的旧定义、目标关联和逐次阶段事实放在一起；相关知识、保存关系及必要物理 metadata 单独提供，避免复制每个单元的完整子图。原始检索 ID 列表在代码计算 raw_retrieved/selected/included_via 后移除；依赖引入按已保存 dependency edges 从选中根展开，并要求相应节点/边确实传入生成上下文；不从任意关联边或完整图的连通性推断。实际上下文存在性和选中事实分别记录。未知事实保留为 null。baseline 与最近 accepted 不同时才补充旧 round_baseline。优先复用已有 prior 验证证据，不额外调用 SQL 生成或数据库来拼评审上下文。

质量与应用独立：合法知识可以未检索、未选中或未应用，错误知识也可能碰巧伴随正确 SQL。目标解决按局部正确行为而非整体执行成功判断；回归必须引用真实 prior_evidence，归因不明不得指责任意候选。Value 的评审对象是其在语义绑定中的角色；数据库存在性、规范 ID 和 schema 仍由代码负责。正确行为、已观察错误与 reason 因果解释使用不同证据，不要求当前正确 SQL 重复历史错误。

控制器检查所有 unit/target 恰好覆盖一次，汇总 applied 声明必须在每次验证中有实际上下文包含事实；删除单元才允许 not_applicable。未通过的质量、应用、目标以及回归必须有具体 feedback；字段问题和所需语义修正放入 gap/required_change，不另外重复 evidence/defects/exceptions。按 unit eligibility、group、coupling 和目标状态计算接受资格，再与现有同版本 stable SQL gate 联合。相关回归、归因不明或证据不足不推进接受。Repair 收到这些独立结果、确定性 stage_facts 及紧凑逐次 SQL 证据，由 Agent 决定如何调查和改图，不恢复旧框架的固定 owner 路由。

assessment policy 升至 v3，并将 prompt 与响应 schema 的指纹纳入恢复 context，拒绝旧协议 checkpoint。验证覆盖未选中但质量正确、部分应用、逐次覆盖、依赖引入、删除、目标独立性、回归归因和旧协议拒绝。完整 prompt、三份历史快照重建输入及新真实模型反馈见 workspace/assessment_task_v2_review/；该真实运行只重评历史最终快照，不重跑完整 train、不提交或 merge 新图。

验证记录：一轮全量回归 456 项通过；后续响应 schema 恢复检查、实际上下文证明与 prior 证据复用专项均通过。最终真实模型重评使用上次最终快照，首次返回合法新协议，3 个单元分别为 valid 且各 3 次 applied，无未解决目标或回归；未重跑完整 train 或提交新图。实际请求和反馈位于 workspace/assessment_task_v2_review/final/。

当前 Assessment 只输出汇总结论：质量一次评估，应用/目标检查全部 SQL 后各输出一个状态，任何失败或不确定都不能汇总通过。问题统一反馈，重复问题合并；通过项不附理由。Regression 仅保留 prior_evidence_ref、归因信息和问题 feedback，不额外复制证据文本。旧逐次结果协议直接移除，响应 schema 与 prompt 指纹使旧 checkpoint 不能混用。


### Metric 原子性与 Direct/Repair Rule 边界（当前约束）

Metric 表达 NLQ 中需要计算才能实现、不能再拆成“查询操作＋已有完整 Concept/Metric”的最小语义单元。它不是所有 SQL 计算结果的统称，也不按运算符数量拆分。共同定义同一业务量的多步公式保留；对完整量执行当前请求的 AVG/COUNT/排序/筛选不因此产生组合 Metric。差值可以是 Metric，差值的当前群体平均值由查询操作承担。定义共用 asset_contract.METRIC_ATOMICITY，由类型指南、详细 contract 和 Assessment 同步引用。

Direct 的 operation registry 和 taxonomy 不包含 joinability；普通单跳或仅沿现有边展开的多跳连接，复用抽取前的 joins_with 事实。写入端拒绝 Direct 的 joinability Rule。Repair 保留连接决策能力，用于真实错误支持的键/路径选择、桥表语义或粒度约束；不硬编码禁止所有连接知识。Registry 增加 population_average_comparison，表达对完整量计算同群体均值后比较，不建立 average helper Metric，并允许等价 CTE/window 实现。

实际错误驱动的纠错 Rule 与正确绑定 Concept 可以共存：Concept 保存语义绑定；Rule 保存实际错误所证明的错误替代、正确选择和适用条件。DOCType 被误用为 DOC 的错误可产生纠错 Rule，不因两个节点引用相同 Concept/Column 就删除；条件不应绑定“同时输出学校名称”等无关细节，也不将所有 DOC type 措辞都推广为编码请求。Direct 不预先制造这种纠错 Rule。

References 仅保存定义直接且共同需要的依赖。Rule 优先引用完整语义操作对象，只有自己承担独立物理选择时才另引 Column/Table；不展开所有可达依赖，也不为扩大检索添加引用。Concept 的 Column+Value 完整谓词和 Metric 的公式物理依赖不做机械传递裁边。GraphManager 的 refs 默认边、显式覆盖、版本与冲突检查不变。

Assessment quality 增加原子性、重复事实、case 特例和依赖必要性检查，同时明确保留实际错误支持的纠错 Rule。SQL 正确或 applied 不证明内容设计合理。语义边界由指南与独立评估判断，不按 AVG/SUM 关键字硬编码拒绝；控制器继续要求同版本 SQL 稳定验证和 Assessment 通过。领域合同升至 v28；既有 prompt/guide/registry 指纹拒绝混用旧 checkpoint，不增加新状态或另一套实现。

真实验证补充：历史验证曾使用 bracket quoting；当前 SQL 表达式统一采用方言序列化，仅保留必要标识符引号。含空格列名需要引用，避免模型在 JSON 参数中重复转义双引号；不做宽松 JSON 修复，也不增加重试。Repair 对已确认的投影绑定目标优先使用 Gold bindings 与精确 Column 查询，不重新扫描整表或重跑 Diagnosis；context.budget 显示 repeated_read_decisions_left，沿用三次停滞上限。通用 same-population Rule 不把本 case 的类别 Concept 作为永久依赖，除非自身条件确实限定该类别。

本轮验证：466 项全量测试通过。旧真实图重评明确拒绝 metric.average_enrollment_difference 与普通 join Rule，保留实际错误驱动的 DOC Rule。新 california-28 经 Direct → 三个实际绑定错误的 Diagnosis → 一次 Repair → SQL 连续三次正确 → Assessment 五个单元 valid/applied、三个目标 resolved → 接受和封存。历史语义产物为 1 Concept、1 Metric、3 Rule；后来核查确认其中缺少明确命名的输入和输出 Concept，不能用纠错 Rule 代替绑定知识。该产物不作为完整抽取的验收基准。新图无 average helper Metric 或普通 join Rule，同群体比较 Rule 仅引用差值 Metric；最终 SQL 使用等价 CTE。新过程有一次非法 JSON 批次被既有校验拒绝并由模型改正，没有发布非法图版本。接受快照、验证指纹与评审修订匹配。产物见 workspace/atomic_extraction_live_20261024/verified/，仅完成 case 封存，未执行全局 Graph Merge。


### Rule、原始 metadata 与 Concept 表达式（当前规范）

Generation Rule 表达一个原子的 SQL 生成决策。可独立成立的操作意图分开；共同完成不可分割决策的多个操作保留。当前请求值、无关输出与固定行为分离，只有决策自身需要的固定事实才持久化。Registry 匹配只说明允许抽取，不要求逐 SQL 操作生成 Rule。Direct 不能假设发生过错误；Repair 根据真实错误学习纠错行为。Concept、Metric 定义与纠错行为各司其职。规则原子性与 metadata 支持同时进入现有 Assessment quality，不增加状态或新的评估协议。

`database.description_path` 是可选外部 CSV metadata 目录，对 SQLite/MySQL 配置均可使用，并按配置文件路径规则解析。复用现有 DatabaseDescriptionLoader，在构建物理 catalog 时按真实表/列名匹配规范节点。数据库 comment 保留在 Column description，外部 business_name/description/data_format/value_description 保存在 Column source_metadata，按来源区分，不覆盖 comment。不存在 metadata 时正常运行；已配置但目录不可读时失败，不静默忽略。冻结的 prepared catalog 使用自身已有 metadata。

启动输入只包含 Gold 实际涉及的字段及其来源释义，简短描述最多 400 字符，截断时明确提示通过现有 inspect_graph 节点读取完整内容。长值编码说明只在节点读取时披露。Assessment 的相关 physical_context 复用同一物理节点 metadata，不把整个数据库目录注入。业务描述必须受证据支持；缺少释义或来源冲突时按需读取，不猜测缩写。Aliases 不属于本次改造范围。

Concept target 的 sql_expression 是完整的源内物理绑定 SQL 表达式，不是完整查询、图 ID 或模板 token。字段使用真实 table.column；类别使用完整谓词；允许有证据支持的局部表示转换。references 精确列出所用物理节点 ID。复用现有语义绑定解析与验证路径，均以 SELECT 投影表达式解析；规范化按连接器的数据库方言序列化，序列化保留字段拼写、必要引用和字面量内容，删除多余标识符引号；不引入新的表达式语言或第二套实现。Metric plan 的 <table.column> 仍只是计划文本标记。


本版真实验证记录：workspace/rule_metadata_live_20261024/complete/。最终 california-28 Direct 提交 5 Concept、1 Metric、1 Rule，SQL 三次正确，Assessment 全部通过，accepted 与 seal 的版本/指纹匹配。DOC 原始 metadata 已同时进入 Learning 和 Assessment，描述及 SQL 绑定正确；未执行全局合并。本次最终运行没有触发 Repair；此前实际 DOC 错误验证过 Concept 与纠错 Rule 共存。仍存在模型质量局限：比较 Rule 固定了当前类别，Assessment 未报告；另有两次非法工具 JSON 被拒绝并由模型修正。此记录不作为抽取语义全面正确或工具首调全成功的证明。全量 479 项测试通过。


### Assessment 输入压缩（v4）

完整图快照与执行证据仍保存在既有 evidence 存储，压缩只发生在 assessment_inputs / assessment_views 的模型输入投影。refs 和全部相关自动边仍同时提供，不做关系去重；当前边、历史边及变更边统一为 id/type/from/to/可选 properties，保留稳定 ID 与有语义的属性。关系定义仅保留 type 与 description，不注入写入权限、字段 schema 或管理规则。

每个 unit 的 context_facts 将内容相同的检索/选择/上下文包含事实合并，通过 attempt_ids 标注适用验证。输入必须恰好覆盖所有 attempt，重复、缺失、未知或空分组被拒绝。任一验证缺少实际包含证据都不能声明全部 applied。显式 absence 仍覆盖 selected 或依赖引入；不改变输出协议、group/coupling、SQL 稳定门槛或接受逻辑。Repair 使用同一分组事实，不保留旧协议分支。policy v4 与 prompt 指纹拒绝旧 checkpoint 混用。

保留每次完整 SQL；执行成功且结果相等时只提供 execution_ok/result_equal，不重复 Gold/预测结果样例。失败保留现有 reason 与可用结果差异；执行状态无法确定时用 null，不推断成失败或成功。字段采用明确白名单，避免新增内部字段自动泄漏。完整结果仍可从证据文件核验。

物理输入仅提供相关节点的名称、类型、必要约束与原始业务释义。只有相关 Value/类别谓词的 owner Column 才携带完整值编码说明；无可靠结构的原文不做猜测性截句。语义节点定义不截断，仅去掉空展示字段与 sources；新增单元不重复传 accepted_before:null，删除单元保留 candidate:null。Aliases 只在输入中省略空数组，不改变图内内容或策略。System 中 response schema 本轮不变。


v4 验证记录（2026-10-25）：全量 487 项测试通过。对同一历史图与三次验证证据重新投影，紧凑 JSON 从 19,741 字符降至 13,442（减少 31.9%），14 条边全部保留。新 california-28 实际 User Message 为 16,007 字符，含 9 个评估单元（7 节点、2 显式边）、17 条紧凑边；每个单元以一个 context_facts 分组覆盖三次验证。SQL 三次正确、Assessment 九个单元全部通过，v4 接受记录与封存版本一致；未执行全局合并。本轮 Direct 已通过，没有触发 Repair。完整输入和输出见 workspace/assessment_compact_live_20261025/actual-assessment-user.json 与 actual-assessment-feedback.json；完整执行证据仍在 case evidence 目录。

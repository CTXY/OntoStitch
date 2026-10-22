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
    observability.py            显式安全摘要 spans
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
- Assessment 和 accept 分为两个节点。接受记录先引用已保存评审和完整新图，然后保存节点结果。
- final gate 精确匹配整个最终图；semantic accept 不代表 SQL correct。
- close_resources 在 seal 操作之前执行，ExitStack 在所有退出路径兜底。故障只生成失败报告，不覆盖已完成 result。

## 5. 验证方式

```bash
python -m pip install -r requirements.txt -e . -e ./baseline
python -m pip install -e '.[dev]'
python -m pytest -q
python -m pip check
python -m compileall -q src scripts ablation_study
```

恢复测试使用真实 SQLite saver、fresh GraphStore/runtime 和故障注入，覆盖接受结果与 checkpoint 间的崩溃、episode 中断、结果重放、单 writer 和不兼容恢复拒绝。既有完整图 schema/Value、并行读、SQL scope/read-only、m2/m3 回退、最终筛选验证、默认入口和 LangSmith 内容隔离测试继续保留。仅测试旧 runtime 的用例随删除的实现退休。

本次迁移未重新运行真实模型数据集质量/泛化实验。已有真实模型 Direct/Assessment 小规模冒烟结果不是本次持久化和框架迁移的质量保证。

## 6. 本轮实现验证

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

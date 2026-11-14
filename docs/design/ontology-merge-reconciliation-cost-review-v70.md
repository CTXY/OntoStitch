# v70 Reconciliation 调用成本复盘（已暂停）

## 状态与证据

用户要求暂停后，已向运行进程发送 SIGINT，退出码 130，确认没有后台 merge 进程。保存 workspace、model_calls、agent_traces 和 usage_report；未发布最终 graph，不启动新的模型测试，不修改算法代码。

冻结代码：13f286f。四个输入及 evidence 与 v69 的哈希相同。matched-node 核心 _run_task AST 未改。v70/r5 的修复轮数上限为 5，旧基线为 3；不能把这项变化称为优化收益。

|范围|v69 已完成基线|v70/r5 暂停时|
|---|---:|---:|
|完整请求|65|71（70 个响应）|
|matched + identity|17|11|
|Reconciliation 含 Assessment|48|60（59 个响应，未完成）|
|Reconciliation 输入 tokens|2,077,611|1,671,729|
|Reconciliation 输出 tokens|313,094|348,078|
|其中 reasoning tokens（包含在输出中）|275,413|318,116|

暂停的在途请求未返回 usage，不能视为免费。输入暂时较低不是成功降本证据：流程未完成，输出已高于基线。此前失败试验及续跑 247 次请求，本轮 71 次，共 318 次；已返回的输入共 12,405,733、输出 2,065,102 tokens，另有中断请求的未知费用。

## 60 次调用的两种分解（不能互相相加）

按响应行为精确拆分：22 次有效提交/最终输出；27 次请求上下文的模型响应；6 次无效 tool JSON；4 次输出契约校验失败；1 次用户中断未响应。共 60 次。有效提交也包含语义拒绝和重复问题，不是理论最低调用量。

27 次取上下文的响应合计发起 43 个 inspect_graph、5 个 read_evidence、1 个 read_guidance。多个工具可以在同一响应中发起，工具数不等于模型调用数。

按完成任务拆分：5 个无修改任务共 27 次；5 个有修改任务共 32 次；未完成任务 1 次。共 60 次。不能删除所有无修改检查：其中包含首次必要决策比较。

|已完成任务|Writer|Assessment|图中实际修改对象数|
|---|---:|---:|---:|
|structure-0|3|2|0|
|structure-1|3|2|0|
|structure-2（含局部重试）|4|7|2|
|structure-3|2|3|0|
|structure-4|2|3|4|
|structure-5|2|2|0|
|structure-6|2|2|2|
|structure-7|3|5|2|
|structure-8|6|2|0|
|structure-9|3|1|1|

## 根因 1：检查失效范围与 Agent 可见范围不一致

reconciliation_scope.py 的 _stamp 将检查目标的全部入边/出边纳入签名。source_selection_variants 对管理员名字的选择，应依赖 Concept 绑定、相关 Rule 内容与 applicability；School→Administrator、Administrator→Email 的业务边调整不改变这些选择，却会使缓存失效。

实际例子：structure-1、5、8 都检查 source_selection_variants:input::n192。三次的当前 Concept、核心依赖边相同；candidate_nodes / candidate_relations 也相同。structure-5 与 8 的输入只有 task_id、new_id_prefix、task_scope 不同。它们分别花了 5、4、8 次调用。后两次合计 12 次是明确可疑的重复工作，不意味着未经验证便能保证原样省去 12 次。

失效由全图边变化触发，而变化边不在该任务的输入中，Agent 收到几乎相同的题目，自然再次得出 keep。节点定义签名已经收窄，但结构检查签名没有同步收窄，先前修复只解决了一半。

## 根因 2：上下文在结构化字段中要求继续查，在 prompt 中要求不要重复查

ReconciliationController 的组包预算为 90,000 字符，复用的 build_task_context 仍按 24,000 字符软预算裁剪。必需单元不会截断，但可选邻接会进入 deferred_context。

本轮 12 个 Writer episode 中，10 个同时收到 required_units_complete=true、complete=false、budget_exceeded_by_required_units=true。deferred_context.note 仍明确写着“Inspect adjacent relation pages, then request complete endpoint nodes ...”，并附 next_query。

这与新 prompt 的“必要材料齐全时直接提交，不要重查”形成行为指引冲突。它强烈解释了补充查询，不能据此断言 27 个读取响应全都无用。需要统一必需上下文完整性与可选目录可展开性的含义，按真正缺失的判断前提查询。

## 根因 3：局部任务按拓扑信号切分，同一语义问题被分散处理

本轮管理员相关处理先后经过来源选择、平行边、Email 三角关系，再到管理员指代节点重命名。彼此相关的改动没有形成一个连贯的业务问题修复单元，因此每次提交又触发后续检查。

批量输出格式不等于已经批量解决问题。大多数 Writer episode 仍只有一个 unit；多 unit 批次通常全部通过，唯一语义失败批次只有一个单元。因此部分接受在本轮主要提供事务保障，尚未体现显著调用收益。

相关决策与来源选择的无修改提案仍按设计强制 Assessment。若缓存不断失效，同一已通过决策便反复进入双审；应该先减少重复开题，而不是先删除安全评估。

## 根因 4：格式错误和内部契约错误仍交给模型整份重写

6 次 invalid_tool_calls 都是一个完整有效 JSON 对象后多出一个 `}`。离线 JSONDecoder.raw_decode 后，六个完整对象均通过对应 ReconciliationBatch / BatchNodeReview schema。没有进行语义验证，也没有将这些前缀提交到运行图。

当前 llm/incremental.py 对 invalid_tool_calls 进入 plain JSON fallback，再发起一次模型生成。它把一个传输/封装错误变成整份复杂结果重写，增加成本，也可能改变已做出的语义判断。

另有 4 次提交契约拒绝：遗漏 Rule 决策检查、理由引用不是当前 reason、把 node_content: 前缀加在只接受裸 node ID 的检查 ID 上、删除已经不存在的原始节点。另有两次取图时误用归档节点为当前 seed 的查询错误，已包含在上下文读取响应中。

这些不应统称为必要的语义修复轮次。需要先区分无歧义的通用格式恢复、代码管理的 bookkeeping，以及真正必须重新推理的语义问题。不能放宽来源或语义校验来换调用量。

## 根因 5：此前验证没有约束“工作量与收敛性”

1,108 项测试验证了事务、来源保留、部分接受、拒绝后不能空补丁绕过等重要不变量，但没有证明真实调度中的任务数下降，也未覆盖两套上下文完整性标志的行为冲突和上述真实 tool JSON。

早期试验还先后暴露范围扩展后清单不同步、双清单协议、覆盖未持久化、节点定义对所有邻边失效。逐一修复了局部缺陷，却未先验证整体任务量，因此增加轮数和额度只是延后失败，并不是降本。

## 暂停后的验证顺序（尚未实施，需先 review）

1. 用现有接受状态/补丁序列离线重放调度，检查业务边变化不重开无关字段选择；绑定、Rule action、applicability、冲突分支变化必须重开。
2. 对照当前 payload 和实际查询结果，确认哪些前提确实缺失。统一 required completeness 的含义，保留按需检索，不机械删除工具。
3. 用保存的 6 个真实 malformed outputs 做纯本地解析测试。任何恢复必须保留完整内容并继续执行原有 schema、来源、语义校验；不得盲目补括号。
4. 固定任务、输入和反馈做小范围真实对照之前，先确认上述离线指标改善。之后才考虑从零完整运行；预先固定预算与失败停止条件。

没有重新调用模型，没有把人工发现回灌给运行中的 Agent，也没有把可疑重复任务数当作已实现的节省。

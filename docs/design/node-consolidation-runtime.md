# 节点整合局部契约：运行接入与小样本验证

本版已接入 `IncrementalIntegrationAgent` 的 consolidate / node_assess。没有切换既有后台 merge 进程，也没有修改原始 case graphs 或生产 checkpoint。

## 可编辑 Prompt 与构造入口

运行源文件位于 `src/cortex/capabilities/graph_merge/llm/prompts/node_local/`。每次只拼接：共用原则 → 当前角色 → 当前结果契约 → 唯一节点类型 guidance。Concept、Metric、Event、GenerationRule 共用前三部分，同角色工具 schema 不随类型变化。构造函数为 `llm/node_contract.py::node_system`。

`docs/design/node-consolidation-local-review/` 是历史复阅草案；旧 incremental_consolidate / incremental_node_assess / incremental_content_shared 不再由运行入口加载。新的修改入口见 node_local/README.md。

## 局部输入与工具权限

`llm/node_context.py::build_node_packet` 从真实固定输入构造完整单组成员及解析后的完整 alternatives、source unit manifest、紧凑出边和必要端点。仅 relation_metadata 明确引用的入边才交给节点 Agent；无全量消费者、邻居整图或 graph 查询权限。

两个节点角色都只有 `read_evidence`，读取 IDs 必须来自本组公开白名单。Writer 的结果工具为 submit_patch，Assessment 为 Review；公开名称与 Prompt 一致。节点输出只能引用已提供的成员/端点和原边，不能猜一个真实全局 ID 借编译器读取权限过关。

可选属性不使用时省略；输入类型描述去掉无意义的 default:null。顶层、integrate_groups[0] 与每条 source route 的 explanation 各自必填。

## 节点结果和关系闭合

`node_contract.py` 公开窄 Pydantic 模型，禁止通用节点/边编辑。普通完整 content 与完整 alternatives 互斥；真实来源单元恰好路由一次。成员与来源覆盖错误返回 code/path/owner/missing/extra/duplicates，完整节点与类型规则仍由真实编译器校验。

代码继承原关系，执行已确认的端点映射、来源分支路由和严格重复边去重；未知邻居身份不由节点 Agent 提前处理。待协调的关系由 relation_followups sidecar 独立持久化，记录原边、原任务/草稿/候选绑定及当前映射。

`integration/node_relations.py` 区分内容错误与关系结构问题。未知公式操作数或非法计算仍是节点内容错误；真实依赖不一致、关系分组失效或原入边指向新增/改写 conflict 目标时，由 controller 携必要原始含义交给关系协调。alternative_ids 仅限定起点，不可代替终点版本选择。

`node_relation_repair.py` 在未接受的节点事务内先调用关系 Writer，严格编译后立即存 Patch，再独立 Graph Assessment；恢复不重跑已保存 Writer。节点 Assessment 仅评估实际候选。普通语义关系待办则由 relation_followups 队列驱动后续关系事务；即使关系 Writer 无修改，Graph Assessment 仍收到当前 owner/关系并核查全部保义义务。队列未闭合禁止最终发布。

关系共享 Prompt 明确识别 node_relation_followup、node_relation_obligations 和结构诊断，它们是必须闭合的义务，不是可选 inspection signal。节点内容问题不能藏进关系待办。

## 每轮上下文与恢复

1. System：固定四模块，唯一类型在最后。
2. User：当前类型 schema。
3. User：固定局部任务。
4. 后续 User：按首次读取顺序保留 evidence，每个 ID 一份。
5. 唯一末尾 User：当前草稿/候选、有效诊断及版本绑定。

旧 Patch、过期候选与已失效诊断不累积；结构错误中的非本地 graph 数据会投影，保留具体错误字段和省略标记。premise fingerprint 包含当前可见节点、端点含义、权限、来源与候选，排除纯错误措辞和草稿版本噪声。

普通字段错误继续使用原生结果工具修复；损坏工具 JSON 才进入明确的 plain_json、无工具恢复模式，避免重复读取证据。checkpoint 保存输入/权限绑定、证据读取、当前 draft、候选、关系修复与独立评估。所有新 Prompt、schema 和关系/恢复 adapter 均进入契约指纹；旧契约不可静默恢复。

## 少量真实样本

从已存在的 case-level graphs 精确提取以下五组，保留目标节点完整内容与出边，邻居不做身份合并；辅助结构校验只补必要引用。未运行完整数据库 merge。

| Group | 数据库 / cases | 类型 |
| --- | --- | --- |
| patient_sex | thrombosis_prediction / 1201、1237 | Concept |
| examination | thrombosis_prediction / 1164、1202 | Event |
| count_distinct_patients | thrombosis_prediction / 1231、1247 | GenerationRule |
| carcinogenic_molecule_count | toxicology / 266、204 | Metric |
| molecule | toxicology / 300、229 | Concept |

首轮报告：`output/node_consolidation_smoke_20261120_live/index.html`。五组共 35 次真实模型调用，3 组通过；patient_sex 遇上游流中断，Metric 在关系评估通过后暴露已退休边 identity alias 的编译错误。每个节点请求最多 6 条消息，初始 4 条；三组通过结果均严格图校验通过，原出边及来源保留，原图 SHA 未变。

失败样本已在 `output/node_consolidation_smoke_20261120_retry_live/` 从保存状态续跑，两组都通过独立节点 Assessment，累计五组全部通过。续跑只新增 6 次调用，总计 41 次；Metric 没有重跑节点或关系 Writer，只在新契约下补独立关系评估和节点 Assessment。三组首轮通过结果未重跑，原始记录保留。每组最终结果均通过严格 GraphManager 校验。节点通过不代表全数据库 merged graph 已生成。

## 验证范围

192 项相关定向检查全部通过，覆盖公开 schema、局部读写权限、四类型 guidance、来源单元、分支/关系继承、工具输出协议、错误保留、固定前缀、证据去重、输入事实变化的无进展判定、候选恢复、嵌套关系修复恢复、关系待办完整性与最终 gate。真实 Metric retirement 失败记录另有离线回归，验证显式语义后继不会被误当作 identity alias。

不以整个测试集或全量 BIRD 运行替代上述针对性验证。供应商缓存只报告真实 usage 中存在的数据，缺失时标为 unknown。

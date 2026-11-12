# 两阶段 merge：四个管理员 cases 的实现与验证

日期：2026-11-12。最终产物：`output/ontology_merge_v67/administrator_two_stage/provenance_review`。

## 实现

完整方法与各模块输入输出见 [两阶段设计](ontology-merge-two-stage.md)。生产路径已切换为 Identity 对齐 → Node Consolidation → 独立 Node Assessment → Graph Reconciliation；图阶段改动核心内容/共享决策后会定点返回节点评估，最后严格校验并提交一次。

- `integration/submission.py` 保存按对象 ID 替换的累计提案。失败对象留在 pending，当前图保留最后通过校验的版本；提供局部替换、撤回和版本检查。
- `integration/tools.py` 对提交工具使用按操作区分的参数结构，并提供 inspect_pending、完整内容式 write_conflict。代码只做无损结构处理，不替模型决定业务意义。
- 内部字段检查覆盖节点、解析后的每个冲突版本、Metric expression 和 Rule triggers 等嵌套结构。
- `integration/provenance.py` 在发布前检查直接映射与非空改写组的精确 SourceReference 覆盖，不自动把来源扩散到所有后继断言。真实输出在该新增检查下重新编译、检查通过。
- 每次底层模型开始、结束、异常均即时记录；失败阶段与结构化输出重试仍计入用量。提示词集中为 Markdown，类型与 operation registry 按需读取。节点任务不重复携带无关目录。
- 节点评估退修时返回内容整合阶段；图阶段的问题返回结构协调阶段。评估收到上一轮问题与引用的简短列表，以便核对修复；先前评估结论不能代替原始证据。
- 官方接口的 thinking 开关及多轮 reasoning_content 传递已修正。本次后段使用独立实验配置启用 low reasoning，未修改 .env。

生产逻辑未加入案例 ID、数据库表名或具体槽位名分支。案例特定内容仅在输入图、实验反馈和本验证记录中。

## 最终图与核对

最终图包含 122 个节点、153 条边；其中 23 个业务节点、29 条业务关系。物理目录可在交互图中切换显示。

```text
school
  └─ has_administrator → school administrator
        └─ has_administrator_email → School administrator email address

chartered school
  ├─ is_a → school
  └─ has_charter_number → charter number
```

管理员节点以角色为指代；姓名短语保留为 aliases，三个 first/last 联合目标组保留为表示方式。邮箱连接管理员角色，不连接姓名属性或 School；类型级联系不证明某个姓名槽位与多个邮箱属于同一个人。charter number 的业务归属仍是 chartered school；没有通过该属性反向断言 charter 状态。

管理员姓名保留一个真实冲突节点、两个完整实现：case 36 的三个姓名槽位与 case 63 的第一槽位。两者 action 实际不同，分别保留自身来源；没有用案例编号或偶然过滤条件制造互斥适用范围，也不宣称第一槽位是 primary。

邮箱选择和布局分开：cases 35/87 支持按单复数选择第一槽位或前两槽位；这是本批证据允许的条件泛化，不声称第三槽位无效。布局规则接收已经选定的列，用 SELECT <selected email columns> 模式描述横向输出，不再决定槽位数。输出排序规则也只排列已选姓名字段，不覆盖姓名冲突。

原始 406 个节点和 438 条边均有映射或显式改写处置；363 条默认物理来源边映射通过检查。最终 16 个改写组没有用空 after 隐藏删重，直接映射与改写组的原始精确 SourceReference 均覆盖。最后修复了被合并 applies_to 边的具体后继边引用，并保留原始 QA 定位符。组级覆盖不表示每个 before 与每个 after 两两等价。

保存、重载后整图相等；真实冲突的两个完整版本及其来源不变。最终严格图校验通过，抽象 has_attribute 实例边为零。原始图始终保留。

另做了只读汇总核对：第三邮箱列有 42 个非空记录（均含 @）；22 个 charter number 被多个 chartered-school 行共享，72 个非 charter 行也记录了 charter number。这些结果保存在 database_audit.json，是独立后验核对，不冒充模型当时可用的证据。

## 真实运行经过与限制

本次不是全自动一次通过。完整重跑的独立节点评估拦下了姓名槽位冲突被掩盖、primary 称谓等问题，但人工检查仍发现输出布局、指代命名与精确来源去向的遗漏。第一次局部修复还出现第三邮箱覆盖要求前后矛盾并耗尽预算，未发布成功图。

后续在保留来源输入的前提下，以明确记录的实验反馈继续小批修复。补充了“表示覆盖不等于 SELECT-all，也不等于语义排除”、独立内容/布局、上一轮反馈核对、对象指代标签和来源覆盖要求。最终输出经过真实模型复核与独立代码核对；图协调的自检不等于独立全图语义证明。

没有重跑四个 case 的 extraction，也没有重跑 NL2SQL 预测或证明 SQL 准确率提升。该实验不能证明任意图均可稳定自动整合，也不能证明每个优化单独导致 token 下降。最后一条来源修复仍耗费 10 次调用（协调 7、节点复核 3），说明评估范围与未变化知识的重复检查仍有收窄空间。

## 调用、tokens 与成本

下列调用来自官方接口，均为实际记录；累计输入包含多轮重复上下文，输出包含 reasoning tokens。缓存输入已按独立费率计价。

| 运行 | 调用 | 输入 tokens | 输出 tokens | 已知用量估算 USD |
| --- | ---: | ---: | ---: | ---: |
| 四图完整重跑 | 30 | 1,367,056 | 123,959 | $0.1190 |
| 首次定点修复（轮次耗尽） | 29 | 1,375,493 | 117,777 | $0.1039 |
| 策略与布局修复 | 9 | 468,030 | 36,203 | $0.0361 |
| Concept 指代修复 | 7 | 295,101 | 21,742 | $0.0257 |
| 精确来源修复 | 10 | 469,326 | 42,463 | $0.0320 |

从完整重跑到最终交付共 **85 次**，累计输入 **3,975,006**、输出 **342,144**，估算 **$0.3166**，其中包括失败修复。

本轮 v67 全部调试尝试（另含 r1–r12）共 **316 次开始、315 次返回 usage**，已知输入 **14,753,271**、输出 **686,797**，估算 **$0.7509**；另有 1 次中断调用没有 usage，未假定为零费用。SDK 内部未返回用量的传输重试也不能虚构费用。这不是一次生产批次的稳定成本。

费率快照来自 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)：本批 UTC 17–18 点为 off-peak，每百万缓存输入/非缓存输入/输出为 $0.003/$0.15/$0.6。官方说明 legacy deepseek-v4-flash 名称目前由 Flash 服务和计价。以上为按响应 usage 估算，不是账单实扣。完整逐次数据见 output 下 cost_report.json 与各运行 model_calls.jsonl。

## 最终本地检查

完整测试：1016 passed；本轮核心模块 Ruff 检查通过。同一来源修复前后图仅一条边的 sources 不同：新增来源检查器拒绝修复前图、接受最终图，比较记录为 provenance_guard_comparison.json。没有为校验改动重新调用模型；这是对已记录真实输出的确定性验证。

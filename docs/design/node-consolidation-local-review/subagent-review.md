# Sub-agent 审查与修复记录

范围：局部节点整合 Writer / Assessment 的四类型 prompts、公开输出契约、文档构造器和缓存布局。未调用真实模型服务，未改后台 runtime 或 checkpoint。

## 首轮审查

| 审查 Agent | 独立范围 | 首轮结论 |
| --- | --- | --- |
| `/root/review_type_guidance` | 四类型语义、泛化与冲突、共用规则和示例完整性 | 总体结构可用，类型尾部、Rule 等价边界和未声明细节需修正 |
| `/root/review_format_contract` | 对照真实 schema、compiler、来源路由与 Review 引用 | 公开字段大体一致；Metric/Event 校验、分支来源资格和分支引用格式有遗漏 |
| `/root/review_cache_layout` | System 顺序、tools schema、动态消息与缓存观测 | 类型 guidance 提前截断固定前缀；动态草稿位于证据之前会破坏后续复用条件 |

## 已纳入的修复

| 发现 | 修改 |
| --- | --- |
| guidance 不在末尾 | 统一为 common → role → role output → 唯一 type，构造器检查实际拼接 |
| 共用规则没有明确角色权限 | common 开头声明只提供语义标准，权限和结果由随后角色模块限定 |
| NULL、粒度清单可能制造回审义务 | 只保留来源明确或行为承诺的语义，未说明细节不新增义务 |
| 排名写法被过早认定冲突 | 等价可合并，实际不兼容才分支，前提未知先读证据/报告 |
| Metric 恒等表达式拒绝条件遗漏 | 明确裸列、纯 CAST、+0/*1 等不通过，禁止编造运算补丁 |
| Event 匹配约束遗漏 | 补组去重、规范索引、非空表达式及无 targets 时不留表达式 |
| Metric/Event 示例不足 | 补完整 NodeContent、相容整合/泛化与身份/冲突反例；四类均验证完整内容 |
| Review alternative 引用不明确 | 使用 owner + fields=["alternatives"]，文字定位分支 ID；禁止额外引用字段和展示层路径 |
| common_evidence_ids 不满足分支支持前检 | 写明实际编译器的三个通过路径，区分资格门槛与语义支持 |
| SQL 模式要求可能强迫编造 | 改为必要时提供，保留来源已有承诺 |
| 旧边句柄适配尚未部署 | 角色契约明确部署前提，未保证解析则报告；不声称现 runtime 已支持 |
| 退役 schema 仍暴露不支持的 alternative_id | 拟议窄 schema 删除该字段，契约明确禁用，并检查元素只含 id/kind |
| 工具 schema 可能按类型/组漂移 | 同角色固定完整工具 schema、顺序和描述，ID 与类型约束由 controller 检查 |
| 草稿/候选在 evidence 前变化 | 拟议消息重建器将唯一当前状态移到末尾，证据只按首次顺序追加 |

## 修复后复核

| 审查方向 | 最终结论 | 验证边界 |
| --- | --- | --- |
| 四类型语义 | 复核通过，无剩余实质阻塞 | 已解决的约束不增加历史输入未承诺的新义务；共享示例区分 Writer 与 Assessment |
| 代码契约 | 复核通过，最后的退役分支字段暴露已修复 | 审查者本地执行公开输出/局部整合/分支 Review 验证；最后字段修复仅核对代码，主 Agent 再统一构建 |
| 缓存布局 | 复核通过，无剩余布局阻塞 | 审查者独立执行 check_layout；provider 命中与 SDK wire 不在本次验证范围 |

缓存审查额外指出两处界限，已补入：`messages()` 是纯渲染器，跨轮 ledger 不可变性与白名单由未来 controller/checkpoint 实施；User 示例是逻辑 packet，不表示只发送一条消息。没有把这些待接入能力标记为已经部署。

## 验证边界

自动检查覆盖公开输出示例、局部 `_integrate`、真实字段/语义验证器、分支 Review 引用，以及四类型前缀和拟议 tools/messages。未执行完整 TaskRunner 回归、provider wire 序列化验证或真实命中率测量。窄工具、局部 packet、ID 句柄适配和运行日志接入仍待生产实现。

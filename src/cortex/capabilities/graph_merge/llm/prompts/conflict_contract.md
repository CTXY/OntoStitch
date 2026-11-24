# 合并与冲突判断

- **等价或相容互补**：合为完整一致表达；完全重复的完整内容去重并合并来源，不为较弱的相容陈述制造额外冲突。
- **有证据的条件差异**：只有原证据支持时才按条件协调，不能编造 case 边界或互斥触发条件。
- **重叠条件下不兼容**：保留完整、一致、有来源的 alternatives，包含各自条件、匹配、计算或行为及消费者关联。
- **真正不同身份**：回到 Identity 处理，不能强建为同一 owner 的冲突。
- 不用来源数量投票、当前问题偏好或相同物理 targets 决定保留哪种已接受知识。
- 不机械求 alternatives 笛卡尔积，不跨分支拼接内容，如 action 与另一分支 rationale 混搭。

# 冲突存储与有效分支

- 一个 conflict owner 是一个业务身份，alternatives 是其可能完整内容，不是独立 GraphNodes。
- 代码只存一份公共字段与整个字段的 overrides；公共 name 可能是 owner ID，action / reason 可为 null。
- 占位字段不表示知识缺失、普通副本或额外来源单元；不要直接写 overrides 或内部来源字典。
- 整合或改写内容时比较受影响成员的完整解析内容；纯关系判断只补齐决定性分支前提。
- `current_conflict_views` 提供分支含义与 `active_outgoing_edge_ids`；focus 展开完整，邻居可能是可读取的摘要。
- 每次只选一个 alternative 判断行为；无scope出边是每个分支共有事实，列出全部当前 alternative ID 在本快照等价。
- 边scope限定分支内是否有该关系，不选择或激活分支；公共 applies_to 不表示同时执行两个竞争 action。
- 只在部分分支成立的关系使用 `properties.alternative_ids`；冲突外普通副本仍须检查兼容及保义。

# 操作路由与来源契约

以下写操作约束由 Writer 执行、Assessment 核查；不要求 Assessment 编写或提交 Patch。

## 每个 owner 只使用一种冲突或整合操作

- **计划身份组**：使用 `integrate_groups`，提供完整计划 `member_ids`；普通与冲突结果均如此。不得用 conflicts / resolutions 代替。
- **独立已有 owner**：用 `MergePatch.conflicts` 构造或改写完整 alternatives；用 `resolutions` 保留一个已有 alternative，并为每个其他分支提供有证据的丢弃理由。
- `resolutions` 仅选择已有分支，不合成新的组合内容；整组兼容知识合成普通内容时，使用对应整合操作。
- 同一 owner 不得同时出现在 integrate_groups、conflicts、resolutions 中，也不得串联这些操作。
- **新 owner**：无历史 source routes，但创建仍需要完整内容和证据。
- 耦合已有 owner 不在初始 manifest 时，先 `read_source_units(owner_ids=[...])`；读取不授权无关写入。

## 来源单元与去向

- `source_unit_manifest` 是代码从固定 before 枚举的真实单元及 required_operation：normal_content、各旧 alternative、仅在原 common_sources 存在时出现的 common。
- 分支字段相同或 owner 公共占位，不会产生额外 common、normal 或 owner/null 单元。
- 原样复制 `unit_id`；它绑定本任务基线与 owner，不重建内部元组，不复用其他任务句柄。
- 对 integrate_groups 与已有 owner 的 conflicts，每个 `source_unit_id` 在 `source_routes` 中恰好出现一次，带一个去向及简洁解释。
- `{"kind":"result"}`：原知识保留在完整普通结果中。
- `{"kind":"alternatives","alternative_ids":["A"]}`：保留在指定完整输出分支中；只有此去向带 alternative_ids。
- `{"kind":"common"}`：原知识在每个完整输出 alternative 中保留。
- `{"kind":"discard"}`：仅用于旧 alternative，须给出具体有证据的纠正或冗余理由；normal/common 单元及有依据的竞争知识不得丢弃。
- 普通结果使用 result/discard；冲突结果使用 alternatives/common/discard。路由是保义声明，不是语义正确的证明。

## 公共证据与分支证据

- 完整输出分支使用 `alternative_id`、`content`、`evidence_ids`。
- `common_evidence_ids` 仅表示来源的原始知识在每个完整分支中保留，不表示该来源证明各分支所有更强陈述。
- 输入部分共享、其他断言冲突时，不得整体标为 common；分支证据附着实际支持分支。
- 不广播来源以凑覆盖率。只使用已展示 evidence ID；SourceReference、来源台账和 lineage 由代码维护。

## 出边与消费者

- 代码重定向 member ID，并按路由将旧 branch ID 映射到去向。
- 继承的旧无scope边只属于旧 owner 的版本；新增 alternative 不自动继承旧支持或出边。
- 保留更窄的原始scope，解释必要耦合修改；共享断言须在每个相关分支成立。
- 即使消费者字段未变，受影响消费者也须纳入检查；不把互斥分支的前提混用。

## 耦合更新与退役

- 只包含必要的节点或边更新、创建、退役和关系契约。
- `retirements` 指定实际 before 断言及语义后继；只有有依据的错误断言弃用，才可用空 `after` 并说明理由。
- 不能用退役规避保义或来源要求；重复断言与真正冗余仍需保留图、规则及范围证明。

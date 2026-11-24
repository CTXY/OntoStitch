# Reconciliation 的可读 User Prompts

本轮调整 Writer / Assessment 输入呈现、自动编译评估流程及恢复绑定，不切换后台 runtime，也不覆盖旧 checkpoint。

每次请求固定为 1 条 system + 2 条 user：

1. system 保存共用图契约、角色职责、输出契约及本轮 Signal guidance。
2. 第一条 user 用中文说明本轮具体任务、必须结果、权限范围及阅读方法，提供固定的修改前局部图和相关谓词契约。
3. 第二条 user 表达当前状态。Writer 看到完整当前草稿、已编译的实际变化、补读材料与有效反馈；Assessment 看到真实候选变化和待复核问题，不重复 Writer 的完整修改草稿。

可编辑模板位于 `src/cortex/capabilities/graph_merge/llm/prompts/reconciliation/user/`：

- `writer-task.zh.md`：Writer 固定任务与原始材料。
- `writer-state.zh.md`：Writer 当前草稿、候选、反馈与本轮动作。
- `assessment-task.zh.md`：独立评估的具体任务与原始比较材料。
- `assessment-state.zh.md`：真实候选与本轮结构化回复要求。

`reconciliation_presentation.py` 负责填入任务数据。节点内容仍使用公开 JSON 字段；关系按端点、谓词、属性和证据 ID 呈现，便于对照输出引用。模板填充只发生一次，不对图中的 `${...}` 表达式做二次插值。

## 缓存与多轮管理

固定 user 消息在同一局部事务内缓存并逐字保留。新增读取和候选变化只更新第二条 user，不回放模型的历史回复、工具调用历史或旧草稿。

初始空草稿不发送空数组、成功回执或逐对象 unchanged 标记。未变对象沿用原始局部图；只为实际变更呈现完整候选内容，删除对象只列 ID，原始正文仍保留在固定输入。原始 evidence 正文按证据 ID 合并去重，不展示正文哈希及内部字典。

内部候选版本、覆盖证明、来源快照绑定和完整错误记录仍保存并校验。模型只见任务必需的材料与诊断；历史读取使用公开 handle，信号中的内部 source_snapshot_id 转换为对应 handle。

无进展判断使用实际可见材料的指纹：固定输入、真实候选和新增读取。反馈措辞变化、版本号变化和 coverage 账本变化不单独算新增事实。错误会完整保留；同一错误在多个回执中出现时，呈现一次。

## 真实测试的要求边界

Signal 可表示疑点或优化机会。明确的任务要求不会因为 Signal 的一般性质而变成可选项。受控 value_concepts 测试要求业务层存在标签维度 Concept，因此用 required + required_outcome 显式描述；这不是要求所有真实 value_concepts 信号都必须创建新节点。

报告保存原始 HTTP 请求、完整 Agent 输出、工具/编译反馈及前后图差异。HTML 同时提供可读排版和逐字原始文本，服务端拒绝、格式修复及测试预期不满足均不会标为通过。

## 谓词目录与信号记录（本次更新）

初始完整谓词契约保留所有已呈现关系的契约；额外候选只按局部实际存在的业务起点类型和终点类型匹配，不为假设的新 Event / Metric / GenerationRule 扩大目录，也不做基于名称或模型打分的语义筛选。无 Event 的局部图不需要额外 examines；已有该关系时其契约仍保留。

Writer 确需创建其他类型时，可用只读 read_predicates(from_node_type,to_node_type) 读取固定输入目录中的契约。它不展开图、不增加已有对象写权限。结果进入动态消息；没有匹配契约时也给出明确的类型对查询结果。读取及负结果可从 checkpoint 重建。Assessment 不开放该工具，实际候选使用的契约会完整提供。

User 中的命中节点/边 ID 与检测/任务记录，是本轮实际输入，不是示例或 guidance。abstract_predicate 的命中原因直接从固定原图实际边谓词解释；受控测试给出的候选提示单独标注。通用示例只放在 System 对应 Signal 章节，不能作为本轮来源证据。

每次 edit_draft 为本轮完整候选，编译成功直接进入 Assessment，无独立提交调用。无需改动时显式空编辑；读取可先完成，相关修改应放在同一次编辑中。

> Active runtime prompts are the `.en.md` assets. The `.zh.md` files are retained earlier review copies and are not loaded.

# Reconciliation：可读的局部输入

System 按共用语义契约、角色职责、输出契约、本轮 Signal guidance 排列。User 模板可直接编辑于 user/：writer-task.en.md、writer-state.en.md、assessment-task.en.md、assessment-state.en.md。

每次请求为 1 条 system + 2 条 user。第一条 user 用中文说明具体任务、必须结果、权限范围与固定原始局部图；第二条只更新当前草稿、真实候选变化、补读材料和有效反馈。固定前缀逐字缓存，不回放旧对话；未变正文沿用原始输入，不逐项发送 unchanged。

内部版本、哈希、coverage 证明、来源快照和完整诊断保留于 checkpoint。公开材料使用 reconciliation_projection.py，user 呈现使用 reconciliation_presentation.py；公开历史 handle 用于读取与引用，不展示内部来源快照账本。

Writer 编辑后自动编译，成功直接进入 Assessment；空编辑显式请求评估当前草稿。Assessment 独立比较真实候选，直接返回 explanation/feedback；没有结果提交工具或阶段路由。具体任务已经明确要求的结果必须核查，不因 Signal 通常只是疑点而改成可选项。

设计及实际测试记录：docs/design/reconciliation-readable-user-prompts.md 与 output/reconciliation_readable_prompts_20261121/。

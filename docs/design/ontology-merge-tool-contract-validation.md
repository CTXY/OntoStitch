# 简化 Merge 接口：可控验证记录

日期：2026-11-16。对应[接口规范](ontology-merge-tool-contract-review.md)及[语义职责设计](ontology-merge-content-and-relations-review.md)。

## 第一阶段：协议验证范围

本轮没有外部模型调用，没有重跑完整 merge，也没有恢复后台任务。使用固定图、程序化模型响应及已有回归测试验证接口行为，不将模拟输出当作真实模型语义评测。

最终合并相关回归：**325 passed**。包括新建 `tests/test_merge_simplified_interface.py` 中 **14 项**针对性测试；`git diff --check` 通过。

测试命令使用 `.venv/bin/python -m pytest -q`，选择 tests 目录下名称以 test_merge_、test_incremental_、test_graph_integration、test_graph_merge_、test_relation_、test_node_revision_、test_pending_merge_ 开头的测试文件。

## 已确认的行为

| 场景 | 检查结果 |
| --- | --- |
| 首屏已含必要上下文，Patch 修改一个相邻对象 | 无 extend_task，机械合法即完成该 Writer 输出 |
| Patch 需要未展示的耦合节点 | 第一次不应用任何修改，返回 required_context；使用反馈的下一次输出可以完成 |
| 已读无关节点 | 仍拒绝修改，不将读取等同写权限 |
| 新边指向未展示端点 | 先提供端点上下文，不因此开放端点内容修改 |
| 范围临时纳入后，另一项编辑无效 | 范围、暂存操作、候选均回滚，不留下半个提案 |
| 重复读取后继续读不同字段 | 重复请求引用已有响应，新的必要读取仍执行 |
| 同一证据只被边引用 | 收集并在末尾提供，保留边的 evidence IDs |
| 不同证据 ID 对应完全相同正文 | 正文只一份；各自精确来源仍分开映射 |
| 后续读取遇到同正文的新 ID | 返回 same_content_as，不再复制正文 |
| 相同 ID 对应不同正文 | 明确报错，不静默覆盖 |
| 新 Agent 对话 | 重新提供所需正文，不沿用其他对话的已读标志 |
| 冲突摘要 | 首屏提供必要含义/生效边；完整内容仍可定向读取，底层 alternatives 不变 |
| Node Writer / Assessment 实际输入 | 使用同一份按类型整合规则；不注入旧理由/原子性必查清单 |
| 最终 Review schema | 只暴露 verdict、issues、issue_refs；旧检查字段可读 checkpoint 但不要求新模型生成 |

上述模拟对话中，完整合法结果只需一次输出；缺少范围上下文的路径是一次被拒结果加一次完成结果，没有额外 extend/finish。它们是**协议路径验证**，不是实际模型调用成本预测。

已有回归覆盖冲突内容、分支边、精确来源、字段部分更新、历史 ID 重映射、待审候选恢复、无修改跳过评估与失败处理。

## 第一阶段尚未验证的部分

实际模型是否更少主动读取、是否减少语义修复，以及管理员/loan-date/card_games 的最终质量和 token 成本，仍须使用固定输入进行真实对照。不能从本轮机械测试推出“模型生成质量不降”或“成本降低某个百分比”。

## 真实验证完成与后台交付

以上早期协议测试已进一步补充真实模型验证（2026-11-16）。完整记录见 `output/merge_contract_v87/REVIEW.md`，逐请求原始记录与 token 报告保留在对应目录。

| 真实测试 | HTTP/模型请求 | 输入 tokens | 输出 tokens | 结果 |
| --- | ---: | ---: | ---: | --- |
| financial loan-date 局部修复成功轮 | 13 | 194,926 | 116,792 | 通过；历史同任务为70次，本轮初次另1次传输失败 |
| card_games 修订后的单匹配组 r2 | 3 | 56,640 | 58,493 | 独立检查通过，三组匹配、父set知识、两项冲突、来源保留 |
| financial 四图完整流程 | 8 | 92,073 | 66,402 | 独立检查通过；85节点、102边，280输入节点与292输入边全部有映射/明确重写 |

financial四图调用分解：Identity 2、Consolidation 2、Node Assessment 1、Reconciliation 2、Graph Assessment 1。没有30次工具往返，也没有依靠调用上限截断。

全部小测、失败尝试与修订轮共36次请求；33次返回usage，已知输入522,074、输出365,371 tokens，输入缓存命中261,376；另3次传输失败usage未知。不能把未知计成零，也不能由代理token数推断实际账单。虽然调用减少，部分响应仍含大量reasoning_content；请求已显式关闭thinking，服务返回行为并不一致，尚不能声称输出token问题完全解决。

代码回归：前轮356项通过；最终修改后所选接口/上下文/恢复相关128项通过，另匹配保留专项42项通过。真实成功与代码测试分别记录，不互相替代。

后台使用冻结runtime_r2（哈希清单检查）、独立工作区、10图/批、原有checkpoint和同对话传输恢复。financial四图在runtime_r1完成；r2只进一步澄清匹配保留，card_group_r2验证该澄清。复用已审查的四图作为financial基图，避免重复花费。

financial共53份，后台继续49份；card_games共96份，从原始case graphs重新整合，其中386、523、529保留原needs_review标记。后台语义检查失败会保留状态并退出为needs_attention，不会静默强行发布。完整图尚需后台完成；本轮启动确认不代表所有未来批次保证通过。


## 后台恢复：空失败标记（2026-11-16）

card_games第六批的Writer提交`failure: "null"`，被当成真实失败退出。`BatchResult.failure`现明确要求成功时省略或使用JSON null；空字符串、null/none/nil/undefined字符串属于可纠正的schema错误，返回同一Agent对话重新提交。真正失败原因仍触发原有终止与checkpoint保留。没有把字符串标记静默转换成通过，也没有更改语义规则或回滚已发布50份输入。

新增测试覆盖字段错误反馈、同一episode纠正后完成，以及已有真实failure终止行为。后台以runtime_r3恢复，和runtime_r2相比仅修改BatchResult契约模块；旧运行与调用记录保留。最终成功需要后续Node/Graph Assessment与来源审计，不能由字段修复自动推出。

financial测试评测单独位于`output/financial_online_v88`：53个固定test IDs与53个train IDs不相交；使用全量training最终图的冻结副本。一跳语义检索、问题过滤、Simulator选项选择、按适用性提供GenerationRule，随后full_schema + downstream_guidance baseline生成一次SQL并进行完整结果行集合相等评测。失败case计入总分母，仅传输错误可重试；未完成前不报告最终EA。模型deepseek-v4-pro、thinking关闭、并发8，配置和图哈希均有记录。

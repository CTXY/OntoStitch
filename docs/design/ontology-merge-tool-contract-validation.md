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

## card_games 创建接口受控验证（v89，2026-11-16）

修改前代码已提交并推送 GitHub checkpoint `16f3547`。三个 subagent 审查了创建/更新契约、纠错草稿与来源反馈，实施后再次审查。最终相关回归 **242 passed**；生产代码没有 case-specific 分支。

重建 batch-0006 / structure-0-3 的原候选图，使用历史首次失败的 `new_edges` 提案作为明确标记的模拟种子，由生产代码产生真实拒绝反馈，再调用真实模型恢复。模拟种子不计模型调用。最终 `correction_r4` 使用冻结代码（哈希可核对），一次真实 Writer 返回完整合法 Patch，一次 Graph Assessment 返回 accept；输入 **20,501**、输出 **2,876** tokens，HTTP 响应时间合计 **165.72 秒**。

新增 `One output row per artist → applies_to → format`；局部关系9→10。原322节点和479边全部未改；新增边保留 bird-card_games-520 的 QA/qa 两条精确来源。Writer略微改写description，因此测试脚本的逐字断言失败；保留真实输出并仅续跑Assessment，未重跑Writer或人工修改候选，语义评估通过。

失败实验也保留：local_r1自主检查返回空修改、跳过Assessment，不能证明新边创建；correction_r1/r2/r3观察到reasoning中的正确意图与HTTP工具参数不一致，均停止，没有创建候选。操作分型让必填字段明确，但仍不足以保证服务输出正确。最终恢复复用纯JSON通道，保留相同schema及完整校验，不靠猜字段或放宽约束。此次真实成功使用完整一条边Patch，不能声称模型成功执行了corrections.move；该操作有真实数据机械回放和回归覆盖。

所有实验共 **15 HTTP尝试**，12次有usage，已知输入 **129,864**、输出 **30,546** tokens；3次在途终止用量未知，不计为零。最终成功为固定错误提案的恢复验证，不是完整merge效果或严格的自主发现A/B。自主检查输入缺少原两个related_decisions导航信号；原候选无冲突节点，不能据此宣称通过冲突重构压力测试。

产物：`output/merge_contract_v89/index.html`、`correction_r4/graph.html`、`correction_r4/change_summary.json`、`probe_costs.json`。本轮没有将隔离测试结果替换到完整后台合并工作区。

## card_games transport recovery (v92, 2026-11-16)

Historical non-streaming calls repeatedly lost the connection around 420 seconds despite a 3600-second client timeout. The reconstructed failed body was about 111 KB (49 nodes, 52 edges, 56 evidence entries); larger bodies had already succeeded, so these logs do not establish a hard input-size limit. Thinking was explicitly disabled, but a successful call still returned 33,819 output tokens over 422 seconds. SDK retry=1 duplicated a long HTTP attempt before dialogue-level key rotation. Generic `Connection error` logging concealed the cause and never activated the previous string-based streaming toggle.

A same-body SSE probe exposed server-sent `[Errno 32] Broken pipe` and `upstream_stream_truncated`. Its first HTTP-only success marker was corrected to failure: a closed HTTP stream is not a complete model result. The exact intermediary causing every historical 420-second failure remains unknown; the server's upstream failure is directly observed.

Merge now requests SSE, uses connect/write/read/pool timeouts of 30/120/3600/60 seconds, and leaves retries to the same-dialogue recovery loop (SDK retries=0). Audit stores response-header timing, streamed byte/chunk progress and redacted underlying exception chains. HTTP-200 SSE errors or missing terminal completion raise a transport failure before any partial response reaches Agent decisions. Read/graph tools, model, prompts, semantic iteration limits and graph quality checks are unchanged.

24 transport/recovery tests passed, including fragmented tool arguments, SDK compatibility, mid-stream socket failure, error events after valid-looking tool JSON, silent incomplete EOF, provenance-safe dialogue recovery and credential redaction. Real SSE ping succeeded. The original large-body probe failed upstream, was retained, and its partial result was rejected by the new guard without executing graph tools. Production then produced multiple complete SSE responses and advanced accepted checkpoints.

Checkpoints 18 and 19 were backed up. The transport rollout preserves the original accepted schema and merge implementation fingerprint; the frozen-runtime diff consists only of `scripts/run_resilient_training.py` and `src/cortex/infrastructure/llm/stream_audit.py`. Graph and exact-source preservation audits passed, and all 96 input graphs plus five published batch states retained their hashes. Detailed measurements, raw probe, loaded-runtime manifest and current status are under `output/card_transport_v92`. This establishes functioning recovery, not a guarantee that the external provider will never truncate another response.

## Transport comparison and execution-failure contract boundary (v93)

card_games was paused at accepted revision 25 before further comparisons. The exact failed request was about 76.6 KB. Single-attempt tests used the same provider, model and key slot; no graph tools were executed by the probes.

| Comparison | Elapsed | Actual result |
| --- | ---: | --- |
| Original tool output, SSE | 10.34 s | Upstream truncation |
| JSON result output, SSE | 13.92 s | Upstream truncation |
| Original SSE request bypassing local proxy | 5.84 s | Upstream truncation |
| Original tools, non-streaming | 324.82 s | Complete response, but invalid submission: missing `patch.explanation` |
| Original SSE request repeated after that response | 24.72 s | Upstream truncation |
| JSON result output with read tools, non-streaming | 255.30 s | Complete response with two schema-valid `inspect_graph` calls |

The last test proves transport and read-call formatting, not completion of graph repair. The malformed submission in the first non-streaming response remains rejected by the normal contract. Neither HTTP 200 nor provider response-ID prefixes establish a valid result or locate the exact failing upstream component. The evidence is consistent with intermittent/provider-link instability; it does not establish a permanent endpoint failure or an input-size cap.

HTTP mode is now explicit (`--merge-http-mode`) and survives client replacement. After three consecutive connection failures for the same logical request, recovery exits with a non-retryable transport error and retains the checkpoint. The outer runner cannot restart that failure indefinitely. This is a network-failure safeguard, not a semantic model-call or training-round limit. Successful model responses reset the logical-request boundary.

card_games resumes from the unchanged revision-25 checkpoint in non-streaming mode. The frozen runtime preserves the previous graph schema, merge logic and prompts, overlaying only the runner and transport helper. All 96 case states and five published batch states are hashed; restored lineage and exact-source coverage pass before launch. Detailed logs and manifests are in `output/card_transport_v93`.

football case1058 exposed a separate parser-boundary defect: singleton repair-group annotations were normalized only after nested Pydantic parsing could already reject them. Normalization now runs before validation and removes only inapplicable multi-error annotations; it preserves the error, owner and repair instruction. Invalid `base_asset_id` is still rejected, with its exact field path and the allowed accepted IDs; retrieved or draft nodes never become accepted UPDATE bases automatically. The real resumed case passed and its coordinator/watchdog resumed. Other database workers were left running. The selected transport/contract/runner regression suite reports 29 passed.

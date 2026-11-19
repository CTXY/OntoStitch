# Offline 阻塞恢复：输入、局部范围与 SQLite 执行

## 1. 累积读取导致 Reconciliation 输入超限

formula 的失败区域首屏约 67 KB；13 次模型调用不断读取关系、节点字段和端点，累积请求达到 222,345 字符，超过 210,000 的保护预算。不是首屏已经超限，也不是网络重试能修复的问题。

`compact_relation_history` 在请求视图中引用已展示的相同字段：

- 按 input/candidate view、revision、对象 ID、字段路径和实际值区分；不同版本与不同值不合并。
- 保留第一次完整值，后续长值用 `reused_fields` 指向它；短值不做反而更长的引用。
- 保留每个工具响应、tool_call_id、所有不同事实及原始对话。代码处理图时仍使用原始结果，不使用简化视图。
- 预算仍按最终序列化请求检查，不扩大预算，不裁掉唯一证据。真正的唯一信息超限仍会停止。

原失败对话产生 144 个字段引用，17 个 JSON packet 均已逐一展开还原为原始结构，未删除唯一事实。真实模型调用一次，返回合法 `submit_changes`；输入 61,904、输出 2,340 tokens。该探针只验证输入恢复和提案格式，不代替完整流程中的独立语义 Assessment。

## 2. 退役与承接节点的范围判断矛盾

student 的 Writer 明确选择退役重复 Rule，并指明实际承接 Rule。来源校验要求把退役 Rule 的证据分配到承接节点，但局部范围只承认图邻接，孤立重复节点与承接节点没有路径，所需 `add_evidence` 因而被拒绝。Agent 在删除来源更新和重新添加之间来回失败。

`ProposalScope` 现在将本地 assertion 的显式替代后继视为这次修改的耦合对象。后继仍必须是实际存在的语义节点；若尚未展示，代码先返回上下文，不应用任何修改。任意读取不授予写权限，物理目录仍只读。实际身份／替代含义由 Agent 和独立 Assessment 判断；来源、分支和图格式校验保持不变。

同一失败区域在隔离工作区进行了完整真实测试：Writer 6 次、Graph Assessment 2 次，最终 accept。重复 Rule 被删除，承接 Rule 获得原 QA/qa 精确来源，其他关系修改也经过评估。输入 187,340、输出 3,838 tokens。测试没有覆盖原生产 workspace，后台从它自己的 checkpoint 继续。

## 3. Oracle 超时来自当前 SQLite 查询计划

codebase case701 的生成 SQL 已有结果，Gold SQL 每次在 30 秒超时，故被正确标为 evaluation unavailable；不是 Assessment 无法判断或 SQL 生成服务不可达。

只读对照使用完全相同的 SQL 和数据库：

- 当前 Python SQLite 3.53 的计划按 posts/user 行反复进入 MAX(Reputation) 子查询，120 秒仍未完成。
- 系统 Python SQLite 3.43 的计划先读取该聚合子查询，约 1 秒得到结果。
- 通过统一执行接口的进程启动／冷缓存对照约 3.9 秒，结果与历史生成 SQL 完全一致；前后原数据库 SHA-256 相同。

这里只认定本次计划和性能差异，不推广为所有 3.53 查询都不可用。

执行接口增加可选 `CORTEX_SQLITE_PYTHON`：显式选择一个 Python/SQLite 后端。SQL/参数通过 stdin JSON 传递，子进程只依赖标准库；Gold 和候选 SQL 使用同一后端、相同 SQL 和原数据库。支持超时、只读、重复列名、NULL 和 BLOB，未配置时维持原有执行路径。没有增加索引、修改数据库、改写 Gold SQL 或放宽 EA 判断。

本次 codebase 进程配置 `/usr/bin/python3`；其他数据库不切换后端。case701 已通过原有真实抽取验证，codebase 协调器恢复并进入 merge。

## 恢复与验证边界

相关回归 56 项通过。原 case graphs、已发布批次图和 workspace SQLite 均保留并记录哈希；恢复使用原始输入、节点 ID、Accepted 状态和 lineage。没有把隔离实验结果直接覆盖到生产工作区。

修改仅涉及接口呈现、局部写入许可和执行基础设施；业务 schema、独立语义 Assessment prompt、编译器和来源校验与原版本逐字核对不变。对于 formula/student，显式记录相容的既有 review-policy 标识以保留已通过区域的缓存覆盖；实际加载代码单独记录哈希。图内容变化仍产生不同区域签名，不会跳过被改变区域的评估。

后台保持 10 graphs/批及既有语义轮次，不增加 case-specific 分支，不无限重启确定性契约错误。小测的两个前置脚本装配错误发生在模型调用之前，保留日志，未改动原 workspace。产物、真实输入／输出、加载记录、备份和恢复状态位于 `output/merge_recovery_v97`。

## HTTP 502 停止保护与实际版本部署（v98）

formula 后续发生同一请求连续 39 次 HTTP 502。旧冻结运行版本只把连接异常、超时和不完整响应计入失败窗口，未把 HTTP 502 计入；恢复回调会再次替换客户端，故计数始终重置。当前仓库源码已有状态码识别，但该修复未进入上一轮冻结运行版本。检查源码不足以证明正在运行的进程已修复。

实际部署的 `transport.py` 现在沿 SDK exception 的 cause/context 链读取 `status_code` 或 `response.status_code`，把 408/409/429/500/502/503/504 与连接异常计入同一窗口。同一个逻辑请求最多三次失败、两次恢复；第三次抛出 `TransportRecoveryStopped(retryable=False)`，不能被 Node Agent 的外层 API 重试或 merge runner 绕过。成功响应后的下一次逻辑调用使用新窗口。SDK retries=0，正常 Agent 调用次数与语义轮数不变。

传输、stream audit、runner 的相关测试 34 项通过，包括真实 SDK 错误类型、嵌套状态码、成功后窗口重置，以及 Node Assessment 入口的外层重试不能绕过停止信号。

使用当前配置接口，分别重放两库原失败请求，消息、工具、模型和全部请求 body 均核对相同。每个探针只进行一次 HTTP 请求，不执行图工具：

| 原请求 | 结果 | HTTP 耗时 | 已知输入/输出 tokens |
| --- | --- | ---: | ---: |
| formula 第七批 | 完整响应，合法 submit_changes | 16.69 s | 61,564 / 195 |
| codebase 第三批，首次 | HTTP 502 | 126.84 s | 用量未知，不计为零 |
| codebase 第三批，换 Key 的第二次 | 完整响应，合法读取调用 | 11.67 s | 34,282 / 248 |

另有 formula Key 槽位预检失败，发生在 HTTP 之前，模型调用数为零；当前 Key 文件只含 0..10，原槽位 11..13 已失效。恢复时 formula 使用 8/9/10，codebase 使用 3/4/2，不改变数据或领域规则。

两个 workspace 均已备份；Accepted graph 与 lineage 的 SHA、revision 和精确来源覆盖在恢复前核对通过。新进程首次加载 checkpoint 会再次断言与备份一致；运行记录包含真实模块路径及 transport 文件哈希。除 transport helper 外，冻结的图 schema、prompt、合并逻辑和区域审查覆盖均保留，不从头重跑已接受批次。产物位于 `output/merge_http502_recovery_v98`。

探针成功证明该请求在当前接口完成过，不等于服务从此无故障，也不代替图语义 Assessment。背景运行继续正常评估；新的连续故障会停止并保留 checkpoint，不回到无限重试。

# 独立审查：契约完整性、输入成本与实现边界

本页审查的是拟议方案，不是新的运行结果。没有修改 src、启动后台任务或调用模型。方案可以进入用户 review；下列“已修正”只指文档及离线 schema 的设计修正，生产 controller/editor/adapter 仍未实现。

## 1. 实际收益与成本不能混为一谈

这版方案主要消除整份 Patch 反复生成、工具正文重复回注、旧候选和旧 issue 绑定混乱；它也明确关系义务与可选 Signal 的区别。不能据此宣称每轮只剩很小输入，或者工具 schema 已大幅缩短。

一个稳定的 edit_draft 同时公开八类 change，包含完整 NodeContent、alternatives、来源路由和退役来源约束。它给模型明确格式，也保留固定成本。按需 write_contract 主要延迟语义说明与当前对象的真实来源单元，不会自动把这些已经公开的 JSON schema 从初始请求里移走。

## 2. 工具 schema 的可核查成本

以下是本次最终 schema 固定后的测量。统计口径为双方均经当前 LangChain convert_to_openai_tool 转换后的 parameters 紧凑 JSON 字符数（新协议移除空默认元数据），ensure_ascii=False、键排序、无缩进；不含工具名字/description 包装、System、User、输出保留量，也不是供应商实际 token 数。

旧接口基线取已保存的实际工具定义 writer.tools.actual.json / assessment.tools.actual.json；Writer为六个读取工具与MergePatch，Assessment为同一读取工具集与Review；新角色分别使用其拟议公开工具。两者使用相同计算方法，不能把一边的带缩进文件大小与另一边紧凑请求混比。

| Parameters schema | 原接口字符数 | 拟议终稿字符数 | 差额 |
| --- | ---: | ---: | ---: |
| Writer 全部工具 | 13,669 | 18,007 | +4,338（+31.7%） |
| Assessment 全部工具 | 3,555 | 5,209 | +1,654（+46.5%） |
| 单独写结果/编辑工具：MergePatch → edit_draft | 12,054 | 16,020 | +3,966（+32.9%） |
| 单独评估结果：Review → 新 Review | 1,940 | 3,336 | +1,396（+72.0%） |

拟议读取/提交 schema 分别为 read_nodes 606、read_evidence 143、request_context 1,124、submit_draft 114 字符。新 Review 增加逐 Signal、旧 issue 复核及类型一致性；新编辑工具增加完整八类变更和退役来源路径。因此最终 schema **明显大于原接口**，不是缩小了工具定义。

这组数值已包括最终的单节点普通/冲突重写、普通结果新证据、节点退役逐来源路由、issue.kind 与新回归路径。初轮审查时曾测到 Writer 小幅减少；修补上述必要契约后已经增加，不沿用早期有利数字。

单 Signal 完整 System 也有固定成本；主文档不应把按需内容说成已免费消失。其最终字符范围及与工具合计的下界在下面记录；这些下界尚未包括不可变任务、working view、evidence、canonical draft、feedback 这五条 Human 正文及输出空间。

| 单 Signal 组合 | Writer 字符范围 | Assessment 字符范围 |
| --- | ---: | ---: |
| 共用+角色+输出协议前缀 | 4,164 | 4,092 |
| 加一个当前 guidance 后完整 System | 4,794–5,109 | 4,667–4,900 |
| 再加该角色全部工具 parameters | 22,801–23,116 | 9,876–10,109 |

范围包含八类 signal 和四类强制义务叶子模块中的一个，不代表其余任务材料的上界。当前旧关系 System 为 Writer 10,090、Assessment 10,149 字符；即使新 System 变短，较大的工具 schema 仍会抵消部分收益。本轮进一步精简了System，但工具schema与动态材料的成本仍须单独统计，不能据此给出固定总长度或速度保证。

本次终稿参数集合的规范化 JSON SHA256 为 `a2310443e0f3e50f8ba4b038a1457fad47a1489c5bacc663fb672e866c2bbee7`；review_contracts.py 源码 SHA256 为 `0980f5746c1c0ba9bb20e636a05e3e24e1f3d48bb2efdfc7eb7e92e82a59012f`。这些指纹定位本次测量，不代表模型实际请求或缓存命中记录。

无论最后差额正负，schema 只是请求的一部分。更有价值的比较是完整请求体、实际模型调用次数、重试数、延迟与 usage 缓存字段；本方案没有新的真实调用，因此不声称速度或命中率已验证。

## 3. 完整 guidance 与按需成本

十二份任务guidance各含触发与理想状态、核查材料与例子、Writer调整、Assessment核查。仅拼入当前任务的共用依据与当前角色部分；重复强制义务父总览已移出当前来源。不能把全部模块或另一角色的协议无条件拼进请求。

合并多个真正耦合的 Signal 时，保留各自覆盖要求，但相同 guidance 只出现一次。多个触发共享大 hub，不足以把整个连通分量打包。若一个不可分义务本身过大，应保留明确阻塞或重新规划；不能把完整关系集合截成一页后声称已覆盖。

粗节点也不是省略必需含义的许可。决定公式、匹配或完整版本时仍须补读；新增/实质重构仍需类型写契约。最终输入可能因这些正确读取而增长，预算必须计算真实序列化内容。

## 4. 六条消息与缓存布局的真实含义

六条 messages 固定的是结构，不是长度上限。完整输入预算至少包括：System（共用/角色/协议/当前 guidance）、全部工具、不可变任务、当前 working view、evidence、canonical draft 和反馈；还要另行保留输出空间。只计算节点摘要或 message 数量会重现旧问题。

同角色跨 Signal 的共用前缀与工具顺序稳定，动态 IDs 不写进工具枚举，有利于保持可复用布局。但当前顺序中 working_graph_view 在 evidence/canonical draft 前面：补读改变第三条时，其后内容不能仅因文字没变就被宣传为整段前缀复用。它换来每份正文只有一个位置，属于正确性和缓存长度之间的显式取舍。

按内容共享正文不等于共享来源身份；before/candidate 相同字段也不能丢掉快照绑定。将已驱逐内容重新读回，不应被当成第一次出现的新事实或清空无进展历史。真正命中、token 数和收益需未来真实请求测量；本次仅做静态布局与离线形状检查。

## 5. 复杂度被集中到受信 controller，尚未消失

新编辑协议降低模型重复维护全文的负担，但需要新的实现能力：

- 原子编辑：同一批操作全部成功或全部不执行，版本推进与失败回执均持久化；提交网络重试不重放副作用。
- 八类 change 的归一化：同一图对象可否同时 update/merge/retire、语义后继何时存在、普通/冲突节点的唯一写路径，均需在实现前提供明确组合矩阵。不能只在编译器里保留隐式校验。
- 单节点 node_rewrite：保持已有身份，普通/冲突完整转换，真实 source units 覆盖，准确继承分支关系、metadata 与消费者；旧 conflicts/resolutions 不能直接冒充新能力。
- 节点退役：每个旧来源单元到实际后继的保义路由，不能通过退役重建绕开 normal/common 与已有分支义务。
- 影响闭包与授权：读到节点不自动获得写权；候选编译后 controller 主动补齐内容消费者、入边、metadata、分支和来源影响。
- Review 绑定：旧问题必须明确复核，新的实际回归即使没有原 Signal 也可阻塞；每条状态与当前具体 issue 关联一致。

这些是实施前必须验证的门禁，不应把 review_contracts.py 的形状通过当成真实权限、编译、原子持久化或语义保留已通过。此前节点小样本的真实成功也不能替代这套新 canonical 编辑协议的回归。

## 6. 本轮发现与定稿状态

| 审查发现 | 定稿处理 | 仍需实施的部分 |
| --- | --- | --- |
| 全部原 Signal 满足、只有新回归缺前提/身份反例时无法诚实表达 verdict | issue.kind 明确 defect/missing_context/identity_counterexample；外层按全部当前 issue 类型决定，允许无原 Signal 的受影响回归 | 真实 affected closure、候选/事实/policy 绑定 |
| satisfied 仍挂阻塞 issue，或 Signal 与 issue 单向关联矛盾 | satisfied 必须空 issue_ids，状态与 linked issue 类型一致；两侧关联双向检查 | 语义上是否确实已满足 |
| 普通 node_rewrite 无法附加新的支持证据 | 普通结果增加 evidence_ids；冲突仍用分支/common 证据，不串联 node_update 补来源 | 新单节点 adapter 的真实来源更新 |
| 整节点 retire 可以绕过 normal/common 保义要求 | 禁止节点无后继退役；每个 before 节点的真实单元用 node_unit_routes 恰好映射一次到非空真实后继/分支 | 语义后继、消费者和完整来源 ledger 的真实编译验证 |
| node_rewrite 写契约缺锚点仍能通过，operation_kind 可任意字符串 | 必需锚点与八类操作枚举明确校验 | 动态读写授权，不能从请求 reason 自动授予权限 |
| node_rewrite 的 conflict_points 可指向不存在的输出版本 | 与 node_create 共用准确分支检查 | 完整内容、关系与分支的语义一致性 |
| 更新/创建/合并/退役同对象的组合边界不够明确 | 主契约补充跨操作组合矩阵，shape 提交门禁核对 | 真正从固定 before 编译、事务原子性及确定性恢复 |

在上述设计修正落入终稿后，本次独立审查未发现必须阻止方案交付 review 的剩余问题。这里的“通过”不表示新 runtime 或模型表现已验证。是否将更细类型 schema 动态移出 edit_draft、重排 evidence/working view 以延长缓存前缀，属于后续可测量优化，不应为追求 schema 变小而退回隐藏字段契约。

上述修正应与 System、工具 schema、错误反馈和离线示例同时更新。只改文档而公开旧字段，或只改校验不说明条件，都会再次迫使模型通过重试猜契约。

## 7. 通过用户 review 后的验证边界

后续实施应先做小型、针对性的校验：八类 change 的正反例和跨项组合、同一身份普通/冲突转换、节点退役逐单元来源、旧 issue 关闭/新回归路由、两个快照和事实版本、断电/重试恢复、固定输入预算。真实模型验证可复用少量已有案例与这次已定位的 Metric 退役记录；不需要据此直接重跑全部数据库。

现阶段的结论是：整体职责、Concept 匹配/metadata、当前全局直接物理绑定政策、可选 Signal 与强制义务分离方向一致；新控制协议有明确价值，但成本与能力边界必须按上述实际限制陈述。

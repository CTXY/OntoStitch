# 第三版：四类节点、共用前缀与缓存布局

本版覆盖 **Concept、Metric、Event、GenerationRule 全部四类**，并为 Writer、独立 Assessment 分别生成完整 System。四类均有字段要求、整合/泛化边界、冲突规则及完整 NodeContent 示例，不以 Concept/Rule 两例代替其他类型。

## 1. System：共用部分在前，类型 guidance 严格在最后

```text
所有角色共用的语义标准 common-fields
  → 当前角色的职责与局部输入说明
  → 当前角色固定的输出填写契约
  → 当前 group 唯一类型的 guidance（最后）
```

同一角色的四种类型，最后一个模块之前的内容逐字相同。Writer 与 Assessment 也共享最前面的角色中立语义标准；随后的职责、工具与输出保持独立，不为了缓存把无关能力塞给另一个角色。

上一版将类型 guidance 放在输出契约前面，会在类型处提前打断共享前缀，使后面的长契约无法复用。这一顺序已经在**文档构建器**中纠正，并用四类型拼接与哈希检查确认。生产加载器仍需后续显式接入。

## 2. 请求消息：静态类型、固定局部输入、证据、动态状态分开

| 顺序 | 消息 | 什么内容保持稳定 |
| --- | --- | --- |
| 1 | System | 上述共享前缀 + 类型尾部；没有 group ID、时间戳、候选哈希、正文 evidence |
| 2 | User：静态类型契约 | 本类型实际 node_schema；同 schema 的同类型 group 重用相同文本 |
| 3 | User：固定局部任务 | 完整成员、合并前 relations、来源单元、必要端点/消费者约束；同任务修订时不改写 |
| 4…N | User：受控 evidence 数据 | 已读取原始材料按首次出现顺序稳定保留，新材料只追加；不因 revision 更新而移到不同位置 |
| 最后一条 | User：当前轮状态 | task_id、唯一当前 revision；Assessment 的实际 candidate、change_map、source_routes 也放在这里 |

最末动态消息在每次重建时替换，不把旧版本连续追加。特别是 Assessment 复评：候选本身会变化，应放在 evidence 后面，不能为了缓存保留旧候选冒充当前候选。

这些 User 片段是 controller 数据，不享有 System 指令权限。受控 evidence 区是已完成读取的去重投影，不伪造 ToolMessage，也不包含没有匹配 tool call 的 tool_call_id。一次真实读取进行中仍须保留合法 assistant/tool 配对；在读取完成、准备下一次模型调用的边界才能重建。

无需为了缓存把所有 evidence 提前读完；仍然只读取实际需要的原材料。输入和状态压缩必须保留全部仍有效错误、关键来源、草稿/候选绑定和协议恢复状态，正确性优先。

## 3. 工具定义同样保持稳定

| 角色 | 固定顺序 | 四类型之间是否变化 |
| --- | --- | --- |
| Writer | `read_evidence` → `submit_patch` | 不因节点类型或 group ID 变化 |
| Assessment | `read_evidence` → `Review` | 不因节点类型或 group ID 变化 |

实际类型字段、ID 白名单与操作范围由静态 node_schema 和 controller 强制验证，而不是将当前全部 ID 做成工具 enum，或每组重写工具描述。

本地生成了两份**拟议窄工具 schema**：从现有公开字段导出，删除不属于节点阶段的操作定义；四类型共享各自角色的一份。它们尚未替换生产注册器。字段互斥、保义、ID 白名单和句柄解析等仍须代码支持，不能因为 JSON schema 文件存在就宣称部署完成。

这里的共用前缀首先是 System/消息层的属性。SDK 或网关可能把工具定义编码在 messages 之前，因此即使两个角色的 System 开头相同，工具不同也可能使供应商实际看到的前缀不同。生产接入仍须核对实际发送体与 usage，不能用 System 哈希替代整次请求的验证。

正常轮保持稳定 schema；协议恢复中的“仅允许结果调用”、provider 的必要兼容回退仍是允许的例外。不能为保持缓存而继续开放错误的工具或破坏调用协议。

## 4. 序列化与上下文边界

- 模块从固定版本读取一次，保持模块顺序、分隔符、换行不变；构建时间只写入日志 manifest，不写入 prompt 前缀。
- JSON 对象键使用稳定排序与固定分隔符；不自动改写业务字符串或对全部数组排序。
- `targets` 组顺序与 target_expressions 索引关联；不能为统一哈希而打乱。其他数组也先遵守业务顺序，必要的无序集合规范化应由相应构造器定义。
- evidence 新读取项按首次出现顺序追加；不能每轮按 ID 重新排序旧 ledger，使已有前缀变化。
- 不在 prompt 前面放请求序号、当前时间、task_id、draft_id 或错误文本；必要动态数据留在末尾当前轮状态中。
- checkpoint 恢复保持相同 prompt/工具版本、固定输入和 evidence 顺序；真实版本升级应显式改变 policy 并重建，不冒用旧 Assessment 缓存。

## 5. 可复用前缀不等于实际缓存命中

DeepSeek 当前官方说明将缓存描述为已持久化的前缀单元匹配；共同前缀可能需要先被识别、持久化，服务也不保证每次命中。因此不能承诺第二次相似请求一定命中。实际以返回的 `usage.prompt_cache_hit_tokens`、`prompt_cache_miss_tokens` 判断。[DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)

本任务使用过自定义 URL 和多个 API key。不能据官方服务规则推断第三方网关的实现，也不能假定跨服务、模型、账号、credential 槽位或 Writer/Assessment 共享缓存。这里的自动检查只验证**我们构造的请求是否具备稳定前缀**，没有发真实请求测命中率。

后续接入运行日志时应记录：

- System 整体哈希、共用前缀哈希、完整工具定义哈希、静态类型契约哈希、固定 group 输入哈希。
- 非敏感 service profile、实际模型、角色、节点类型、匿名 credential 槽位编号、prompt/工具版本。
- provider 原始 usage 是否存在，真实 hit/miss token 数、总输入/输出 token、调用时延。

缺失计数表示“未知”，不是“命中 0”。不输出 key，不用 API key 本身生成日志标识。若网关返回其他 cached-token 字段，按该服务实际协议单独适配。

现有 `EpisodeTrace` 会归档响应 message metadata，`stream_audit` 也保留 usage；但仅有 tool_names 不足以诊断完整工具定义是否变化，结构化 hit/miss 汇总仍需接入。LangChain 的本地 `cache=False` 也不能证明供应商的前缀缓存关闭。

## 6. 已执行的缓存布局检查

`cache_layout.py` 是独立的 review 构造器，尚未替代生产 TaskDialogue。它验证：

1. 四类型都生成 Writer/Assessment，且类型 guidance 是最后一个 System 模块。
2. 同角色跨类型的完整固定前缀相同；两个角色的中立语义前缀相同。
3. 同角色工具名称、顺序、完整参数 schema 与描述保持一致。
4. 仅修改 revision 时，前面的消息完全不变。
5. 新 evidence 追加时，旧 evidence 前缀不变。
6. 同类型不同 group 保留相同 schema 前缀；Assessment 更换候选时不改合并前材料与旧 evidence。
7. 对象键插入顺序变化不改变序列化结果；数组原顺序保留。

检查输出到 `cache_validation.json`，其中 `actual_provider_cache_hits=not_measured`。字符数仅用于描述布局，不视为 token 数、缓存 token 或性能收益。

`messages()` 是纯渲染器：它保留传入 ledger 的顺序并检查本次 ID 不重复，但不跨轮维护历史，也未实施 evidence ID 白名单或正文不可变校验。生产接入时须由 controller/checkpoint 管理 append-only 的首次读取顺序、已授权 ID 和正文版本；相同 ID 返回不一致正文应作为数据冲突处理，不能让布局检查代替这项状态保障。

## 7. Sub-agent 审查

已开启三个独立审查方向：[语义、代码契约、缓存布局的发现与修复记录](subagent-review.md)。首轮发现已纳入修改；最终复核状态在记录中单列。没有把“使用了 sub-agent”当作没有错误的保证。

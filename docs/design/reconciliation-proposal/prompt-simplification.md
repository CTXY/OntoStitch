# 本轮 Prompt 精简

本轮按用户提供的 abstract_predicate 示例重排中文 Prompt，修改范围为本目录的拟议协议及 HTML。生产 Reconciliation 尚未接入新协议，原 checkpoint、已完成任务和后台 runtime 不变。

## 1. 当前拼接

`共用图契约 → 当前角色 → 当前角色工具/输出 → 当前 Signal 共用依据 → 当前 Signal 角色指导`

- 共用契约只放范围、保义、Ontology/alternatives 与读取前提。
- 角色模块只说明职责；编辑、提交、恢复在各自输出协议中各写一次。
- 每份 Signal 文件只有四段：触发与理想状态、核查材料与示例、Writer 判断与调整步骤、Assessment 核查要点。前两段共享，后两段按角色选择；不会将整个文件发给两者。
- Writer 不收到 Assessment 核查清单、Review 字段或评估 verdict；Assessment 不收到 Writer 的编辑步骤。
- 结构字段与 enum 的完整定义以工具 schema 为准；Prompt 解释难以从 schema 读出的替换、作用域、来源与判断语义。各 Signal 不重复通用恢复和验收规则。
- 原重复强制义务父总览已移出当前来源；实际仅维护十二个叶子模块，旧文本留在本轮修改前快照。

## 2. 仍须保留的内容

| 规则 | 保留原因 |
| --- | --- |
| targets 多种匹配、直接物理绑定政策 | 避免删匹配组或制造非法竞争。 |
| alternatives 完整内容、起点范围、终点版本限制 | 避免跨分支混搭或错误广播关系。 |
| metadata 的原组合知识 | 去重不等于可以丢顺序、配对或覆盖说明。 |
| before/source/candidate 及真实 source_units | 归档来源不能冒充当前状态或创造来源句柄。 |
| 三层替换语义与节点退役路由 | put_change 替换计划项；字段更新相对 before；完整重写/退役仍须保义。 |
| no-op、旧问题复核、新回退范围 | 不因没有修改就免评，也不因换版本就清掉错误。 |

具体节点类型的完整写入要求仍按 write_contract 获取，不把全部类型的长篇规范塞进每次初始 System。

## 3. 长度变化

<!-- METRICS START -->

| 完整 System | 精简前字符 | 精简后字符 | 减少 |
| --- | ---: | ---: | ---: |
| writer / abstract_predicate | 7887 | 5080 | 35.6% |
| writer / source_selection_variants | 8188 | 5109 | 37.6% |
| assessment / abstract_predicate | 7346 | 4844 | 34.1% |
| assessment / source_selection_variants | 7647 | 4900 | 35.9% |

<!-- METRICS END -->

仅统计完整 System 的 Unicode 字符，包含共享前缀及当前角色 Signal；不是 token、总请求大小或速度测量。工具 schema、五条动态消息和输出预算另计；本轮没有改变工具参数 schema，也没有新增模型调用。

## 4. 旧接口应由代码移除

新工具 registry 本身只注册 Writer 的三个读取工具、edit_draft、submit_draft，以及 Assessment 的三个读取工具与 Review。因此已删除 Prompt 中“不要调用旧 submit_patch/MergePatch 或文件路径”的补丁式限制；角色权限通过实际工具集合与参数校验实现。

生产切换时，清理与实现应成套完成：

| 对象 | 处理边界 |
| --- | --- |
| Reconciliation 的旧结果工具注册与整份 Patch 对话路径 | 接入 canonical draft 后删除，不保留给该角色作为兼容工具。 |
| 仅为旧关系公开接口服务的解析/恢复/包装 | 新协议恢复完成后删除；错误信息与真实失败回归迁入新路径。 |
| MergePatch 内部编译与节点 Writer 适配 | 当前 task_runner、node_relation_repair、node_contract 仍调用；先迁移使用者，再删除失去用途的包装/类型，保留仍需使用的编译能力。 |
| 历史 Prompt 与实现指纹 | 同步清理 loader、宽泛文件 glob、测试及 checkpoint 覆盖签名；不能因删一份历史文件就让已完成任务重跑。 |
| 原始请求、真实失败记录、checkpoint | 保留用于审计与迁移回归，不作为运行接口。 |

这不是依赖 Prompt 禁用旧能力；是区分“新接口已经没有暴露”与“生产尚有调用者”。当前没有删除仍服务运行任务的生产类，也不将拟议工具误报为已部署。

## 5. 校验与缓存

构造器逐个检查二十四份完整 System：只含当前角色段、不含旧接口名字，Writer 不含 Review/评估输出契约；缺失或重复角色段直接构建失败。工具集合按角色固定，Signal 变化仅发生在 System 尾部，task 前缀与六条上下文布局保持原设计。

独立复阅重点为：精简是否漏掉保义/分支边界、schema 是否足以说明可填写字段、旧接口是否真的没有暴露。HTML 和示例重新生成；离线协议校验不等于真实模型或新生产链路验证。

## 6. 按用户新示例统一呈现

当前共用契约及 Writer 模块采用本轮用户附件，Assessment 模块采用消息内的完整示例；共用段不写 Writer 专属的 keep 决定，保持两角色共用。十二类 Signal 均使用相同的标题、加粗语义标签和分层条目，保留原判断边界与例子。共用、角色、工具输出和 Signal 四个模块之间以分隔线分开。

同一 Signal 仍只维护一个来源文件，构造器只选择当前角色段，并分别生成“处理规范”或“核查规范”标题。字段和含义没有随格式改写扩张；工具参数 schema、六条消息布局及稳定前缀顺序保持一致。此轮重点为可读性，字符数按重建结果统计，不以强行压字数删规则。

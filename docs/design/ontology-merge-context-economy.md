# Ontology merge 上下文与修订成本优化

日期：2026-11-08。范围：生产 integration 路径；保留独立 Identity、Proposal、Assessment，以及原有完整编译/提交验证。没有新增业务分类规则、case-specific 判断或语义决策缓存。

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `integration/context.py` | 选择局部输入、来源目录、少量原始证据及直接绑定事实 |
| `integration/packing.py` | 无损共享对象正文、紧凑 JSON、预加载体积限制 |
| `integration/tools.py` | 批量读取和按体积分页；原始证据、完整图仍可按需访问 |
| `integration/revision.py` | 将当前候选图上的增量编辑合成为原始输入上的完整方案 |
| `integration/compiler.py` | 继续执行完整的来源、类型、引用、结构和提交验证 |
| `integration/assessment.py` | 继续按真实图差异提供完整审查范围 |
| `integration/engine.py` | 阶段转换、最后有效方案、失败补丁及上轮审查状态 |
| `llm/prompts/integration_*.md` | 分角色 Markdown prompt，以及共享上下文说明和提交指引 |

代码只处理引用、表示和验证；业务身份、兼容性、冲突、知识组织仍由 Agent 判断。

## 2. 无损上下文共享

输入 `nodes` / `relations` 保持原始含义。候选对象和删除前对象可采用：

```json
{"input_ref":"node-id"}
```

表示完全复用该输入对象。发生修改时，可以只给出完整顶层字段替换：

```json
{"input_ref":"node-id","overrides":{"description":"Updated meaning","aliases":[]}}
```

`overrides` 不是递归 merge：指定字段整体替换；空列表、null 也必须保留。没有指定的字段完全继承输入正文。没有可复用输入、或引用表示更长时，使用完整对象。节点与边使用各自的输入目录。

`expand_payload` 可机械恢复完整对象；受控测试要求还原后与原始 payload 相等。所有实际新增/更新、删除前状态、新契约继续可见；不以 Agent 的修改摘要决定审查范围。超过 60 条的实际边写入仍全部可见。

模型请求使用紧凑 JSON，不改变 Markdown 指引的结构。共享格式仅作用于模型输入；graph_state、编译器、工具的完整对象读取和最终图格式保持不变。

## 3. 有界预加载与工具

- 当前局部来源的原始 NLQ/SQL/evidence 最多预加载 12,000 字符。
- 当前节点引用及已展示邻边直接涉及的物理节点，最多预加载 8,000 字符。
- 只加入完整对象；较大对象保留在目录中，可通过工具读取。不裁剪证据正文，也不推测数据分布。
- `inspect_graph(mode="candidates")` 接受多个 seed；结果按 seed 分页。单 seed 返回格式兼容原接口。
- 普通图查询默认最多 40 项、显式最多 100 项，同时按 16,000 字符控制页面。单个大对象完整返回，避免分页卡住；`next_offset` 按实际返回项数计算。
- 没有把整个数据库放进 prompt；没有改变召回评分或业务 identity 判断。

预加载会增加部分阶段初始输入。其成本收益依赖减少后续工具往返，不能单凭预加载存在就宣称降低费用。

## 4. 增量修订与状态

设原始输入为 S，Identity 对齐后的基础图为 B，最后成功编译的完整方案为 P，候选图为 G = compile(B, P)。

1. 首轮 `proposal_mode=base`：Agent 生成相对 B 的方案。
2. 方案编译成功后保存为 `valid_proposal`；编译失败的方案不成为当前候选图。
3. 后续 `proposal_mode=delta`：候选列表与工具均展示 G。Agent 只提交相对 G 的修改 Δ。
4. 代码在 G 上解释 Δ，计算相对 B 的净编辑，并合成完整方案 P′。
5. P′ 必须重新通过原有完整编译器，再交独立 Assessment。最终提交仍使用完整方案。
6. Identity 重新判断后清空上一套有效方案和上轮审查视图，重新建立 B。

`failed_patch` 只解释失败，不被应用。编译错误后，下一次调用继续以最后有效 G 为起点。`pending_patch` 记录 Agent 的原始增量，避免将已经合成的完整方案误报成待修复的增量。

### 来源与结构重写

- `changes.before` 始终指向不可变原始来源，不改为临时候选 ID。
- `prior_changes` 提供已建立的对应关系；未受影响的对应保留。
- 新对应若替换旧对应组，必须覆盖该组全部 before 元素，避免丢掉多来源中的一个。
- 删除/拆分旧 after 目标必须显式更新对应；代码不猜测路径或语义等价。
- 更新新建节点仍使用 update；合成为完整方案时才转换成相对 B 的 create。创建后又删除的临时节点不进入最终净编辑。
- 所有引用与 relation_metadata 仍由 Agent 显式修复；无自动删除级联。
- 现存 ID 的类型不可原地改变，包括上一轮刚创建的 ID。

增量 trace 的 `parsed_response` 是本轮 patch；workflow 的 `proposal` 是合成后的完整方案。离线审计同时支持旧的完整方案 trace 与新的增量 trace。

## 5. Assessment 与 prompt

Assessment 保留三部分：决策正确性、节点/关系质量、支持的知识是否保留。新增 `previous_review` 与代码计算的 `changes_since_review`，帮助后续轮次先验证旧问题，再检查新修改及影响。

这不是已通过知识缓存。未修改的 incoming 知识没有自动豁免，真实写入/删除仍全部呈现，必要时仍可扩展查询。

共享 Markdown prompt 说明 input_ref/overrides、预加载信息、批量工具及最终提交方式。明确通过结构化工具提交，不先输出一份长篇文本草稿。格式失败后的重交指引也存入 Markdown。保留现有服务兼容的 auto tool choice；未强制切换未经验证的供应商输出模式。

Operation registry 的实际读取量很小，本次没有为了压缩它而删掉必要指引。

## 6. 可控对照与结果

执行入口：

```sh
.venv/bin/python scripts/benchmark_graph_integration_economy.py \
  --baseline-run output/ontology_merge_v64/live_r5 \
  --output output/ontology_merge_v64/economy_v1/controlled_comparison.json
```

此实验固定原来三个 case 的语义判断，回放新的真实 controller；没有付费模型调用。使用本地 `cl100k_base` 作为统一 token 长度代理，不是 DeepSeek 的计费 tokenizer。

| 对照项 | 原表示 | 新表示 | 结果 |
| --- | ---: | ---: | --- |
| 第一轮 Assessment 原 payload 的共享/压缩 | 23,196 | 13,891 | 减少 40.11% |
| 第二轮 Assessment 原 payload 的共享/压缩 | 23,830 | 14,882 | 减少 37.55% |
| 第三轮 Assessment 原 payload 的共享/压缩 | 24,030 | 15,063 | 减少 37.32% |
| 第二轮修改输出 | 4,437 | 3,594 | 减少 19.00%；15 项编辑改为 7 项 |
| 第三轮修改输出 | 4,392 | 2,604 | 减少 40.71%；16 项编辑改为 4 项 |
| controller 七阶段初始 payload 合计 | 126,006 | 108,528 | 减少 13.87% |

最后一行已包含预加载、新候选视图和上轮 Assessment 信息。它排除 system prompt、工具 schema、工具结果和阶段内重复请求，不能当作总账单降幅。

计入预加载后，Identity/首轮 Proposal 的初始 payload 分别增加约 5.3%/4.6%，两次修订 Proposal 增加约 1.0%/0.8%；三轮 Assessment 则减少约 31.2%/23.3%/25.6%。这里保留负面结果，避免仅展示局部压缩比例。

固定决策回放证明：最终节点、边、关系契约、sources、全部来源映射和结构 rewrite 与 live_r5 一致。它没有证明模型读取新格式后一定做出相同决策，也没有证明 reasoning tokens 或整体付费调用数已下降。

### 回归覆盖

新增测试覆盖无损共享、字段替换、局部预加载/超大证据回读、批量候选等价、大小分页不丢项、增量/完整方案一致、来源继承、临时节点更新/删除、失败不推进状态、Identity 重置、对应组不允许部分丢失、删除 rewrite 目标必须修复、禁止新建节点原地改类型、结构化提交恢复、增量审计回放。

全量回归在追加最后一条审计回放测试前为 972 passed；追加后成本优化专项 14 passed（包含该审计回放用例）。Ruff 检查通过。未重新进行下游 SQL 执行评测。

### 后续真实模型对照

下一步若进行 live A/B，应记录缓存/非缓存输入、reasoning/可见输出、工具错误、修订轮数、最终语义质量与映射完整性。结构化提交指引是否减少重交、预加载是否避免读取、增量视图是否减少重复推理，仍需实际调用验证。不能通过删掉 Identity/独立 Assessment 或强行限制为一轮来制造成本下降。

## 7. 真实模型验证：r6、r7 与受控增量 r8

日期：2026-11-08。实际调用已授权的 `https://api.deepseek.com` / `deepseek-v4-flash`，关闭 LangSmith。r6/r7 与 r5 的原始图、来源 evidence、schema、runtime 配置指纹、top_k 和迭代上限一致；未发送完整数据库。

### 结果及失败记录

- **r6（完整真实运行）**：9 次模型调用，代码审计通过，模型 Assessment accept。但额外语义复查发现 FRPM Rule reason 保留了“存储值可能预先舍入或采用不同基数”的无证据理由。因此记录为 revision_required，不当作质量保持的成功样本。
- 据此加强通用指引：分别审查 action 和 reason/定义/说明；正确 action 不能使推测性理由成立。明确 distinctions 不可同时包含已合并节点对、候选查询使用 canonical IDs；Identity 修正成功后清除已解决错误反馈。没有增加案例特定代码。
- **r7（完整真实运行）**：10 次模型调用；FRPM 推测问题消失，Identity/工具未报错，模型 Assessment accept。额外复查仍发现等价 `has_attribute` 契约同时在使用，记录为 revision_required。主要业务语义保留，但未把这一遗漏掩盖为完全自动达标。
- **r8_incremental（受控真实增量验证）**：以 r7 的已完成 Identity/完整候选方案和复查反馈为起点，准备阶段明确记录 `reviewer_seed_not_model`、模型调用为 0；后续 Proposal 和独立 Assessment 均真实调用模型。Proposal 6 次、Assessment 2 次。模型只提交一个 update_edge，统一 grade-span 关系的谓词；未重写其他节点。

r8 最终对照 r7：节点、关系契约均无变化；仅一条边的 edge_type 改变，端点、description、sources 保持不变。最终 Assessment accept，额外复查确认已发现问题修复。274/274 默认物理来源边映射完整，全部来源具有映射或明确 rewrite，实际修改均进入 Assessment。最终仍为 12 Concept、1 Metric、2 GenerationRule、1 Event，18 条业务关系；含物理目录共 115 节点、129 边。

### 实际供应商 usage

| 范围 | 模型调用 | 输入 tokens | 非缓存输入 | 输出 tokens | reasoning tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| r5 基线 | 44 | 1,539,617 | 209,825 | 123,636 | 95,067 |
| r7 完整运行 | 10 | 314,837 | 90,069 | 52,481 | 45,178 |
| r8 定向增量修订 | 8 | 255,169 | 24,385 | 28,951 | 27,019 |
| r7 + r8，获得最终 review 图 | 18 | 570,006 | 114,454 | 81,432 | 72,197 |
| 本次全部试验，含失败 r6 | 27 | 773,908 | 177,812 | 120,040 | 103,589 |

每次调用均有 usage 记录。r7+r8 相比 r5：调用减少 59.09%，输入减少 62.98%，非缓存输入减少 45.45%，输出减少 34.14%。这是观察到的 token 用量，不是美元账单，也不是各优化项独立贡献的估计。不能只用较便宜但有质量遗漏的 r7 来代表最终产物的全部成本。

### 解释范围

1. 相比旧运行，本次获得可 review 图的观察成本下降；但训练案例只有三个，不能宣称平均节省比例或所有场景质量保持。
2. r7 自动 Assessment 漏掉了契约规范化，r8 使用外部复查反馈。因此本次验证不能证明完全自动流程已经稳定可靠。
3. r8 确认真正的增量 Agent、编译合成、独立 Assessment 可以协同工作；它不是离线固定决策回放。但一个单边修改仍需要 6 次 Proposal 底层调用，存在重复读取和推理成本，不能把输出变小等同于调用变少。
4. 修订后的 Rule 和 Concept 没有进行下游 NL2SQL 执行评测。本次质量结论只覆盖图语义、来源保留及结构完整性。

### 可审查产物

- `output/ontology_merge_v64/live_r6/semantic_review.json`：首轮失败发现。
- `output/ontology_merge_v64/live_r7/cost_comparison.json`：相同输入与配置的完整真实运行对比。
- `output/ontology_merge_v64/live_r7/semantic_review.json`：第二轮规范化遗漏。
- `output/ontology_merge_v64/live_r8_incremental/manifest.json`：增量实验的种子、复查反馈与真实调用范围。
- `output/ontology_merge_v64/live_r8_incremental/validation_driver.py`：本次受控实验驱动；不是生产业务规则。
- `output/ontology_merge_v64/live_r8_incremental/validation_costs.json`：所有试验成本，不隐藏失败运行。
- `output/ontology_merge_v64/live_r8_incremental/semantic_review.json`、`integrity_audit.json`：最终语义复查与机械审计。
- `scripts/compare_graph_integration_runs.py`：通用 provider usage 对比入口，不推测价格或语义等价。

新增 Identity 错误反馈生命周期回归；integration 相关 54 项测试通过，随后新增反馈回归后 economy 专项 15 项通过。Ruff 通过。审查页面区分真实调用与回放/人工复查准备阶段。

## 8. 聚焦修订与 Assessment 漏检对照（2026-11-08）

### 实现及边界

- `integration/scope.py` 集中实现机械上下文选择。Assessment 的 `issue_refs`（kind/id）选择修订根对象；契约选择实际使用它的边，节点选择邻接边，随后纳入端点、直接内容消费者、声明引用和相关证据。这里不判断业务等价，不递归遍历整张业务图。
- 修订 packet 直接提供完整候选对象、相关对应组和在用契约。代码仍保留完整历史 proposal 与全部来源映射，局部模型输出经 `compose_revision` 合成为完整变更，再做全量编译。工具保留全图访问；没有引用、引用失效/被删除或存在失败 patch 时回退原完整上下文。局部修订不缩小独立 Assessment 的审查范围。
- `inspect_graph(consumers)` 只返回声明的反向内容引用与 relation_metadata，不再把普通邻接边混入。邻接关系由 relations 查询承担；contracts 查询现在遵守 input/candidate view。
- Assessment 保留“决策、知识质量、意义保留”三层。质量层分别检查节点含义、单条关系和在用谓词的一致性。`active_contracts` 提供完整契约与实际使用边，供 Agent 判断是否同义；代码不按名称或端点类型合并谓词，也不要求删除未使用的历史契约。
- 首轮真实对照仍漏检 Rule 的无证据理由，因此补充 `rationale_review_ids` 和逐条 `rationale_checks`：每条相关 Rule 用一句话比较 reason 的主张与原始证据。输出模型只检查是否逐条覆盖、是否把明确不支持的理由错误标为 accept；语义真假仍由模型判断。不是新增 Agent，也没有按学校/FRPM 等案例编码规则。
- 提交失败诊断区分 invalid tool JSON 与没有结构化提交。真实首轮发现参数末尾多一个 `}` 被误提示为未提交；现在反馈具体 JSON 错误位置，不尝试修复/猜测模型的业务内容。`invalid_tool_calls` 独立计入成本报告。
- Prompts 仍集中在 `llm/prompts/integration_*.md`，使用 Markdown。Operation registry 按需读取，未新增重复注入。

### 可复现实验

入口：`scripts/validate_graph_integration_focus.py`。旧实现快照保存在 `output/ontology_merge_v64/focus_v1/baseline_src`，通过 PYTHONPATH 选择旧/新实现。

修订对照固定 live_r7 的完整候选方案、Identity、原始图、evidence 和 live_r8_incremental 的原反馈。新增 issue_refs 只标注反馈已经指明的边/契约 ID，不增加语义结论。三项准备阶段为 0 次调用；修订 Proposal 和后续独立 Assessment 都调用真实 DeepSeek。两边配置指纹、原始数据、schema、迭代上限和 evidence 均相同。

另以 R6（无证据理由）、R7（重复在用谓词）、R8（已修正）分别做无预期答案、无缺陷提示的真实 Assessment 对照。语义判断不是根据 verdict 自动推定：核对实际 issues/rationale_checks 是否指出预设缺陷，并检查最终图差异及来源映射。

### 检测结果（每项每版本一次观察）

| 固定候选图 | 旧版重跑 | 首轮优化 | 最终优化 |
| --- | --- | --- | --- |
| R6：推测理由支持 factual non-equivalence | revise，正确指出 | accept，仍漏检 | revise，逐条理由检查指出缺陷 |
| R7：两个等价谓词同时在用 | accept，漏检 | revise，正确指出 | revise，正确指出 |
| R8：已修正对照 | accept | accept | accept |

最终对本次两个已知缺陷均检测成功，干净对照未误拒绝。不能把三个候选图的一次观察解释成一般漏检率/误报率估计。新增检查不保证每个语义判断正确；代码覆盖检查也不等于语义正确证明。

### 相同反馈下的修订成本

| 项目 | 旧版本次重跑 | 最终优化 | 变化 |
| --- | ---: | ---: | ---: |
| Proposal 底层模型调用 | 3 | 2 | -33.3% |
| Proposal 读取工具调用 | 4 | 2 | -50.0% |
| Proposal 初始 payload 字符 | 67,319 | 16,875 | -74.9% |
| Proposal 输入 tokens | 82,224 | 18,813 | -77.1% |
| Proposal 输出 tokens | 10,052 | 1,235 | -87.7% |
| Proposal + Assessment 模型调用 | 6 | 4 | -33.3% |
| 两阶段读取工具调用 | 8 | 4 | -50.0% |
| 两阶段输入 tokens | 158,718 | 73,904 | -53.4% |
| 两阶段非缓存输入 tokens | 17,662 | 15,664 | -11.3% |
| 两阶段输出 tokens | 16,825 | 10,024 | -40.4% |

历史 R8 Proposal 为 6 次调用，但本次旧实现重跑为 3 次。因此主要对照采用本次重跑，不用历史 6 次夸大效果。首轮优化 Proposal 因两次无效 JSON 提交用了 4 次，其失败记录保留。最终运行没触发该 JSON 错误，所以准确重试反馈的独立节省量尚未由真实对照证明，只通过了可控错误输入测试。

### Assessment 质量与成本的取舍

| 盲测候选 | 旧版调用/输入/输出 | 最终调用/输入/输出 |
| --- | --- | --- |
| R6 | 2 / 40,392 / 12,036 | 3 / 97,878 / 12,858 |
| R7 | 1 / 19,806 / 12,197 | 2 / 55,599 / 8,701 |
| R8 | 6 / 232,976 / 34,049 | 2 / 55,479 / 13,768 |

并非每项评估更便宜：两个问题图的输入增加；更强的评估不应为了省 tokens 而省掉必要检查。三个盲测合计模型调用 9→7，输入 293,174→208,956，输出 58,282→35,327；但非缓存输入 **27,702→55,740**，受读取与缓存命中影响。不能把总输入降幅当作账单降幅。

本轮所有真实试验（旧版、首轮失败、最终复测全部计入）：40 次底层调用，输入 1,068,667（缓存 878,208、非缓存 190,459），输出 181,139，其中 reasoning 164,031。每次都有 usage 记录。费用报告没有假设美元单价；所有数据均来自 provider usage。新旧调用顺序与缓存状态无法完全等同，因此不宣称独立因果效应或长期平均节省。

### 最终图与验证

最终 `focus_v1/final_revision` 为 115 节点、129 边；业务层 16 节点、18 边。与 R7 相比，节点、端点、sources、关系契约均未改变；一条边改用统一谓词，同时在 description 重述相邻 grade-span Concept 已有的“空代码表示未记录”限制。与旧版对照图相比仅此 description 文字不同，因此不是字节完全相等。额外复核认为没有新增业务结论或丢失已有含义。

独立模型 Assessment accept；机械审计确认 274/274 默认物理来源边映射、完整来源对应、类型安全映射和实际修改的 Assessment 可见性。未重新执行下游 NL2SQL，不能据此声称 SQL 准确率提升。

产物：`focus_v1/revision_comparison.json`、`all_experiment_costs.json`，各 `*_assess_*/review.json`，以及 `final_revision/{semantic_review,integrity_audit,workflow,graph_state}.json`。完整真实交互保存在各运行目录的 `agent_traces.jsonl`。


验证脚本通过 `--issue-refs output/ontology_merge_v64/focus_v1/feedback_refs.json` 读取本次固定反馈的对象标注；脚本和生产逻辑没有硬编码本例节点/边 ID。重跑时使用新的 `--out` 目录，保留原实验记录。

最终验证：全量 **980 passed**；涉及文件 Ruff 检查通过。新增测试覆盖局部选择/回退、完整 Assessment 范围、消费者与邻接区别、契约 view 隔离、逐条理由覆盖与 verdict 一致性、无效 JSON 的准确诊断且不擅自修复输出。

## 9. 精确读取与上下文去重（2026-11-09）

### 已实现

1. **精确目录查找**：`inspect_graph(mode="nodes", table=名称或ID, column_names=[精确列名])` 返回完整字段对象。单给 table 返回表定义；不存在的列在 `missing_columns` 中明确说明。重名表返回候选表 ID，必须用 ID 消歧；不任意选择、不模糊匹配、不执行 SQL。与原始 IDs 查询互斥，遵守 input/candidate view 和分页。
2. **关系类型过滤**：`edge_types` 在分页前过滤真实 predicate ID；不改变边的内容、范围和来源。查询 join 可指定 `joins_with`，无需遍历 belongs_to。省略过滤仍能读取完整邻接关系，没有隐藏全图访问。
3. **相同契约定义共享**：`active_contracts` 中与 `local_contracts` 完全相同的定义改为 `contract_ref`，只保留一次定义和使用边索引。不同内容的同 ID 候选契约仍完整保留，避免错误共享不同版本。`expand_payload` 可恢复原始输入，本次三个真实 Assessment 的解包输入与上一版逐项相等。
4. **Rule 当前内容直接可读**：`rationale_review_ids` 中的候选 Rule 提供完整节点，不再要求 Agent 合成 input_ref + overrides；原始来源版本仍保留供核对。没有添加第二份 reason 摘要或新 Agent。
5. **Markdown 指引**：明确“核验不等于重新查询”，缺列用精确目录查询、关系按类型读取，部分分页不能视为完整图。评估摘要应聚焦实质判断，避免多处重复 rationale_checks。这项简洁性指引没有硬性截断模型输出，实测也没有保证所有结果更短。

实现分工：`integration/catalog.py` 负责目录选择；`tools.py` 负责工具校验/过滤/分页；`packing.py` 负责可逆共享与 Rule 呈现；`llm/prompts/integration_context.md` 和 `integration_assess.md` 统一管理指引。新增代码没有案例字段、业务值或节点 ID 的特殊处理。语义合并仍由 Agent 决定。

### 测试与固定输入

新增 6 项回归覆盖精确字段、缺失字段、同名表消歧、view 隔离、过滤前分页、契约版本不混淆及 Rule 完整呈现。最终全量 **986 passed**，涉及文件 Ruff 通过。

真实运行目录 `output/ontology_merge_v64/reads_v1`，仍使用用户授权的 DeepSeek 和 cases 47/60/77 输入，关闭 LangSmith。三张盲测图分别来自 live_r6、live_r7、live_r8_incremental；不提供缺陷提示/预期 verdict。基线复用上一轮 **focus_v1/final_assess_*** 的真实记录，本次没有再次付费重跑旧版。逐项确认 source、config_fingerprint 和 **解包后的整个初始 payload 完全相同**；新旧区别为呈现、prompt 与工具能力。保留修改前代码快照在 `reads_v1/baseline_src`。

固定 payload 的纯格式检验（不是模型行为证明）：

| 候选 | 原 payload 字符 | 新 payload 字符 | 信息恢复 |
| --- | ---: | ---: | --- |
| R6 | 75,668 | 73,431 | 完全相等 |
| R7 | 75,806 | 73,002 | 完全相等 |
| 已修正图 | 75,859 | 73,622 | 完全相等 |

共享契约节省的部分空间用于完整呈现 Rule，所以初始 payload 减少幅度并不大；主要目标是降低后续不必要的查询和上下文累计。

### 真实盲测结果

| 候选 | 旧/新 verdict | 模型调用（旧→新） | 输入 tokens（旧→新） | 输出 tokens（旧→新） |
| --- | --- | ---: | ---: | ---: |
| R6 无证据理由 | revise / revise，均指出 n301 不等价理由没有支持 | 3→2 | 97,878→48,757 | 12,858→10,538 |
| R7 重复在用谓词 | revise / revise，均指出两个 has_attribute 等价 | 2→2 | 55,599→49,250 | 8,701→17,809 |
| 已修正图 | accept / accept | 2→2 | 55,479→50,771 | 13,768→12,440 |

三图合计：调用 **7→6**，读取工具调用 **8→6**，输入 **208,956→148,778（-28.8%）**，非缓存输入 **55,740→37,930（-32.0%）**，但输出 **35,327→40,787（+15.5%）**。R7 reasoning tokens 增加，不能把输入节省等同于总费用下降；未根据单次观察宣称长期平均或单项因果效应。

工具行为：

- 三图都没有再读取 32 belongs_to + 1 joins_with 的整表关系页。
- R6 读取 Charter/Virtual 两个未预加载字段及 frpm 表定义，没有重新读取 5 个候选节点、7 条候选边。读取结果总字符 **34,199→3,910**。
- R7 使用精确列名查找 stored percent / numerator / denominator，另读四条明确指定的边；读取结果字符 **21,306→4,067**。仍有少量预加载对象被重读，没有宣称彻底消除冗余。
- 已修正图用两个精确字段查询替代整表邻接；读取结果字符 **17,488→7,438**。
- `edge_types` 在本轮真实盲测中未被调用；其过滤与分页正确性由受控测试验证，不把未发生的真实使用计为收益。

### 端到端修订与残留成本

另运行同一 R7 候选和同一人工反馈的修订流程（`reads_v1/revision`）：准备的 identity/初始 proposal/反馈不调用模型，后续 Proposal 和独立 Assessment 真实执行。最终 accept，115 节点、129 边，274/274 默认物理来源边映射完整，全部来源对应与 Assessment 修改可见性通过审计。

相对 R7，所有节点、端点、sources、契约保持不变；一条边统一 predicate，并在 description 重述相邻 grade-span Concept 原有的空代码表示未记录含义。不是字节相同，未发现新增业务结论或意义丢失；未进行下游 NL2SQL 执行。

本次 Proposal 4 次调用中出现 **2 次无效 JSON（Extra data）**，之后成功，Assessment 2 次。相对上一轮相同反馈的最终修订：总调用 **4→6**，输入 **73,904→85,668**，输出 **10,024→14,693**。读取工具调用从 4 降至 2，但格式重试抵消了收益。这部分保留为负面结果；本轮没有为了改善数字而重复运行挑选较便宜结果，也没有增加静默修补模型 JSON 的逻辑。

本轮全部真实运行：**12 次调用，输入 234,446（缓存 181,760，非缓存 52,686），输出 55,480，其中 reasoning 49,956**。包括两次格式失败，全部调用都有 usage。模型/缓存波动和只有一次观察的限制依然存在。

### 产物

- `reads_v1/comparison.json`：三图盲测、无损信息对照、全部新调用成本和实际读取参数。
- `reads_v1/revision_comparison.json`：相同反馈的端到端修订对照，包括负面结果。
- `reads_v1/assess_*/review.json`：独立模型盲测判断。
- `reads_v1/revision/semantic_review.json`、`integrity_audit.json`：图差异复核与机械审计。
- `reads_v1/revision/index.html`、`graph.html`：review 页面和交互图。

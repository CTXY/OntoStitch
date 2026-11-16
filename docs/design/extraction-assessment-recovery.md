# Extraction Assessment：契约反馈与恢复

## 问题与边界

Assessment 同时判断知识质量、局部 SQL 应用、固定目标和回归。合法的评估输出不等于 case 通过；评估可以拒绝候选，并把具体问题交回 Learning。

2026-11-16 的三个实际失败输入暴露了以下链路：

1. 输入已有 `application_requirement`，但输出 schema 只列通用枚举。模型把部分 Concept、DimensionValue 或依赖边写为 `not_applicable`，忽略了这些具体 unit 的 SQL 应用义务。
2. 校验器只抛出首个错误，例如 “SQL application is required for this unit”，没有 unit ID。存在多项错误时，一次格式纠正不能有效覆盖全部问题。
3. 格式反馈要求原样保留实质判断，却没有区分“保留发现的缺陷”和“纠正不兼容的状态标签”。
4. 外层运行器把 `Assessment output invalid` 当成 transient，反复恢复到同一个 `assess` checkpoint；格式重试没有进入 Learning，也没有消耗正常的语义迭代，导致持续空转。

这不是降低评估标准或增加运行轮次能解决的问题。

## 修订后的输入与输出

保留逐项 `quality_checks`、应用判断和缺陷反馈。Agent 不再手工输出可推导的总体 `quality`，由代码汇总：任一必查项 invalid 则总体 invalid，否则存在 inconclusive 则总体 inconclusive，否则 valid。内部结果仍保存该汇总供现有接受策略和 checkpoint 使用；显式携带总体字段的旧记录仍严格检查一致性，不静默改写旧结论。

呈现层为每个 unit 添加一个只读输入 `application_options`，由代码根据已有 requirement 和 inclusion 记录计算：

- `sql` 排除 `not_applicable`。
- 不是每次验证都包含该知识时，排除 `applied`。
- 所有验证均未包含该知识时，排除 `partially_applied`。
- 每次验证都已包含时，排除 `not_attributed`。
- 支持的业务结构、表示关系和删除仍保留原有语义应用／删除安全性契约。

代码只列合法标签，不判断业务行为是否落实，不替模型选择 `applied`，不制造反馈、不修改图。例如某 Concept 要求 SQL 应用且所有上下文均缺席，模型应从 `not_applied`、`not_attributed`、`inconclusive` 中结合证据判断，并提供必要的应用／检索反馈，不能用 `not_applicable` 跳过。

每项质量检查、固定目标、来源、最小化和业务指代要求保持不变。质量或 target 反馈不替代失败应用所需的 retrieval/selection/application 反馈。

## 校验与恢复

`assessment_models.py` 一次汇总各 unit 的独立契约错误，指明 `unit_results[实际ID].application` 或 `.feedback` 等位置。错误不自动转化为通过，也不修改模型结论。

`feedback.py` 在原评估对话中保留一次格式纠正机会，提供完整错误列表。纠正需要保留观察到的缺陷，但允许根据原始 SQL/context 证据修正无效状态标签，并补齐反馈。不能为了满足 schema 改写成通过。

若仍无效，抛出 `AssessmentOutputInvalid`。运行器不再将这类错误当成连接故障重启整例；保留 checkpoint 并报告 `needs_attention`。真实网络错误仍走原有恢复路径。内层 5 轮、外层 5 轮及正常模型调用预算未改变。

合法的拒绝评估继续原工作流：过滤／保留符合条件的单元，把可执行反馈交回 Learning，重新验证和评估；不启动另一个外层格式重试循环。

## 已有运行的显式恢复

默认的 CaseWorkflow 兼容性校验未放宽。此次后台使用冻结 runtime 和独立的、可审计的恢复脚本：

1. 用旧 Assessment policy/prompt 指纹替换新 context 中的这两个分量，必须精确重建旧 context ID；否则拒绝升级。因此源数据、数据库版本、配置、图 schema、Learning 和 SQL 生成契约必须一致。
2. 备份 checkpoint；保持 Accepted/Working revision、groups、targets、迭代计数及下一步不变。
3. 原验证证据不覆盖；在新 context 下复制相同的 SQL 验证事实，并显式记录旧／新引用映射。既有接受证明保留原政策与内容，不伪造新评估。
4. 仅重建派生验证索引，通过 LangGraph 的 `update_state` 创建新 checkpoint，验证工作流位置未改变。升级过程核对所有 ontology 文件未改。

该脚本位于 `output/assessment_contract_v94/policy_resume.py`，仅用于这次已授权的冻结后台升级，不是自动绕过 context 校验的生产分支。实际加载路径、代码和 prompt 哈希记录于该目录的 `loaded_runs`。

## v94 小范围真实验证（2026-11-16）

使用三个原始失败候选和相同 SQL/context 证据，不注入针对性的语义修复答案。最终版：

| 固定输入 | 调用数 | 输入 tokens | 输出 tokens | 结果 |
| --- | ---: | ---: | ---: | --- |
| debit 1526 | 1 | 15,608 | 3,959 | 合法拒绝评估，交回 Learning |
| student 1338 | 1 | 12,480 | 3,131 | 合法拒绝评估，交回 Learning |
| codebase 635 | 2 | 31,271 | 7,903 | 一次格式纠正后合法拒绝，交回 Learning |

最终版共 4 次调用；前一修改轮另有 6 次调用，包括两个仍未纠正成功的测试。全部小测合计输入 150,276、输出 35,677 tokens，不把失败测试排除在成本外。它们证明评估接口能返回可处理反馈，不证明三个 case 已达到 stable_correct。

相关回归 106 项通过。后台从原 `assess` 位置恢复后，三个任务均已进入 Learning 处理实际问题。任务达到终态后，由后台脚本恢复对应数据库的 coordinator/watchdog；持续的契约失败会停下并报告，不再次无限重启。

card_games 单独复测原第七批失败请求：非流式一次调用约 5.85 秒完整返回 `inspect_graph`，输入 42,291、输出 93 tokens；未执行图工具或把探针结果当成图修改通过。来源审计后已从第七批 accepted revision 6 恢复；原 96 份 case 图与 6 份已发布批次图保留。产物在 `output/card_transport_v94`。

## v96：移除重复的总体质量判断（2026-11-17）

codebase 后续候选的失败输出把五项质量检查全部标为 valid，同时写总体 quality=invalid。首轮格式纠正后，模型补充 quality 反馈，仍未消除两个标签之间的矛盾。错误并非缺少 SQL 验证：实际 prompt 已含候选、14 个 unit、SQL attempts 和反馈规则；这里是让模型重复填写一个可由代码计算的值。

运行时 Response schema 现不再暴露总体 quality，并显式要求 quality_checks。模型仍须覆盖每项必查标准、判断具体缺陷并提供反馈；缺少检查项或失败项缺少反馈仍被拒绝。代码只派生汇总，不删除检查或反馈，任一缺陷反馈仍阻止相应单元／耦合组接收。旧显式矛盾结果仍拒绝；不是将历史 invalid 强行转换为 valid。

同一失败候选的真实测试：首次 HTTP 尝试遇上游 502；受控重试的 Assessment 使用 2 次调用，输入 28,257、输出 5,666 tokens，返回合法的拒绝评估。两次结果均未再输出总体 quality；第一次纠正仅涉及将 target ID 用作反馈 location，第二次改为实际节点 ID。最终 Metric 的 minimality、support、graph_consistency 为 invalid，且保留 quality/application 反馈，不能计作 case 通过。网络失败的 token 用量未知，不计为零。

另发现 codebase 的一个 Learning 超限属于本地预算错误：thinking=disabled 时，历史响应的 reasoning_content 不会发送给模型，但消息预算仍计算它。原始 AIMessage 序列化大小 160,225 字符，其中 reasoning_content 154,933 字符，已超过整例 160,000 字符预算。

`model_history_view` 只为当前请求建立副本：去除不发送的审计元数据；仅在显式关闭 thinking 时去除该视图中的 reasoning_content。原始响应、工具调用、工具结果和原始历史不变；启用 thinking 时保留其协议字段。历史回放中预算视图为 1,671 字符，序列化后的实际模型请求完全相同。这修复的是错误的本地超限，不声称减少了原本就未发送的 API input tokens。

相关回归 139 项通过。codebase 使用 `output/codebase_recovery_v96/runtime` 的冻结代码恢复，加载路径／哈希记录在 `loaded_runs`；正常运行的其他数据库不滚动升级。兼容性预检覆盖四个暂停案例，允许且仅允许旧 Assessment policy/prompt 版本迁移；86 份已通过 case graph 的哈希保留。恢复后的抽取达到终态才按原规则进入 merge，不把合法拒绝或网络恢复等同于 SQL 通过。

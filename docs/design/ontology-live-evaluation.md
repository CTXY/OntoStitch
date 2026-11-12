# Ontology extraction：真实运行与问题定位

> 历史实验记录：对应旧测试产物已于 2026-11-04 按用户要求删除；保留本文作为设计与实验历史。当前结果见 [v50 匹配与关系要求](ontology-matching-and-relations-v50.md)及新版 review。

后续纠正：本文为上一轮历史记录；其中 closed school count 的 Metric 判定过宽，已在 [两例 review 规范](ontology-two-case-review.md) 中纠正。最新以该文档为准。

日期：2026-11-03。范围为 case-level extraction training，不重设计全局 merge，不优化 metadata。

本记录使用真实模型、原始 BIRD NLQ/Gold SQL 和只读数据库。所有图均取运行结果中 `accepted_revision` 对应的保存快照；没有手工修图或使用脚本响应替代模型输出。完整输出与全部失败轮次保存在 `output/ontology_live_eval/`；可运行 `scripts/build_ontology_live_review.py` 重建 review 页面。

## 评估方法

质量检查分开记录，不以 SQL 通过替代语义通过，也不计算知识覆盖率：

| 维度 | 检查内容 | 判定方式 |
| --- | --- | --- |
| 硬契约 | 节点字段、Concept 非空分组匹配、边端点、description、实际直接绑定唯一性、公式引用和必要关系 | 写入校验、回归测试、最终接受图重新校验 |
| 节点语义 | Concept 最小完整含义；存储量与计算的区别；Metric 最小性；Event 结构证据；Rule 独立行为 | 内部 Assessment、独立 subagent 阅读实际输出 |
| 关系语义 | 谓词与端点含义一致；类别与属性槽区分；数值与群体区分；状态和日期的限制；历史事件与当前属性的区别 | 逐边检查 description 和原始证据 |
| 规则适用性 | 原子行为；不重复绑定或公式；条件覆盖完整动作；方向、选择基数与使用方式一致 | 原始请求、Gold SQL、生成 SQL 和实际节点对照 |
| 执行与接受 | SQL 执行及结果等价；稳定 replay；Direct 完成；Assessment/修复结果 | 分别读取运行记录，不能只看外层 passed |
| 回归 | 原有正确行为是否保持，工具拒绝是否准确且原子 | 全量测试和保留每轮原始 artifact |

独立 reviewer 不修改代码或图，报告见 `output/ontology_live_eval/independent_semantic_review.json`。没有要求 Assessment 新增数据库探索证据链；其使用已有 case、metadata、图与 replay 输入。独立人工式复核可使用只读数据库检查。

## 环境定位

原 `.env` 中的模型 ID `deepseek-v4-flash` 请求返回 HTTP 503。只读查询服务的 models 接口，确认其列出 `deepseek-flash` 和 `deepseek-v4-pro`；使用 `deepseek-flash` 的最小请求返回 HTTP 200。实验配置据此切换为 `deepseek-flash`，未修改用户 `.env`，不据此推断服务商已正式废弃旧别名。

实验配置：`output/ontology_live_eval/config.yaml`、`config_financial.yaml`。温度 0、无 embedding；每轮独立工作目录。模型服务仍可能存在随机性，温度 0 不保证严格可复现。

## 已复现的问题及修复

1. **类别值被当成属性槽的子类。** 多类学校早期输出将 partially virtual 与 virtual instruction type 用 `is_a` 相连。指导改为比较兼容的类别范围；不能因代码值更具体就建立子类。新输出中 charter school 与 partially virtual school 都是 School 的子类，不声明互斥。
2. **计算规则重复 Metric 公式。** FRPM 输出同时建立百分比 Metric 与重复该公式的 Rule。Direct、Repair 和 Assessment 统一要求 Rule 具有独立的生成决策；仅将公式和请求列一起输出不形成新行为。新输出保留公式 Metric，删除重复 Rule。
3. **数值与群体混淆。** FRPM count 被写成 enrollment 数值的子集，甚至另附 `relation_hint=depends_on`。统一说明数值本身不是集合；底层群体关系必须明确区分。移除与真实谓词竞争的 hint/strength 字段。
4. **Metric 最小性仍不足。** monthly average school openings 曾被包成 Metric，又重复生成平均规则。增加可复用的 split test：剥离独立请求操作后，若已存在完整 Concept/Event 群体，不能仅凭操作加群体创建业务 Metric。真正定义业务量的内在比例、单位、时间基仍保留在 Metric。
5. **Rule 吞并下游请求。** 选最大县与返回该县学校明细曾写成一条 Rule。指导按独立触发/纠正行为划分，不机械地逐 SQL 子句切分；保留被选组的键与返回明细是不同义务。
6. **条件与动作不一致。** 某 Rule 的条件同时包含最高/最低，动作却固定 DESC。检查整个条件与动作的方向、基数一致性，不允许用一次正确 SQL 支撑过度泛化。
7. **端点语义漂移。** 节点明确定义为状态值，但关系 description 改称学校记录并赋予关闭日期。增加检查：属性槽、状态值、承载它的对象/群体不能在边上无声互换；模型可修改定义或选择更准确谓词，不强制特定建模模板。
8. **探查事实被写成永久定义。** 唯一主键只能证明记录身份，不能证明所有目录行都是同一种学校实体；当前所在地不能证明历史事件地点。指导收窄这些声明，并将临时行数、查询结果留在工作记录。
9. **只读循环没有实际写图。** Agent 在预算内重复读 guide/数据库直至停滞。提示记录已读类型，并在最后一个停滞决策仅提供编辑或显式无法完成的 handoff；不编造图，也不放宽超时/停滞上限。错误总结现在明确报告真实停止原因。
10. **工具错误路径模糊。** 嵌套 properties 和 edge 的校验反馈曾只返回 description 等短路径，导致模型误改位置。修复为完整 `upserts.0.properties.targets`、`edge_operations.0.edge.description`，保持原子拒绝，增加回归测试。
11. **运行状态误报。** SQL 正确但 Direct 未完成时曾显示 `stable_correct`。最终状态现在同时要求 Direct 完成，否则保留 resource_limit/direct_incomplete 等原因。

这些修改是共享语义约束或真实运行故障修复，没有为某个 BIRD case 硬编码节点、SQL 或自动边。

## Review 入口与边界

`output/ontology_live_eval/review/index.html` 展示各 case 最新一轮的原始问题、Gold/生成 SQL、节点匹配与计算、全部语义出边及 description、独立结论和全部历史；`summary.json` 保存相应机器可读记录。失败或未完成的新轮不会自动退回旧成功轮伪装为通过。

最终案例结果及测试数量在运行完成后写入下方。五个小样本用于发现设计和执行问题，不能代表整个 BIRD 数据集准确率，也不能证明跨 case 全局一致性。

## 可 review 的实际构图

以下是已完成独立复核的实际模型产物摘要，精确字段和完整边以 review 页面 JSON 为准。

- **BIRD 学校 60**：6 个 Concept，包含 School、school county、school website、virtual instruction type、charter school、partially virtual school。后两类都 `is_a → School`，分别绑定 Charter=1 与 Virtual=P；没有互斥关系，因此允许同一学校同时属于两者。没有为了字段或请求额外创建 Metric/Event/Rule。
- **BIRD 学校 77**：School、county、grade span served、school CDS code、enrollment (ages 5–17)、FRPM count (ages 5–17) 为 6 个 Concept。CDS code Concept 的两个匹配组对应 schools 与 frpm 字段；Metric 定义 `100.0 * FRPM_count / enrollment`，显式依赖两个存储量 Concept。不保留重复该公式的 GenerationRule。
- **BIRD financial 104**：Account、account opening date、transaction amount、transaction date 为 4 个 Concept；account transaction 为 Event，通过显式角色联系 Account 和 occurrence date/amount。区分交易发生日期与账户开立日期；没有从一次 JOIN 推断支付方向、币种或单事件记录唯一性。

开办学校和关闭学校案例的旧输出虽可通过 SQL，仍曾含计数、历史地点、规则条件或端点语义缺陷；只有最新最终接受图通过独立复核后才记为语义通过。

补充修复：Assessment 的物理上下文压缩曾丢弃表 `unique_keys` 与字段 `key_role`。真实相邻轮次因此对已有目录键的声明给出不一致反馈。现在保留这两项已有物理属性，不扩大到全目录、不引入新探索结果，回归使用与 BIRD 无关的 items/id 夹具。

代码独立性审计见 `output/ontology_live_eval/case_independence_audit.md` 和独立 reviewer JSON 中的审计记录。没有发现本轮 extraction 路径中的 case ID/业务值特判。为减少提示偏置，学校/FRPM/月均学校示例另改为订单、利润率和明确排序示例，具体答案只留在实验输入与输出。审计范围限于本轮路径，不声称对整个已有多版本工作区完成全面审计。

## 已完成的验收结果

下表逐行指明实际运行版本；这些是迭代开发样本，不是同版本、独立留出的 BIRD benchmark。更晚的新轮次另在 review 页面显示，不用旧成功代替新轮结论。

| 案例/运行 | 最终接受版本 | SQL 与内部验收 | 独立语义复核 |
| --- | --- | --- | --- |
| 多类学校 / case60_r3 | a352cd5633564eadb9177393a0aadf15 | stable_correct | 无阻断缺陷；少量措辞备注 |
| FRPM / case77_r2 | 9d1a61d80f95409ab90d37bb6006da3a | stable_correct | 无阻断缺陷；学校名应明确是 label |
| 金融交易 / casefinancial104_r2 | db81b07f75b54c9ab054c91d24fd4deb | stable_correct | 无阻断缺陷；建议去掉定义中的临时统计/请求值 |
| 学校开办 / case47_r5 | 16765d356876496184f8fbf5d3dc5a1d | stable_correct，3 次修复迭代 | 无阻断缺陷；计数对象与历史地点推断已修 |
| 学校关闭 / case49_r6 | 2d6c58cffd114d49a742e10c15d67757 | stable_correct，1 次修复迭代 | 无阻断缺陷；关闭日期关系的作用域仍可收窄 |

关闭学校最终图保留实际计算的 closed school count Metric，但删除重复 COUNT 语义的 Rule；选领先县、限制明细群体、投影去重、请求名称的 NULL 输出处理是分开的行为。状态值与状态属性槽不再使用 `is_a`。

开办学校 case47_r6 是替换试验近似提示示例、保留目录键后的额外新运行；完成前不能把 case47_r5 的成功算到它。SQL、内部 Assessment 和独立 reviewer 结论分别保存。

最终代码全量回归：838 passed、2 warnings（52.81 秒），见 `output/ontology_live_eval/pytest_full.log`；本轮改动文件 Ruff 通过。此结论仅说明回归结果，不证明模型每次抽取都能稳定达到语义要求。真实运行仍有重复读导致未完成、过度泛化和内部评估漏判的历史记录。


## 中性提示重跑与当前局限

- `case47_r6`：Flash 使用中性示例和保留目录键的上下文，最终接受 `64b1988fec5a4597973eef4a6acba3df`，SQL 与内部验收通过；独立复核仍判 **requires_semantic_revision**，因为 Metric 将计数、月均及请求范围组合到一起。旧的 case47_r5 通过不能替代这一失败。
- `case47_r7`：同通用约束的 Pro 对照，重复读取后停止，没有语义图。不能据无图得出两个模型的语义能力优劣。
- 此次对照定位到工具边界问题：只在模型请求中缩小可用工具还不够，模型仍会请求旧读取工具。执行前现按实际当轮能力集合拒绝并反馈；保持原模型调用/连续错误/停滞上限，不自动提交图。新增 stale read 被拒绝、模型纠正后成功写图的回归。
- `case47_r8` 验证该守卫；真实模型从重复读转为成功提交图，最终接受 `0350ea7e60fe489fb1e4438e996a1aca`，SQL 正确且内部 Assessment 通过、零修复；独立复核仍判 requires_semantic_revision，因为 count/12 及 County/DOC/year 请求范围被组合为长 Metric。更换模型没有消除这一漏判。

因此，此轮已获得五例分版本的满意图，也确认了当前通用提示与内部 Assessment 尚不能稳定保证最小性。不能把内部 accepted、一次满意输出或模型升级包装成已经稳定解决；独立语义复核仍有实际增益。没有为使样例通过加入针对该数据库、问题或答案的确定性修图规则。


## 最终状态

所有 21 个运行目录对应的尝试均已结束，最新评审无 pending。各案例最新轮：case60_r3、case77_r2、casefinancial104_r2、case49_r6 独立复核无阻断缺陷；case47_r8 需语义修改。case47_r5 为先前满意版本，仅作对照，不计作 case47_r8 通过。最新五图均通过确定性 schema 校验，55 条语义出边都有 description；这没有覆盖或证明 Metric 最小性。838 项回归通过，本轮文件 Ruff 通过。

后续应优先解决 Metric 适格性与请求操作分离的评估漏判，而不是针对学校 case 固定删节点。独立评审发现的具体节点缺陷是反馈证据，不能固化为按该数据库或问题触发的生产规则。

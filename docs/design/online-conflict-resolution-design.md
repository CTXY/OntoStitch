# Online：现有识别流程 review 与选择性澄清机制

更新日期：2026-11-14

状态：设计讨论稿，尚未修改生产实现。本轮保留现有三阶段 discovery、每个 slot 至少两个 candidates、SQL fragment 选项、多选与自由说明。

核心方法：现有 conflict identification → 澄清必要性判定 → 预算约束下的澄清选择 → 回答后重评估 → guidance。

“明确指定”“当前执行等效”“因预算而未问”是三种不同结论，必须分开记录。

2026-11-14 补充：question/evidence 明确约束判定、数据库情境执行等效性判定均在 scorer 之前完成；scorer 只接收筛选后的 slots。关于具体产品 Jev 的接口、训练公开程度及适用性，见 [Jev 专项核查](jev-scorer-feasibility-review-2026-11-14.md)。

**2026-11-14 实现范围更新：** 当前落地为[基础字段属性优先的 Filter Agent](clarification-filter-implementation.md)：保留明确约束判断，数据库探查优先基础字段属性，依据不足保留；来源排序及阈值继续沿用。本文第 5 节的查询情境验证和第 7 节风险排序保留作研究草案，以新实现说明为本轮实际边界。

## 1. 第 2、3 步目前已经实现什么

这里对应上一轮讨论中的“候选生成”和“差异点最小化”。它们已有完整主框架；这部分以局部修订为主，不重新设计 discovery。

| 功能 | 代码／prompt | 当前行为 | 修改结论 |
|---|---|---|---|
| 三阶段入口 | discovery/organizer.py:79 | intra → initial SQL/memory → cross → audit | 保留三个 discovery 阶段 |
| 节点内信号 | organizer.py:288；intra_node_conflicts.md:14 | conflict 节点和 multi-target concept；按本题判断相关、互补、竞争 | 保留 |
| initial SQL 与 memory | organizer.py:153；initial_sql_memory_conflicts.md:16 | 读取完整 SQL、局部比较，使用 handled_slots 增补 | 保留，放宽合理适配 |
| 跨节点 | organizer.py:205；cross_node_conflicts.md:15 | 比较同一角色的不同实现，排除互补和不同用途 | 保留，放宽合理适配 |
| 至少两个候选 | discovery/models.py:35；application/online_context.py:108 | Pydantic min_length=2 | 不改 |
| 局部替代和最小差异 | shared_slot_candidate_contract.md:5、:13、:58 | 一个角色、一项可独立回答的决定；耦合公式保持整体 | 主规则保留，增加具体正反例 |
| 同 slot 增补 | organizer.py:455 | 根据 slot ID 合并 provenance 和新 candidate | 保留 |
| 去重 | discovery/candidates.py:29、:64 | 相同 statement，或相同 SQL evidence + graph refs 的候选合并；不足两个则移除 slot | 保留确定性去重；不是语义等价判断，也不自动合并不同 ID 的同义 slots |
| 来源检查 | discovery/evidence.py:13 | 引用存在、SQL 片段逐字匹配、ID／声明不重复 | 保留，不能把它解释成语义真实性证明 |
| 相关性审核 | organizer.py:236；audit_slot_relevance.md | LLM 检查局部性、语义重复、互补等；只返回 keep/drop | 在此扩展澄清必要性判定 |
| 候选强度 | application/online_context.py:57 | applicable=1、uncertain=0.5；定义是相关性而非正确概率 | 保留作候选描述／对照，不直接当回答收益 |
| 排序 | discovery/prioritization.py:11 | 来源类别 4/2/1，再结合候选相关性之和 | 替换为后文可检验的风险收益机制 |

相对于 src/cortex/capabilities/online_context/ 的位置省略共同前缀；application/online_context.py 位于 src/cortex/application/。

Review 文件入口：

- [Organizer](../../src/cortex/capabilities/online_context/discovery/organizer.py)
- [共享约束](../../src/cortex/capabilities/online_context/llm/prompts/shared_slot_candidate_contract.md)
- [节点内 prompt](../../src/cortex/capabilities/online_context/llm/prompts/intra_node_conflicts.md)
- [SQL/memory prompt](../../src/cortex/capabilities/online_context/llm/prompts/initial_sql_memory_conflicts.md)
- [跨节点 prompt](../../src/cortex/capabilities/online_context/llm/prompts/cross_node_conflicts.md)
- [当前 audit](../../src/cortex/capabilities/online_context/llm/prompts/audit_slot_relevance.md)
- [候选去重](../../src/cortex/capabilities/online_context/discovery/candidates.py)
- [排序](../../src/cortex/capabilities/online_context/discovery/prioritization.py)

## 2. Identification 到底改什么

### 2.1 修改候选构造边界，不修改候选数量

当前 shared contract 要求 candidate 保留原始表、值、算子等，并禁止从 question 转移 literal／qualifier；intra prompt 要求行为出现在 alternatives 中；initial-memory prompt 要求 memory 提供完整实现。防止凭空编造是合理的，但措辞可能排除合理适配。

统一改为以下规则：

- 可以用 question/evidence 实例化 memory 的参数，也可以结合 schema 调整实现。
- 适配必须保留所借用知识的核心作用，不能暗中改变题目的人群、粒度、时间范围或单位。
- Schema + question 可以支持生成 predicate／aggregate；单凭某个列存在不能支持任意业务解释。
- 在 explanation 中说明“原来源提供什么，当前适配什么，依据是什么”。
- initial_sql_evidence 仍然只能引用 initial SQL 原文；由其他信息推导出的行为不能伪称原 SQL 已实现。
- 不为凑满两个候选补造选项；适配后同义则合并，剩余不足两个就不生成 slot。

三个阶段的职责边界不变。跨节点阶段仍不引入 initial SQL 引用；SQL/memory 阶段仍比较实际 initial SQL 与 memory／schema 支持的方案。

复用程度只在现有 explanation 中说明，不新增 direct/adapted/inferred 的必填结构，也不把改写多少作为质量分数。

### 2.2 最小差异规则基本不改，只提高可执行性

当前 prompt 已有同角色替代、不同决定分开、耦合计算保留、共享行为不重复。新增少量对照例：

- 同一指标的两个完整百分比公式可以比较，不能拆成任意交叉组合的分子分母。
- 同时改日期范围、返回字段和计算方式的整条 SQL 不能作为单个计算选项。
- COUNT(*) 和 COUNT(id) 可先作为待判断候选；不能仅凭不同写法就确认存在有效冲突。
- 同一个 decision 出现在多个 historical conflict points 中应合并；不按来源生成多个问题。
- 选择结果含义由 SQL fragment 定义，短解释必须忠实于 fragment，不能额外增加含义。

语义去重仍主要由 Agent 完成。现有 _deduplicate_slots 名称容易让人误以为做了跨 slot 语义去重，实际上主要清理每个 slot 内的重复候选；这一点应在评估中检查，而不是新增复杂 AST 去重器。

### 2.3 明确两个 Agent 职责之间的边界

Discovery 构造合理候选，并过滤明显不适用项。它仍可以遵守“不要凭偏好选赢家”。

后续必要性判定允许依据明确需求排除候选；这与“不要选赢家”不矛盾。当前 audit 的 “Judge ... not which Candidate is correct” 不适合直接承担这项职责，应修改。

目前 audit 把“可能改变结果”作为保留理由，但“结果不同”不能替代“有必要向用户询问”。这正是新增机制的主要落点。

## 3. 名称与两个判定通道

建议使用：

**澄清必要性判定（Clarification Necessity Assessment）**

它回答“这个 slot 还需要向用户提问吗”，包括两个顺序通道：

1. **显式约束判定**：question/evidence 是否已经足以确定争议维度？
2. **情境执行等效性判定**：剩余 options 在当前查询情境和数据库中是否等效？

先判断明确要求，再按需探查数据库。两个通道都不能解决时才进入问题价值评估。

保留原 audit 对局部性、互补、无关的基础检查，不把这些结构问题混入下面的自动选项判断。

## 4. “明确指定”的可操作标准

### 4.1 定义

“明确指定”是：在不增加新的业务假设的前提下，question/evidence 的具体文本已经约束了这个 slot 所比较的语义维度。

它可以由明确自然语言表达，不要求用户提供 SQL。允许同义归一、明确日期展开、已验证的字段绑定；不允许通过领域常识补出缺失政策。

它是语义约束判定，不是“Agent 对某个选项很有信心”。

### 4.2 四项检查同时通过，才自动确定

1. **原文锚点**：指出具体 question/evidence 片段。不能引用 initial SQL 或历史 memory 作为“用户明确说了”的依据。
2. **维度对齐**：文本确实约束该 slot 的区别。例如日期条件不能用于决定去重；返回一位最优者与是否保留所有并列不是同一句自然语言就必然确定。
3. **无额外业务假设**：由该片段到约束只需要明确语义解释与可靠 schema 绑定，不需要猜测默认阈值、业务定义或遗漏政策。
4. **排他性检查**：解释其他候选为何违反同一条约束，或其实与已确定行为等价。若另一种真正不同的解释仍不违反原文，不能称为明确指定。

不是必须唯一一个 SQL fragment：可以多个 fragment 实现同一明确含义。排除的是不一致的含义，等效实现交给下一通道合并。

### 4.3 正反例

| question/evidence | 当前 slot | 结论 |
|---|---|---|
| morning means before 13:00:00 | <13:00 或 <12:00 | 明确 13 点边界 |
| year(dob) > 1980 | 年份 >1980 或 dob>1980-01-01 | 明确排除 1980 年内生日 |
| number of distinct users | COUNT(DISTINCT user_id) 或 JOIN 行数 | 明确不同用户粒度；还需核实 user_id 的实体绑定 |
| how many users | DISTINCT 或 COUNT，在 JOIN 粒度尚未厘清时 | 不能仅凭复数 users 判为显式 DISTINCT 指令 |
| the customer who paid the most | LIMIT 1 或返回所有并列 | 单数措辞不足以可靠确定并列政策 |
| active customer | 登录过或发生过交易 | 若没有定义，不明确 |
| percentage of ... | 两种分母总体 | “percentage”本身不明确分母范围 |

如果问题和 evidence 相互冲突，不静默选择其一；不能通过“evidence 优先”的硬规则伪造明确性。

### 4.4 Agent 设计

由现有 audit 扩展出的 Agent 完成。每次自动确定只需输出简短依据：原文片段、约束、其他候选不成立的原因。不是要求长推理轨迹或形式化定理证明。

不确定时不触发该通道。人工审计重点衡量 false resolution，而不是只看删除了多少题。

## 5. 情境执行等效性判定

### 5.1 定义

固定当前 question 的已确定条件、统计范围、粒度和必要连接，将不同 options 放入同一个比较上下文。若它们对当前所需行为／输出没有区别，则本次不需要用户选择。

这是当前任务／当前数据的结论，不是所有数据库上的 SQL 等价证明，也不是长期业务政策确认。

### 5.2 Agent 的职责

仍由同一个必要性判定 Agent：

1. 说明 options 在什么条件下会不同。
2. 选择一个能够检查该区别的共同查询上下文。
3. 生成探查 SQL。
4. 根据观察判断 equivalent、different 或 unknown。

数据库工具只执行 SQL，返回完整性／截断／错误信息。SQL 由 Agent 结合本题生成，不写固定的 COUNT／日期／并列业务验证器。

优先检查差异条件，例如 NULL、重复实体、谓词差集、排名边界；必要时生成完整比较查询。

### 5.3 等效性必须覆盖真正的区别

- COUNT(*) 与 COUNT(col)：检查的是当前聚合输入中 col 是否为 NULL，而不是仅看原表样本。
- COUNT 与 DISTINCT：检查当前 JOIN／过滤后的实体重复。
- 两种日期过滤：检查完整相关范围的谓词差异。
- 并列策略：在已确定的统计指标与范围中检查边界并列。
- 两种完整查询都为空，不能在其他条件尚未核实的情况下据此合并。
- 结果比较遵守题目所需的重复、顺序、NULL、列形状与数值精度语义。
- 排名度量尚未确定时，按其中一个度量查不到并列，只能得到条件性结论，不能移除整个并列 slot。

如果三项中 A=B、C 不同，合并 A/B 的当前任务等效关系，但仍保留与 C 的选择。原 SQL fragments 及引用保留；展示代表或附等效写法，不改成含混的自然语言选项。

### 5.4 unknown 不等于 equivalent

超时、语法错误、截断、样本不足、未决依赖，都产生 unknown。不能把验证失败记成“没有差异”。

当前数据上等效的合并只对这一上下文有效。用户回答改变上下文后，相关等效关系需要重新判断。对可重复运行报表的政策选择不能仅依据当前偶然相等免问。

## 6. 当前排序为何不足

当前实现：

    R_s = sum_c a(c),   a(c) ∈ {1, 0.5}
    score_s = w_s × (1 + R_s / (R_s + 1))
    w_s ∈ {4, 2, 1}

这里 R_s 是代码中的相关性之和，后文的风险记作 risk_s，避免同符号混淆。

三个问题：

1. 来源类别在排序中占主导，但不表示用户回答更有用。
2. 增加候选数量会增加分数，即使只是等效写法。
3. 所有候选都相关，不表示系统难以判断；也不表示选错会改变本次结果。

因此，候选支持度仍然有用，但应服务于“剩余解释的不确定性”，不能直接作为 slot 的提问分数。把 1 和 0.5 归一化成概率也不成立：它们原本不是正确率。

## 7. 建议算法：基于结果风险的预算化澄清

研究机制的核心是：

**评估不询问该 slot 时，系统预计会承担多少当前任务的结果错误；用有限提问预算优先减少这些错误。**

不是把若干主观因素相乘，也不把单纯 entropy 换个名字当新方法。

### 7.1 剩余解释、等效组与损失

对未通过前两个通道消除的 slot s，保留候选 C_s。

语义去重、明确约束处理及执行等效合并全部在前置必要性判定中完成。Scorer 只接收剩余 slots 和整理好的候选／等效组 G_s，不重新决定哪些候选等效，不生成探查 SQL。未验证相等的候选不合并。

p_s(c) 表示候选作为当前意图解释的估计支持分布。组概率为：

    P_s(g) = sum_{c in g} p_s(c)

默认损失：

    L_s(a,c) = 0    若选 a 与正确解释 c 在当前任务中等效
             = 1    若两者在当前任务中导致不同所需结果

这对应“是否选错有效结果”的目标。它不假定改动更多行就必然更严重，也不试图比较不同业务单位的数值误差。

数据关系 unknown 时不能填写 L=0。可用“若二者不同”的保守上界 L=1 排序，并标注未验证；对应分数不是已测得的差异风险，实验需单独报告该子集。

### 7.2 不询问的最小预计风险

    risk_s = min_{a in C_s} sum_{c in C_s} p_s(c) × L_s(a,c)

在上面的 0/1 等效组损失下：

    risk_s = 1 - max_{g in G_s} P_s(g)

解释：

- 全部候选等效：由前置判定直接移除 slot，不调用 scorer。
- 两个不同结果组各 0.5：risk_s=0.5，不问时存在较大选错风险。
- 两个不同结果组为 0.95／0.05：risk_s=0.05，提问的预计收益较小。
- 多条 SQL 写法支持同一有效结果，应按组计算，不应被当作多重歧义奖励。

这里是模型估计下的局部决策风险，不是经过验证的真实错误概率。

### 7.3 回答收益与用户预算

在候选覆盖正确解释、用户能准确确定一个有效结果组的理想条件下，询问后这个 slot 的风险降到 0，因此：

    gain_s = risk_s

更一般地，用户可能多选、跳过或给出仍不充分的说明，此时：

    gain_s = risk_s - E_answer[risk_s after answer]

第一版将 risk_s 用作可实现的“完美回答收益代理”，不宣称已建模真实用户噪声。保留现有多选 UI；用户多选并不意味着风险归零，必须按实际答案更新候选与剩余风险。

令 B 为用户允许的最多提问数。每题成本先统一为 1，避免额外引入主观复杂度分数。

    每轮选择剩余 gain 最大的问题；
    只在 gain > τ 且尚未达到 B 时提问；
    得到回答后重新执行必要性判定并重算剩余分数。

τ 是最低收益阈值；B 是数量上限。二者不是同一个参数。若用户只指定“最多问 3 个”，就是 B=3，不要求用满预算。

如后续有真实回答时间数据，再使用 gain/cost 或总时间约束；不先假定 SQL 长度等于用户负担。

### 7.4 明确优化目标与适用范围

简化目标是最小化剩余局部决策错误的期望数量：

    min_{A, |A|≤B} sum_{s not in A} risk_s

若 slot 互相独立、风险固定、每题等成本、答案完全消解对应 slot，选择风险最大的 B 个即为该目标的最优选择。

实际系统可能有依赖，且回答会改变其他 slot。采用逐轮重评估的贪心策略，不能宣称一般情形的全局最优或固定近似比。该目标也不等同于最终整条 SQL 的 execution accuracy；后者必须作为端到端指标另外评估。

这个基础分数只计本 slot 的直接收益，尚未预估“回答一个问题会同时解决其他问题”的间接收益。回答后重算能利用已发生的影响，但不等于提前计算了全部影响。若实验显示这一限制显著，可对高价值问题增加一步假设回答评估；不把这种额外成本隐藏在基础算法中。

不枚举所有候选组合，不额外建立完整 SQL 假设空间或通用依赖图。

### 7.5 p_s(c) 怎样得到

这是机制中必须验证的一环，不能由来源权重冒充概率。

当前推荐方向是：训练一个跨数据库共享、以当前 memory 为条件的候选打分器，再在独立数据上做概率校准。具体训练与数据协议见第 11 节。重复评估保留为无需训练的 baseline：

- 语义重复已合并后，用小规模重复评估估计候选选择分布。
- 每次呈现同样的 question/evidence、SQL fragments 和支持说明，打乱候选顺序。
- 让评估 Agent 回答“如果暂时不能问用户，哪个候选最符合已有信息”，可报告候选集不足。
- 以候选选择频率估计 p；可用对称平滑避免少量一致投票直接产生 1.0。
- K=5 可作初始实验设置，不是已证明最佳值。可将多个 slots 放在同一轮评估中，减少调用开销。
- 重复引用和同源 memory 不能当成独立投票。
- 若候选集不足或无法比较，不归一化成某个候选高置信；保留技术未决或按保守策略处理。

例如有效票数 n_c，语义不同候选数 m，平滑参数 α：

    p_s(c) = (n_c + α) / (sum_j n_j + mα)

这些是模型选择频率，不是自动校准的用户意图概率。需在独立开发集上评估校准；K、α、τ 不能用最终测试集调。用单次比较评分、均匀 p、现有候选强度归一化、可训练 scorer 作为对照，而不是默认它们等价。

保留原 discovery 的 source_types/applicability/explanation 作为评估证据，不让 discovery 自己给一个“重要性分数”便结束排序。

### 7.6 为什么不只用信息熵

Entropy 衡量候选不确定性，但本任务还需要知道不确定性是否改变当前结果。

等效写法已经在 scorer 之前合并。对于剩余的互异结果解释，entropy 与 top-1 错误风险是不同的不确定性统计量，但都来自同一候选概率分布；不能把前置等效过滤再次包装成 scorer 内部贡献。

特别地，若默认采用概率最大的候选、不同候选错误损失均为 1、每题成本相同、用户回答完全消解，则 risk = 1 - max(p)。它等同于按低最大概率排序，是透明基线，不构成独立的新风险算法。只有明确引入并验证非均匀损失、实际默认策略、回答后的残余风险或间接消解收益时，才超出了该基线。进一步核查见 [Jev 专项说明](jev-scorer-feasibility-review-2026-11-14.md)。

当各组等概率、所有不同结果损失都是 1 时，风险也不会表达所有业务严重性差异。这是有意选择的简单目标，不能声称它全面理解业务价值。若要研究“错一点”和“错很多”，需要另行定义并验证任务损失；例如结果多重集差异可以作为消融，但不能任意给计算／排序 scope 加权。

### 7.7 示例（示意支持分布，不是实测概率）

| slot | 支持分布 | 本次执行关系 | risk |
|---|---|---|---|
| COUNT(*) / COUNT(non-null id) | 0.5 / 0.5 | 等效 | 0 |
| 两个不同业务口径 | 0.5 / 0.5 | 不同 | 0.5 |
| 两个不同日期解释 | 0.9 / 0.1 | 不同 | 0.1 |
| 三个写法 | 0.4 / 0.4 / 0.2 | 前两个等效，第三个不同 | 0.2 |

B=1 时询问第二项。B=3、τ=0.15 时最多询问第二、第四项；第二项回答后可能消除第四项，因此实际可以只问一项。

未问的低收益项不能标为“question 已明确”或“执行等效”。若下游需要作默认选择，应注明这是未获用户确认的当前假设；预算结束不代表所有歧义解决。

## 8. SQL fragment 保持为选项主体

不将 SQL fragments 替换成自然语言。

例如：

    A: COUNT(DISTINCT users.Id)
       解释：同一个用户在连接结果中出现多次时，只计一次。

    B: COUNT(users.Id)
       解释：对连接结果中的非空用户 ID 逐行计数。

短解释仅辅助阅读：

- 不增加 fragment 中没有的过滤、排序或粒度。
- 不把 COUNT(id) 无条件解释为“用户人数”。
- 两个 SQL 选项的别名／共同上下文明确。
- 解释与 fragment 不一致时修正解释，不以解释覆盖 SQL。
- 后续 guidance 保留选定 SQL 行为，不靠自然语言改写重新推断用户选择。

这是对上一版“把选项翻译成业务自然语言”的修正。

## 9. 精确到代码的变更范围

1. shared_slot_candidate_contract.md：修改来源与适配边界；保留最小差异定义；明确 SQL fragment + 可选短解释。
2. intra_node_conflicts.md / initial_sql_memory_conflicts.md / cross_node_conflicts.md：同步允许有依据的适配，保留分工和至少两个候选。
3. audit_slot_relevance.md 与 organizer.py 中 audit 调用：从只 keep/drop 扩展为澄清必要性判定；原局部性与互补检查保留。
4. 数据库工具：绑定当前数据源，为必要性 Agent 提供只读查询。复用 QueryDatabase 能力，不引入固定语义验证器。
5. discovery/prioritization.py：将当前 source_score 作为 baseline；增加按等效关系与支持分布计算 risk/gain 的策略，程序计算公式，不让 LLM直接生成最终优先级数字。
6. workflow/graph.py：回答后重新判定剩余 slots，并在 B 与 τ 下选择下一项；沿用恢复和预算保护。
7. output/guidance.py / engine.py：接收自动确定与本次等效结论；与真实用户回答分开。未问项仍保留未决／假设身份。

不变部分：检索输入、三阶段 discovery、min_length=2、现有多选／自由说明 UI、初始引用检查、核心安全投影规则。适配后的引用不自动成为整 alternative 裁剪授权。

对于无有效候选、候选缺失、工具不可用等技术未决，不为满足机制而编造选项或虚构等效关系。

## 10. 论文定位与评估

### 10.1 已有研究与潜在贡献

基于回答价值排序并非新思想。Rao 与 Daumé III 的 ACL 2018 工作已经将 expected value of perfect information 用于澄清问题排序：
[Learning to Ask Good Questions](https://aclanthology.org/P18-1255/)。

MISP 已研究何时、何处需要人工干预：
[Model-based Interactive Semantic Parsing](https://aclanthology.org/D19-1547/)。

AmbiSQL 已提供交互式歧义检测与多选澄清：
[AmbiSQL](https://arxiv.org/abs/2508.15276)。

因此不能将“有选择地问”“用信息收益排序”或上述 Bayes risk 公式本身宣称为新贡献。这里值得检验的组合是：

- memory 适配产生的局部 SQL 选择；
- 明确需求约束与数据库情境等效性两条免问路径；
- 在当前结果等效组上计算问题价值；
- 在用户提问预算下逐轮更新。

这只是待验证的研究方向；本轮文献核查不是完整 novelty survey。

### 10.2 基线与消融

基线：

- 全部询问与随机排序；
- 当前 source_score；
- 仅候选支持度／不确定性；
- 仅 entropy；
- 在相同门控后按 entropy 排序；
- 执行等效组风险排序。

分步消融：

- 无显式约束判定；
- 无数据库等效检查；
- 无等效组，仅按 fragment 计算不确定性；
- 均匀 p、单次评分、重复评估 p；
- 固定次序与回答后重排；
- perfect-answer simulator 与有跳过／多选／噪声的回答。

### 10.3 主要指标

- 同一问题预算 B 下的最终 SQL execution accuracy。
- 达到相同准确率所需的平均提问数。
- 准确率—提问数曲线与面积，同时报告 token、SQL 探查次数和延迟。
- 显式约束误判率、错误免问率、等效判断错误率。
- p 的校准与置信分桶表现。
- 收益分数与实际回答收益的关系；实际收益只能在离线反事实评估中测量，不能把估计 risk 当真实提升。
- 候选覆盖不足、多选和自由说明下的表现。

BIRD 单个 Gold SQL 不能完整标注用户意图分布或所有可接受实现，需要人工审计的歧义子集。Gold 不进入生产判定、概率评估或探查生成。系统增加的计算预算要在对照中报告，避免把更多模型调用的收益误归因给排序公式。

### 10.4 五项自审与 claim-evidence map

| 维度 | 结论 |
|---|---|
| 贡献 | 风险／信息价值不是新数学；组合机制的新意仍需更完整对比 |
| 清晰性 | 显式约束、执行等效、低收益未问三种状态分开 |
| 实验强度 | 尚无新机制实验，不声称降低问题数且保持准确率已经实现 |
| 评估完整性 | 需要预算曲线、门控错误、校准、用户答案噪声与总计算成本 |
| 设计合理性 | 局部独立、候选覆盖和理想回答是简化假设；不宣称全局最优 |

Claim：当前第 2、3 步已有主体框架。Evidence：第 1 节代码位置。Status：supported。

Claim：新机制减少无效提问且保持最终准确率。Evidence：尚待实验。Status：needs evidence。

Claim：等效组风险比来源相关性分数更符合本次结果错误目标。Evidence：第 7 节目标与推导，在列明假设下成立。Status：analytic justification；实际收益 needs evidence。

Claim：本方法具备充分研究创新性。Evidence：仅完成部分相关工作核查。Status：needs further literature review and experiments。

## 11. Memory 条件化的通用候选 scorer

本节保留自训练通用 scorer 的数据与监督设计。此前把 Jev 泛指为可训练 judge 的定位已纠正：TypeSafe Jev 是具体商业产品，当前不提供客户 fine-tuning／LoRA；本节方法适用于我们自己的开放 scorer，而不是官方 Jev 的已公开训练配方。具体区别见 [Jev 专项核查](jev-scorer-feasibility-review-2026-11-14.md)。

### 11.1 先确定预测对象

单个 benchmark Gold SQL 不提供“真实用户会如何选择所有解释”的完整概率分布。

首版可以监督学习的是：给定当前问题、候选、schema 与可用 memory，哪个候选／结果等效组符合标注任务。论文中称为标注任务条件下的候选选择概率；不能直接宣称已经恢复真实用户的偏好分布。

以结果等效组为主，可以避免多个不同 SQL 片段恰好实现同一正确结果时强行互相排斥。

设组 g 的共享 scorer 输出为 z_g，则：

    z_g = f_θ(question, evidence, slot, fragments_g, schema_g, memory_g, stats_g)
    P_θ(g | context) = exp(z_g / T) / sum_h exp(z_h / T)

θ 在多数据库训练样本上共享，T 是独立校准集上拟合的温度参数。若需要 candidate 层的 p，组内如何分配并不影响本方法使用的组总概率；不必额外声称辨别了等效 fragments 的唯一真值。

候选／组在计算前先做语义去重。模型输入应对选项顺序不敏感，并在训练中随机化顺序。

### 11.2 最小输入，不要求完整 graph encoder

建议首先使用已有预训练语言编码器的 cross-encoder 或轻量微调模型，文本化当前相关的 ontology 内容并输入评分头。不从头训练大模型，不首先引入 GNN。

输入包含：

- question/evidence 和本 slot 的争议维度；
- 原 SQL fragments、明确的共同上下文，以及相关 schema 绑定；
- 每个选项实际使用的 Concept／Metric／Generation Rule／alternative 内容；
- 少量可选来源统计，首版只考虑独立支持 case 数；
- 若必要性判定已查库，可附线上同样可得的观察摘要。

Memory 内容应保留定义、公式、条件、粒度、单位及绑定，不能只输入“有 20 个 case 支持”。模型需要判断这些支持是否适用于本题。

不要把 Gold SQL、Gold-assisted 标注理由、由 Gold 模拟的最终用户确认或下游已知正确性放进 scorer 输入。原始无 Gold 的 discovery explanation 可以输入，线上与训练保持一致。

不要求输入原始历史 query examples；它们可作为独立可选消融。关闭 examples 便于研究 ontology 知识本身的效果，但不能代替数据来源隔离。

### 11.3 Memory 强度怎样使用

以“实际支持当前候选所借用定义的独立 source cases”计数：

    n_g = number of distinct source case IDs supporting the relevant definitions
    strength_g = log(1 + n_g)

当前 graph 的 node.sources 和 alternative.sources 可以提供来源。冲突节点的 owner sources 是来源 union，不应把对手 alternative 的来源也算成当前候选支持。多个引用来自同一个 case 只计一次。

这只是来源覆盖特征，不是正确率。20 个月级案例不能投票推翻本题明确的日级要求；重复模板、同源派生和数据集规模也会影响计数。训练过程允许模型降低不适用强支持的权重，不强制“支持越多，概率越高”。

当前 QueryExample.support_count 是“某个历史 case 被多少相关资产引用”，不是“某个资产有多少 cases 支持”，不能直接复用成该特征。

首版只做“memory 内容”与“memory 内容 + 支持数”的对照。避免先堆积大量质量、覆盖和置信特征。

### 11.4 训练样本如何得到

一条样本对应一个真实 online slot：

    输入：q、evidence、slot、候选 SQL fragments、schema、当时可用 memory 和统计
    标签：符合标注任务的结果组，或一组可接受候选

生成过程：

1. 先按后文协议准备不包含当前样本及其派生信息的 memory。
2. 用固定的初始 SQL 生成、检索、三阶段 discovery 和必要性判定产生真实 slots。生成输入中不出现当前 Gold。
3. 冻结这些输入后，才让离线标注器读取当前样本的 Gold SQL，判断它在该 slot 的局部选择。
4. 可以通过局部 Gold 语义匹配获得弱标签，再利用同一上下文下的查询执行核查。Gold 只用于标签，不用于把错误候选偷偷修成正确候选。
5. 多个未决 slots 导致完整查询错误时，不能把一次完整执行失败分摊为所有局部候选错误；不能隔离该决定的样本留作未标注或人工核查。
6. 自然候选作为主训练分布。若有合成负例，单独记录来源与比例，不编造支持 case 数，不让“人工构造”标记成为标签捷径。

若多个选项属于相同正确结果组，就监督这个组。若存在多个可接受标签 Y，可用：

    loss = -log(sum_{g in Y} P_θ(g | context))

它只监督可接受集合，不提供各可接受解释的真实用户频率。不能仅凭该目标声称学习了偏好概率。

若所有候选都不符合 Gold，不能强行把最高分候选标为正确。保留这类 candidate-coverage failure 统计，必要时单独训练内部 coverage／none-of-the-above 判定；这不要求向用户新增伪候选。候选闭集 softmax 风险始终要说明覆盖假设。

Gold 也可能只有一种标注实现；语义等效、数据偶然相等和真正不同口径要在小规模人工审计集中区分。

### 11.5 使用同一训练池是否合理

同一训练池同时用于构建 memory 和学习 scorer，本身不构成测试泄漏：两个模块都可以使用合法的训练数据。

但若样本 i 的 Gold 已被提取进 M，再用 (q_i, M) 训练 scorer，它是在带有自身答案衍生知识的输入上学习。它可能学到“认出已有答案”，而非估计新问题的候选风险。这属于训练内的自包含捷径与分布偏差，不应和“最终测试集被使用”混为一谈。

删除原 query example 不能保证排除它：概念绑定、公式、规则条件、冲突合并结果、统计及模型生成的说明都可能保留该样本的信息。

同样，删除一个 source_id 或将支持数减 1，不等于撤销它对合并图内容的贡献。可靠协议从源 case 划分开始生成 memory。

### 11.6 两种数据协议

**先验证可行性的简单协议：**

    现有合法训练池 T → M（memory） + J（judge 训练） + V（校准）
    memory = Build(M)
    train scorer on slots from J using this memory
    calibrate on V using the same frozen memory
    evaluate on untouched test questions

三个集合按原始 case／派生样本族划分，不按生成的 candidate 行随机拆分。

优点是只需构建一次 memory。代价是没有充分复用训练池；所有对照都要使用相同 memory 规模，避免混淆。

**充分复用训练池的推荐研究协议：**

    T → F（模型拟合池） + V（独立校准池）
    F → F1, F2, F3

    对每个 k：
        M_k = Build(F minus Fk)
        仅用 M_k 为 Fk 生成 slots 和输入
        用 Fk 的 Gold 生成监督标签

    用所有折的评分样本训练共享 θ
    M_final = Build(F)
    用 V 在 M_final 下产生的样本校准 T 和提问阈值
    冻结 θ、校准和 M_final，再评估最终 test

三折是成本折中，可按资源评估。每个源 case 可以为别人的 memory 提供知识，也可以成为 scorer 监督样本，但它生成自己评分输入时不出现在自己的 memory 中。

校准池 V 在这个协议中不进入 M_final。若改为用整个 T 重建 memory，必须重新说明校准数据来源和适用性，不能沿用包含自信息的校准成绩冒充独立验证。

图构建成本主要在离线。可以复用冻结的 source-local case bundles，再按折重新合并；不能先合并全图再仅删除当前 provenance 冒充折外 memory。

### 11.7 跨数据库与跨数据集的通用模型

训练一个 θ，线上读取目标数据库的 schema 与 memory，不对每个数据库再微调 θ，是合理的目标。

需要区分两个评估设置：

- **已见数据库的新问题**：scorer 训练见过数据库的其他问题；测试 query 与训练 query 分开。
- **未见数据库迁移**：目标数据库的 query 标签不参与 θ 的拟合，也不参与温度／阈值选择；只在测试时作为上下文提供该库允许的 memory。

后者允许目标库的历史样本用于构建 memory，属于“有目标库历史知识的 scorer 迁移”，不能宣称整个系统没有使用目标数据库监督数据。

数据库划分应先于候选生成和模型选择。可留一数据库／一数据集作为 scorer 测试域，其他数据库训练，另留数据库校准。使用目标库的支持数作为输入可以，使用其最终测试表现调参数不可以。

BIRD 与 mini-clean-BIRD 等可能共享原始问题或派生内容，不能只按数据集名称认为它们独立。应按原始 source lineage 和近重复问题分组，防止跨数据集泄漏。相同问题的所有候选行与派生标签保持在同一划分中。

通用 scorer 的泛化是要验证的结论。相关预印本已观察到 Text-to-SQL verifier 的跨 schema 退化：
[What Predicts Correctness in Text-to-SQL?](https://arxiv.org/abs/2607.06799)。
不能因为混合多个数据集训练就默认解决。

### 11.8 校准与风险公式的连接

只会排序还不够：相同排序下，概率 0.55 和 0.95 会导致不同的免问行为。

在独立校准集拟合温度或其他简单校准器，报告 NLL、Brier、置信分桶与预算—准确率曲线。温度缩放是标准基线：
[On Calibration of Modern Neural Networks](https://proceedings.mlr.press/v70/guo17a.html)。
Text-to-SQL 中也已有利用结构信号和校准器的工作：
[Calibrating LLMs for Text-to-SQL Parsing by Leveraging Sub-clause Frequencies](https://aclanthology.org/2025.emnlp-main.859/)。

得到 P_θ(g | context) 后，沿用第 7 节：

    risk_s = 1 - max_g P_θ(g | context)

但它是在候选覆盖、当前等效关系和标注目标下的局部风险估计。不能把它当作无条件 SQL 真实错误率；多选、不充分回答、候选覆盖不足和完整 SQL 中其他错误仍需另外处理。

### 11.9 最小实验矩阵

至少比较：

1. 无 memory 的 scorer；
2. memory 内容，不含 query examples；
3. memory 内容 + 独立支持数；
4. 可选加入历史 examples；
5. 自包含 memory 与折外 memory 的训练差异；
6. 同数据库新问题与未见数据库；
7. 训练后是否校准；
8. 与前述无需训练的重复评估 baseline 比较总计算成本。

当前结果包 BIRD 的 train_ids/test_ids manifest 为 769/765，注明上游 dev 在包内命名为 test。这 765 条及其 Gold-assisted online 决策不能因为目录名不同就转作 scorer 训练数据再沿用原测试成绩。多数据集汇集前同样应检查底层 source lineage。

研究主张：共享 scorer 是否能利用 ontology 内容与来源统计得到可迁移、可校准的风险估计。Status：设计假设，需要上述隔离与实验；不预先声称训练有效或 memory 支持数一定有益。

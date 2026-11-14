# Jev 是否适合用于剩余澄清问题的风险估计

核查日期：2026-11-14

状态：文献、官方文档与公开代码阅读；没有调用收费推理 API，没有训练模型，也没有得到本项目上的准确率／校准实验结果。

## 1. 先固定系统边界

本次确认的流程：

    Conflict identification
        → Question/evidence 明确约束判定
        → 当前查询情境下的数据库执行等效性判定
        → 剩余 slots
        → Jev 或自训练 scorer
        → 预计回答收益与预算选择

前两个判定在 scorer 之前完成。Scorer 不负责重新判断是否已被 question 明确回答，不生成验证 SQL，不负责决定 options 是否等效。

Question/evidence 仍然可以作为 scorer 输入，用来比较剩余解释的支持程度；这不等于让 scorer 再承担前置过滤职责。

部分等效 options 的合并也是前置处理。Scorer 只读取整理后的候选／等效组。验证 unknown 时保留其状态，不能让概率分数偷偷把 unknown 变成 equivalent。

## 2. 关键纠正：Jev 是具体产品，且确实返回概率

前一版把 Jev 泛指为可训练 judge，这是定位错误。TypeSafe 的 Jev 是具体的商业 System One 模型，不能把一般 scorer 的训练建议称为“Jev 的训练方法”。

官方接口有三个与本任务相关的 primitives：

| Primitive | 返回 | 对本任务的意义 |
|---|---|---|
| Choice | top-1 choice、完整 probabilities、confidence | 比较同一个局部决策的替代解释 |
| Noul | yes 概率，0–1 | 判断一个明确命题，例如默认选项是否符合局部要求 |
| Score | rubric 数值、各等级概率、confidence | 评判有序等级；不适合把等级均值直接当候选正确概率 |

因此，“decision model 只能选 top-1，不能输出概率”不符合 Jev 的接口事实。top-1 只是概率分布上的离散读数。

来源：

- [官方 Choice](https://docs.typesafe.ai/primitives/choice)
- [官方 Noul](https://docs.typesafe.ai/primitives/noul)
- [官方 Score](https://docs.typesafe.ai/primitives/score)
- [官方 Python SDK 响应类型](https://github.com/typesafe-ai/typesafe-sdk-python/blob/f36e68549ceca3191e5791e5f99ea1baa1a258b5/src/typesafe_sdk/_core/response_types.py)

## 3. probabilities、confidence 与正确率不能混用

Choice 的 probabilities 在给定选项上归一化。confidence 是从这个分布算出的统计量，不是另一个独立的正确性判别器。

官方 adapter 中的 Choice confidence：

    confidence = (max_i p_i - 1/K) / (1 - 1/K)

例如：

- 两个选项，top probability=0.75，confidence=0.5。
- 十个选项，top probability=0.55，confidence=0.5。

如果把 1-confidence 当错误风险，两题都得到 0.5；如果概率在任务上成立，则对应的 top-1 错误风险分别是 0.25 和 0.45。

来源：[官方 confidence 说明](https://docs.typesafe.ai/confidence)、[官方 adapter 的计算代码](https://github.com/typesafe-ai/system-one-adapter-python/blob/e1d4cc938204b22fc5a3c3aca7044072fe3f712d/src/system_one_adapter/_utils/confidence_metrics.py)。

因此，本方法需要读取 probabilities，而不是只读取 choice，也不是将 confidence 直接代入风险公式。

接口有概率，并不证明该概率已经在 SQL + ontology memory 的分布上校准。TypeSafe 自己也说明校准是预测群体的性质，不保证每条回答正确。

## 4. 官方 Jev 的训练：已公开什么、没公开什么

官方发布文与 AI primer 将训练方法称为 Reinforcement Learning for Calibrated Decisions（RLCD），目标是受约束的判断与可靠概率，而不是生成文本。

公开材料描述：

- 接收 state 和用户定义的 typed questions；
- 输出决策及概率；
- 并行处理多个判断；
- 将概率与实际结果的一致性作为目标。

但本轮阅读的官方材料和公开仓库中，没有找到足以复现官方 Jev 的完整训练配方：具体 reward／loss、数据构造和混合比例、优化过程、架构及系统性消融未完整公开。

不能据此断言官方使用了某个社区推测的 Plackett–Luce loss、某个 Brier 配方、特定 head rank 或某个公开 backbone。

更直接的产品限制是：官方 Models 文档明确表示，不用客户数据为 Jev 做 fine-tuning 或 LoRA；所有账户使用相同模型权重。领域适配通过 state、instructions、criteria 和外围逻辑完成。

官方 GitHub 的 SDK 是 API 客户端；system-one-adapter 是调用其他 LLM 的对照适配器，均不是 Jev 的训练实现。

来源：

- [发布文：Introducing System One Models & Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- [官方 AI primer](https://docs.typesafe.ai/introduction/machine-learning-primer)
- [官方 Models：Customizing Jev](https://docs.typesafe.ai/models#customizing-jev)
- [官方 GitHub](https://github.com/typesafe-ai)

结论：可以使用 Jev；不能按公开配方在我们的数据上直接微调“官方 Jev”。训练自己的模型应称为 Jev-style／typed-decision scorer，并明确所用开放架构与目标。

## 5. 开源训练路线实际在做什么

### 5.1 路线 A：只读 logits，不训练

把候选映射到 A/B/C 等输出位置，从已有语言模型读出这些位置的 logits 并归一化。它能提供完整分布，top-1 是 argmax。

这只是从既有模型获取决策分布的方法，不自动获得针对本任务的概率训练或校准。是否省去文本生成，与是否具有可靠概率是两个问题。

### 5.2 路线 B：开放 backbone + 决策头 + 监督训练

Cloudflare Clef 是公开模型路线。其 model card 描述：

- backbone 读取 state；
- joint schema head 为每个允许选项产生 logit；
- 每个 question 内 softmax 得到分布。

发布文说明冻结 backbone 主权重，同时训练路由 head 和 rank-256 LoRA；监督部分使用 label-smoothed cross-entropy 与 Brier loss，也描述了一个额外 RLCD 目标。Cloudflare 的 RLCD 描述不能被当作 TypeSafe 官方 RLCD 的实现。

Unsloth 已提供将普通 LLM 转为 typed-decision 模型的训练文档和代码。检查其 decision.py 后可见：

- DecisionTrainer 接收 state/questions/gold；
- _decision_loss 支持 soft-target cross-entropy；
- 可配置 label smoothing、Brier 项、ordinal 项；
- 决策 head 与 LoRA 可以训练；
- 有 held-out calibration 流程。

不是所有配置都默认启用全部 loss 项，应以训练参数和代码为准。对我们这种无序候选选择，不应启用“相邻选项给部分分”的 ordinal loss。

来源：

- [Clef model card](https://huggingface.co/Cloudflare/clef)
- [Cloudflare 训练介绍](https://blog.cloudflare.com/clef-decision-models/)
- [Unsloth 训练文档](https://unsloth.ai/docs/basics/train-your-own-decision-model-with-unsloth)
- [Unsloth 训练代码，核查版本](https://github.com/unslothai/unsloth/blob/fdf806fbe25326cd48dab37ddd6f48194eb7893c/unsloth/models/decision.py#L165)

另一份独立实现 typed-decisions 的 decision_loss 明确计算 CE + weight × Brier，再单独拟合温度：
[实际 loss 代码](https://github.com/mithril-lang/typed-decisions/blob/e65215db7e554a31f557936f0bd1d8a0a78b6af5/src/typed_decisions/encoder.py#L159)。
这证明“可训练决策模型”不必只学 top-1；可以直接监督整个分布。但其通用 benchmark 成绩不能外推到本项目。

### 5.3 路线 C：使用 proper score 的推理／RL 概率学习

独立项目 openjev-rlcd 研究了对 rationale 之后的答案分布进行评分。当前仓库对应的论文版本称为 PRDO，早期 arXiv 版本称为 OpenJev-RLCD；仓库明确说明不声称知道 TypeSafe 内部训练算法。

其核心是对完整答案分布使用 Brier 或 log score，区分：

- 直接优化每条 rationale 后的分布；
- 优化多条 rationale 的混合分布；
- 在后续阶段是否给 rationale 使用 REINFORCE。

当前 README 和代码包含多种实验模式，不能将其中一种泛称为官方 RLCD。它提供可研究的概率优化方法，但引入 rationale 采样与 RL 后，已经比我们需要的局部候选 scorer 复杂。

来源：

- [当前论文 v2](https://arxiv.org/abs/2609.38850v2)
- [研究代码及版本说明](https://github.com/ZimmyGao/openjev-rlcd/tree/5859d2b893314fb3581f06e4e36c303e372b3f48)
- [分布目标和梯度实现](https://github.com/ZimmyGao/openjev-rlcd/blob/5859d2b893314fb3581f06e4e36c303e372b3f48/openjev_rlcd/estimators.py)

## 6. 为什么“学会选对”不足以保证概率可信

同样 top-1 的两个输出可能是：

    [0.51, 0.49]
    [0.99, 0.01]

准确率一样，免问政策却完全不同。

如果只对采样动作“答对奖励 1，答错奖励 0”，条件真实标签分布为 q 时，期望奖励是：

    E[R] = sum_i p_i q_i

它对 p 是线性的，在 q 最大的选项上集中全部概率即可达到最优；不要求 p=q。

监督交叉熵：

    CE(p,y) = -log p_y

以及 Brier：

    Brier(p,y) = sum_i (p_i - 1[i=y])²

都是对概率预测有明确含义的目标；在总体分布及适当假设下，其期望以真实条件分布为最优。有限样本、模型失配和领域迁移仍会影响校准。

所以，“训练 decision model”与“学概率”并不矛盾。关键在训练目标与评估，而不在是否生成自然语言，也不在模型被称为 judge、reward model 还是 decision model。

本项目的推荐训练基线应是 CE，以及 CE + Brier 的消融，再做独立校准。先不因 RLCD 这个名称而增加 PPO／GRPO。

## 7. Jev 在我们的场景中能承担哪一段

### 7.1 接口匹配

前置筛选后，一个 slot 已具有：

- 明确的局部决策；
- 少量候选 SQL fragments；
- 对应的相关 ontology 定义；
- 已确定的共同查询情境；
- 可选的支持 case 数。

这接近 state + Choice 的接口。可以要求 Jev 判断“在这些候选中，哪个最符合当前局部需求”，读取全部概率。

它不需要生成 SQL，不需要自己探查数据库，也不需要处理整个在线流程。因此在接口与职责层面可应用。

### 7.2 能力匹配仍需验证

官方 jaggedness 列出数值精度、日期比较、间接引用、无关长上下文、选项顺序等弱点。我们的 SQL 可能恰好涉及这些情况。

因此应把片段、共同上下文和对应完整定义明确给出，避免让 Jev 从整张大图逐层追索；保持 SQL fragment 为选项主体，只附不改变语义的简短解释。不能用自然语言描述替代精确 SQL。

前置探查已处理实际数值／日期关系时，只提供可用观察，不要求 Jev重新计算。需要长链 SQL 语义推导的 slots 单独评估，不能假定一趟判断模型能替代强推理模型。

来源：[官方 Model jaggedness](https://docs.typesafe.ai/model-jaggedness)。

官方近期博客也强调 Jev 适合原子判断，外围代码负责组合决定：
[Don’t Call It a Decision Model](https://typesafe.ai/blog/dont-call-it-a-decision-model)。

### 7.3 Choice 和 Noul 对应不同概率

Choice：

    P(在候选闭集中，哪个解释最符合任务 | state)

适合相互替代的意图解释。这里概率和为 1，但它不会自动知道正确解释是否落在集合外。

Noul：

    P(给定候选确实满足当前局部要求 | state)

适合候选可接受性，多候选可以同时得到高分。不应把多个独立 Noul 数值直接归一化，当成互斥意图分布；例如两个候选都可接受时，0.95/0.95 不能被改成“各有一半错误风险”。

必要时可以给 Choice 配合一个候选覆盖检查，但它不替代前置的明确需求／数据库等效性处理。先作为实验对照，不默认增加大量判断项。

来源：[官方 Companion Noul](https://docs.typesafe.ai/patterns/companion-noul)。

## 8. 如何接回“用户回答降低多少风险”

### 8.1 明确无用户时的实际默认动作

若系统不询问就采用 Jev 的 argmax 候选，则在概率经任务验证、候选覆盖且结果组互斥的假设下：

    risk_s = 1 - max_i calibrated_probability_i

若下游实际采用 initial SQL 或另一策略，必须计算：

    risk_s = 1 - P(实际默认方案正确 | state)

不能让 Jev 为方案 A 打分，却将风险解释为下游方案 B 的错误率。也不能将局部风险称为整条 SQL 的错误率。

### 8.2 Jev 不直接计算回答的反事实收益

它提供我们定义的判断问题上的概率，不自动给出“用户回答之后，最终 SQL 会改善多少”。

在理想用户能正确消解该 slot 的简化假设下，直接收益可用当前局部风险代理。存在回答噪声、候选外答案、多选或其他 SQL 错误时，需要另估回答后的剩余风险。

因此角色是：

    Jev／开放 scorer → 概率证据
    校准层 → 目标任务上的概率修正
    明确的损失与默认策略 → 风险
    回答模型与预算算法 → 澄清价值／提问次序

### 8.3 真正未说明的用户意图可能没有可恢复真值

若两个业务口径都合理，问题没有说明，memory 也无法确定当前用户偏好，那么 Jev 的高概率只可能反映其知识与统计先验，不能恢复未表达的私人意图。

此时可以研究数据分布下的最佳默认方案，但论文中必须将它与“用户明确指定”区分。前置筛选已将明确可解的题移除，这会让 scorer 面临更难的样本，校准与训练应在这个剩余分布上评估。

## 9. 对本项目的推荐路线

### A. 使用冻结的官方 Jev 做最直接的基线

固定模型版本，使用 state + Choice，读全部 probabilities；在折外 memory 的独立校准样本上检验／校准，测试预算—准确率曲线。

Memory 在 state 中给出；支持 case 数在程序中计算。先看 Jev 是否读懂局部 SQL、是否有用地排序错误，再决定是否训练自己的模型。

这是使用商业产品，不是微调官方 Jev。

### B. 训练自己的 typed-decision scorer

沿用上一版折外 memory 的训练数据协议，采用开放决策头与 LoRA／encoder scorer。主要对照 CE 与 CE+Brier，独立校准后用于风险排序。

训练数据明确为 state + questions + labels：state 包含当前 memory 内容与可选支持数，questions 的 options 保留 SQL fragments，标签来自合法训练样本的局部标注／执行核查。

它可跨数据库共享参数，但不能因采用 Jev-style 接口就声称获得了官方的泛化或校准。

### C. 冻结 Jev + 轻量下游模型

若原始 Jev 能排序，但无法稳定利用 memory 强度，可以把其概率、极少量有意义的判断和支持统计输入一个小型可训练模型。

TypeSafe 官方提供了“Jev 产生特征，CatBoost 学习标签”的 cookbook。它训练的是下游模型，不是 Jev 权重：
[Autoresearch feature discovery](https://docs.typesafe.ai/cookbooks/autoresearch_feature_discovery)。

本项目不需要首先复制自动特征搜索。先比较原始 Choice、校准 Choice、Choice + 一个支持数特征。若只训练 top-1 正确性校准器，得到的是 top-1 正确概率，不应把它伪装成所有候选的完整新分布。

## 10. 最小可证伪实验

只使用已经通过前置必要性判定的 slots，固定候选与 memory：

1. 强推理 LLM baseline；
2. 冻结 Jev Choice；
3. Jev + 独立概率校准；
4. 开放 scorer 的 CE／CE+Brier；
5. memory 内容与 memory 内容+支持数的对照。

评估四件不同的事：

- 候选／局部决定的 top-1 正确性；
- 错误排序能力，如 AUROC／AURC；
- 概率校准，如 NLL／Brier／置信分桶；
- 交互效用，如相同预算下最终 SQL 正确率、所需题数和总开销。

额外检查选项顺序、候选等效去重、无正确候选、未见数据库与不同 memory 规模。

不能仅报告 top-1 高就声称概率可用，也不能仅报告 ECE 低就声称值得提问的问题被排到了前面。温度缩放通常不改变单题 argmax，且不能补充模型缺少的语义判断能力。

## 11. 核查结论与证据边界

| 问题 | 结论 |
|---|---|
| Jev 只能返回 top-1？ | 否；Choice 明确返回完整概率 |
| 是否能读取 ontology memory？ | 可以作为 state；如何理解和利用需任务验证 |
| 是否能直接训练官方 Jev？ | 当前官方不提供客户 fine-tuning／LoRA |
| 官方 RLCD 是否有可复现公开配方？ | 本轮官方资料与仓库未找到完整配方 |
| 能否训练 Jev-style 模型？ | 有公开训练代码与权重路线 |
| 是否必须使用 RL？ | 否；监督概率目标与独立校准应作为第一基线 |
| Jev 输出能否直接叫“用户意图真实概率”？ | 不能；依赖判断问题、候选覆盖、任务分布和验证 |
| 能否用于本项目？ | 接口适合做剩余 slot 的概率判断器；实际 SQL 能力和校准效果尚未实测 |

本次阅读使用官网、官方 SDK／adapter、Clef model card／博客、Unsloth 文档和 loss 代码、独立 typed-decisions 及 openjev-rlcd 实现。未把第三方对 Jev 内部架构的猜测当事实，也未将开源复刻的训练法归因给 TypeSafe。

## 12. 核心简化：当前 risk 何时只是 Jev 不确定性

这是本轮讨论必须承认的结论：

    明确约束和执行等效在前置处理完成；
    剩余选项是当前任务下需要区分的解释；
    默认选项是 argmax p；
    错误损失统一为 1；
    用户回答可完全消解；
    每个问题成本相同。

在这些条件下：

    gain = risk = 1 - max(p)

因此，风险排序就是按候选最大概率从低到高排序。固定选项数 K 时，它与 Jev Choice confidence 的反向排序相同；不同 K 时：

    risk = (1 - 1/K) × (1 - confidence)

不能简单将 1-confidence 当跨候选数的错误概率。也不能将这一单调变换称为新的澄清收益算法。

用户指出“如果模型直接输出 uncertainty，风险机制是否仍有必要”在这个简化设置下是成立的：基础风险公式的作用是赋予分数明确的决策含义，而不是增加一层独立模型能力。

要保留一个超出 uncertainty thresholding 的研究机制，需要具体定义并评估其中至少一项：

- 不同错误的实际损失 L(a,y)；
- 不询问时系统真正执行的方案 a0；
- 不完全用户回答下的剩余风险；
- 一个回答对其他 slots 的间接影响；
- 可观测的非等成本。

这些不是必须全部加入的模块，也不应为了公式复杂而加入。推荐先将“前置过滤 + 校准后的最大概率 + B/τ”作为诚实、可复现的主基线，再决定额外机制是否有必要。

## 13. 进一步核查的开放训练与读出代码

### 13.1 AnyJev：distribution 与 argmax 在代码中分开

核查 commit：35311c58955716cb8955ce1368a665988bc63735。

- readout.py 将每个 Choice 选项映射为单 token 标签。
- backends/hf.py 从答案位置读取 logits/log-probabilities。
- result.py 的 distribution 返回全部候选概率；argmax 单独取最大项。
- 其 confidence 实现是 max(probs)，与官方 Jev 的归一化 confidence 不同，不能因字段同名直接交换。

来源：[AnyJev result.py](https://github.com/nokia-applied-research/AnyJev/blob/35311c58955716cb8955ce1368a665988bc63735/anyjev/result.py)、
[readout.py](https://github.com/nokia-applied-research/AnyJev/blob/35311c58955716cb8955ce1368a665988bc63735/anyjev/readout.py)。

### 13.2 Releasing the Inner Jev：硬／软标签都可以训练分布

论文第 4 节描述：从答案字母 logits 读取 softmax，使用 LoRA 与决策头，以目标分布交叉熵训练；没有 soft target 时使用正确答案标签，并另外拟合温度。它不是只监督不可微的 argmax。

其训练池同时包含硬标签、人工投票的软分布与 teacher 分布。不能把 teacher 概率直接当作人类真实意图频率。

来源：[论文第 4 节](https://arxiv.org/html/2610.17205v1#S4)。

### 13.3 Jevify：实际 loss 接受 one-hot 和 soft target

核查 commit：a1308666b5460291772161260f43ed95afa65815。

engine/heads.py 为 Choice 先构造 one-hot target；开启 soft_labels 且数据存在软标签时再替换。其 loss 是 target 与 log_softmax 的交叉熵。Noul 使用独立二元目标；Score 使用序数损失，三者含义不能混用。

来源：[Jevify heads.py](https://github.com/uspraveen/Jevify/blob/a1308666b5460291772161260f43ed95afa65815/jevify/engine/heads.py#L112)。

### 13.4 OpenJev-RLCD：区分概率 readout 与推理策略

核查 commit：5859d2b893314fb3581f06e4e36c303e372b3f48。

train.py 从选项 logits 的 softmax 得到完整 u；train_direct.py 的监督基线优化正确标签的负对数概率；estimators.py 的 single_surrogate 对每条 rationale 的完整分布使用 Brier 型 proper score，并将推理策略的梯度与 readout 梯度区别处理。

它包含推理采样和 RL 的研究设置，不能据此假设官方 Jev 必然同样实现。也不能认为训练概率必须先做 RL。

来源：[train_direct.py](https://github.com/ZimmyGao/openjev-rlcd/blob/5859d2b893314fb3581f06e4e36c303e372b3f48/openjev_rlcd/train_direct.py)、
[estimators.py](https://github.com/ZimmyGao/openjev-rlcd/blob/5859d2b893314fb3581f06e4e36c303e372b3f48/openjev_rlcd/estimators.py)。

### 13.5 排行榜能证明什么

JevBench 明确评估 typed decision accuracy、probability calibration、latency 和 cost；用户提到的榜单也可能是独立的 Decision Index。它们说明完整概率是这类接口的核心组成，而不是只对 top-1 排名。

不同榜单的任务、覆盖和聚合指标不同；通用榜单排名不证明 ontology-conditioned SQL slots 上的概率可靠，也不能用于替代我们的校准集。

来源：[JevBench 官方仓库](https://github.com/fstandhartinger/jevbench)、
[Decision Index 复现协议](https://github.com/parthspatel/jev-decision-index)。

# Relation Merger 与 Reconciliation 当前实施方案

日期：2026-11-21。当前契约：编辑后自动编译、显式提交、独立 Assessment 直接 JSON。最新专项验证186项通过；真实小测及全部保留失败见当前 HTML。第8节为旧版历史记录。

[当前HTML实施与验证报告](../../output/reconciliation_edit_compile_20261121/index.html) · [当前完整中文Prompts](../../output/reconciliation_edit_compile_20261121/prompts.html) · [可复现小测入口](../../scripts/run_relation_reconciliation_v2_smoke.py)

## 1. 流程和职责

节点身份及内容整合 → 确定性 ID/引用同步 → Relation Merger（每批最多10组）→ 八类 Reconciliation → 发布严格校验。

Relation Merger 是普通关系内容归一化，**没有独立 Assessment，没有关系级 conflict**。同一有向端点和谓词的描述通常不同，应重新生成完整描述，不把旧模型文本当作必须逐字保留的 gold。代码维护来源、映射、scope及消费者。

Reconciliation 处理真实局部结构调整、概念补全和泛化，允许创建／修改 Concept、Metric、GenerationRule、Event；保留独立 Assessment。禁止借重写、复制、退休或改名选择／消除已存在 conflict。可按固定完整分支内容修复相关字段、引用、metadata和关系，不得改变分支数量、ID及竞争含义。

仅修改模型生成关系不要求保留错误旧文本；需要保留的是有据业务含义、条件、角色、消费者及来源。物理目录只读。完整读取不授予无关写权限。

## 2. Relation Merger

### 分组与批量

按已确认映射后的 from/to/edge_type 分组。不同谓词分别保留。一组最终一条规范关系；不输出clusters、relations冲突或独立评估。不可因合成一条边而扩大版本适用范围。

第一版只合并除 description 外其他属性经规范化完全一致的边；不同 scope/正式条件属于同一端点谓词桶中的独立子组，避免用文本替代正式scope。该限制须向用户透明记录，不能偷偷丢条件。纯同义description和互补描述均走语义整合；完全相同可机械处理。模型输出仅 group_id/description，不允许改端点、谓词、sources或其他属性。

输入：组的完整旧关系、端点完整公开业务内容（按ID去重）、相关完整版本、谓词契约、受影响metadata、evidence IDs。来源正文仅在确需时按ID读取。

工具：read_evidence(ids)，submit_relations(results)。每批≤10组并受完整请求预算约束；组和必要metadata耦合不可静默截断。逐组结果由Controller绑定输入hash，已完成组恢复时不重跑。

输出：每组恰好一次，description非空。Controller稳定选择survivor，来源并集，保存原边文本和精确映射，原子应用并检查引用／metadata。组坍缩导致知识无法保留时不静默删组，也不发布损坏图；进入具体耦合修复义务。

Prompt草稿：

> 你是关系内容整合 Writer。每组具有相同端点、谓词和适用范围，请生成一条完整简洁的description。以最终节点含义、关系契约和必要证据为准，纠正错误旧表述，保留有据角色和条件；不把旧模型文本当作gold，不制造关系conflict，不选择节点alternative。不得新增唯一性、传递性、默认偏好等未给定断言。直接submit_relations，覆盖每组一次；不重发节点或图，不填写Controller维护字段。

## 3. 八类 Signal：观察、原因、修改、验收

| Signal | 为什么检查／正常状态 | 可执行修改 | 不能据此执行 |
| --- | --- | --- | --- |
| abstract_predicate | 抽象实例边没有说明具体角色；具体谓词的类型继承正常 | 修具体谓词、方向、真实主体；补有据缺失角色节点与相关说明 | 凭存储表推业务主体、随便改名掩盖未知 |
| binding_contest | 多Concept直接owner竞争；targets重合正常 | 同身份漏合并移交Identity；不同身份重组表示关系；补真实属性Concept；保留匹配知识 | 删targets、投票选owner、造共同owner、按分支豁免全局政策 |
| parallel_assertions | 关系整合后仍有具体角色重叠／错误诊断；正常多谓词不触发修复 | 修确有依据的谓词/端点/角色表达，必要时补业务节点 | description不同就报错；把不同角色强合；裁决conflict |
| triangle | 直接边与路径可能重复或推导错误；合法三角形正常 | 按已知推导和消费者修错误关系／表示，有据时补中间对象 | 默认传递、用路径代替Metric直接输入、为消三角造节点 |
| same_role_neighbors | 可能丢角色、顺序或配对；独立多值正常 | 澄清角色；补必要对象；Concept中保留有据组合metadata | 强建父节点、猜互斥／顺序、给Event写Concept metadata |
| related_decisions | Rule共享对象可能粒度或对象错误；独立决策共享目标正常 | 修applies_to及节点相关引用／说明；补明确缺失对象或Metric | 强并Rule、重选计算口径、把作用对象等同自动触发 |
| source_selection_variants | 来源选择差异可能丢匹配／表示角色；code/name多匹配正常 | 修/补targets、expressions、角色及消费者；补有据属性节点 | 未投影即删知识、外层匹配拼联合键、默认COUNT可互换 |
| value_concepts | 正向泛化/补全机会，非默认错误 | 复用或补列业务维度Concept；连接语义取值；有据对象子集泛化；保留条件 | 每列机械造节点、取值当对象子类、猜值域完整、制造grounding竞争 |

每项任务提供具体 observed、required_outcome、授权对象和必要局部闭包。完整说明沿用 reconciliation-signals-review.md 各类原因与示例；实际Prompt仅加载当前Signal短指导，避免把长设计文档塞给模型。

Metric直接输入、Concept metadata 和目标版本影响**不是新增三类常规Signal**，而是节点整合／关系变更事务中的机械检查与具体义务。已知ID同步和确定性关系由代码维护；只有真实语义影响才以obligations进入当前任务。node_relation_followup是义务载体，不授予无限权限。禁止先接受损坏节点结果再指望后续修复。

## 4. 完整局部输入、工具和会话

Writer与Assessment使用完整公开业务对象，不提供字段选择器。普通节点完整content；conflict节点完整解析alternatives，不暴露overrides。物理节点只给完整业务定位与必要属性，不塞样本库及内部来源字典。

Assessment首次即有真实before/candidate差量、必要完整端点、全部被检查关系集合、metadata全组成员、相关契约、来源映射、待复核问题。Controller计算closure与coverage，不让模型花调用补缺公式。未变正文只给一次，标content_change=unchanged；关系变化单独显示，不用same_as跳转。

核心锚点允许关系读取；边界节点可完整读，但不自动递归扩展其邻域。Metric直接输入、metadata成员、grounding owners由Controller扩齐；全局owner唯一、计算环及缺边反例在权威图校验，不能由未展示推断不存在。首次bundle_ready失败在模型调用前报告，不伪造完整性。

只读工具：read_nodes(ids)、read_relations(node_ids)、read_evidence(ids)。读取对象全部业务内容与必要前后比较；没有fields/view/draft_version/alternative_ids/direction/cursor/write_contract参数。Controller自动绑定快照、必要内部分页及权限，历史同ID以不歧义handle区分。契约和必要写入说明随任务自动提供，不再通过读取取得写权限。

Writer 使用 edit_draft、submit_draft。合法编辑保存后自动编译；未完成草稿返回缺失决定，编译失败返回结构化诊断，成功返回 ready_to_submit。相同内容不增加版本，绑定仍有效时不重复编译。预览状态为 compiled_unsubmitted；只有 submit_draft 才移交独立 Assessment。编译绑定 before、schema、实际草稿、完整 evidence/source 与契约版本。

Assessment 没有 Review 或其他结果工具，直接返回包含 verdict、signal_reviews、prior_issue_reviews、issues 的 JSON。Controller 在请求中提供公开输出 schema，绑定实际版本与候选，验证覆盖、引用和依据。格式错误在 Assessment 内修复；语义缺陷才反馈 Writer。

每轮依据真实授权材料选择读取工具：完整节点和精确快照下双向完整的核心关系不再开放重复读取；未读历史 handle 保留节点读取，原始 evidence 仍可按需读取。分发前拒绝未公开工具，不扩大边界邻域。工具可用性策略参与 policy 指纹。

不重放 AI/Tool 历史，仍维持固定六块上下文；固定任务与完整初始包为缓存前缀，补读按 handle 去重。恢复成功时旧编译错误归档，不与成功回执并存；独立读取、协议和待复核语义反馈仍保留。

ReviewSession由Controller绑定task/before/candidate/obligations/policy hash。候选改变旧Review失效。移除模型的版本选择不等于移除精确绑定。已提交Writer恢复只续Assessment；仅预编译完成的恢复仍回到Writer等待提交。原记录只读保存，策略不匹配需显式迁移。

## 5. 输出权限、延期和结构门禁

Reconciliation可修改有据普通节点和创建有据业务节点。对conflict节点允许相关业务说明、引用与metadata的保义更新；禁止增删分支、改分支ID或通过改变竞争差异消除冲突；禁止退休conflict后新建普通副本绕过。完整candidate检查不只检查操作名称。

候选安全与义务完成分开。合法现状/可选优化→keep；合法但依赖节点决策→非阻塞deferred并保存依赖fingerprint；确定结构不合法→blocked。延期不放进立即drain队列，相关依赖变动才重开；缺少前提不冒充依赖待决。

内部未发布修复事务允许显式已知baseline诊断逐步修复，但不得新增问题；最终发布仍严格全图校验。只有具备稳定诊断与反例比较才允许此路径，否则Controller将必要组件合为事务，不能用字符串忽略全局错误。

本轮采用后一种保守实现：保留严格全图门禁；Controller聚合必须共同关闭的具体诊断，不实现不完整的baseline错误字符串差分放行。

精确rename/identity alias与semantic successor/source locator分开。仅前者机械同步消费者；关系组坍缩不得直接删说明。新增谓词需完整契约，物理目录只读。

### 退休关系与来源

retire 明确区分 preserve 和 retract。preserve 默认保义，after 必填且非空；边只允许一条旧边到一条真实不同的语义后继边，代码迁移全部旧边来源，多条同义边用 edge_merge。节点退休仍走完整 source-unit 路由；混合节点/边退休因来源路由不完整而拒绝。后继不能是只读物理目录边。

retract 仅用于断言本身无据/错误且无保留后继的边，要求显式 after=[] 和非空结构化 retraction_basis。依据绑定公开可见的 immutable before 对象与具体字段；编译检查存在、权限与字段，独立 Assessment 判断语义理由。撤销来源留在审计中，不附到无关对象。不能把合并后旧边过时冒充原断言无据；其他节点仍有相同 evidence 也不能代替关系来源迁移。

## 6. 验证与成本控制

不运行全库，不切换后台runtime。先保存源码快照，原case graphs、旧checkpoint、旧失败记录只读保留。

从既有case-level graphs构建：①同端点关系不同描述（包含互补角色）整合；②不同谓词/不同scope保留；③关系组metadata影响；④abstract具体谓词修复；⑤value_concepts补维度；⑥合法conflict保留；⑦固定分支Metric输入；⑧target版本能力边界。优先4–6个局部真实测试，其余offline与恶意越界回归。

离线走生产工具、编译、评估和 checkpoint，不能当作真实模型通过。当前真实 Relation Merger 选择 financial/116 与 formula_1/880 两个不同原始图的自然组，不复制凑数；批上限10不要求满批。Reconciliation 独立 Assessment 直接 JSON；已通过记录保留，只重测未通过样本。

真实请求read timeout初始300秒，允许至600秒；SDK隐藏重试关闭。超时/连接/可重试限流可切换不同匿名key slot，保存完整错误链，不能当语义错误。每任务轮次、总模型调用、完整请求长度显式计数；初始总预算48次，若接近上限先报告实际未完成状态，不能假接受。可在有意义进展和明确错误下有界调整，不沿用旧轮24次累计额度。

HTML持续记录每阶段、每组完成情况、模型请求角色/工具、完整输入量、错误、独立Review状态、checkpoint位置、源hash和匿名key slot，不写密钥。最终列清真实通过、未完成、未测与已修复代码缺陷。

## 7. 独立 Review 结论与实施约束

三个review覆盖关系批量语义、上下文/工具、编译/状态机，方向通过；以下发现列为必须实现项：

1. 模型批次≤10组与图事务是不同单位。跨批metadata组件先收集所有结果再原子应用；节点整合中的耦合暂停也调用同一服务。
2. RelationMerger记录pending结果与completed应用，绑定输入/节点/策略hash；应用失败不能把模型结果标completed或重跑已完成生成。
3. 8类Signal与机械obligations分开，保留义务稳定ID和完整覆盖。仅有义务的任务也能运行；不是删除旧名称后跳过它们。
4. public读取无fields/view/version，内部保留精确读证明、历史handle、只读边界和ReviewSession；写合法性说明仅随Writer包提供，不再读工具授予权限。
5. 最终candidate保护conflict owner、全部原分支ID、冲突点及来源下界；允许指定现有分支的相关局部更新，拒绝退休conflict转普通及可识别的分支克隆替代。语义上的绕过由独立Assessment判断，不能声称代码完全证明语义。
6. decision/review可记录deferred，合法deferred独立持久化，不混入accept时清空的issues；结构错误不能defer通过。
7. 旧字段选择/混合request_context测试不作为兼容要求。用完整首包、范围、恢复、权限及越界反例测试替换，旧记录保留。

## 8. 旧版初次实施与真实验证（历史记录）

- RelationMerger实际接入节点整合后的关系阶段；节点耦合事务中同一服务提供局部preview，最终关系／metadata由同一事务严格编译后接受。模型批次最多10组，无独立Assessment。
- Reconciliation公开只读工具为read_nodes/read_relations/read_evidence，Writer另有edit_draft/submit_draft，Assessment仅另有Review。8类Signal与obligations分开；允许有据新建／修改普通节点和局部分支更新，不允许消除已有conflict。
- 初始完整包及无实例谓词契约已接入；修复了恢复闭包二次扩展导致的非幂等，以及旧issue延期后丢失的状态漏洞。旧issue现在必须明确解决或继续阻塞。
- 普通关系描述整合、跨scope保护、metadata坍缩耦合修复、已完成结果零重跑、scope/evidence/policy绑定、冲突替代绕过等均有回归。
- 删除可执行v1 reconciliation小测与自动迁移helper；原脚本快照和所有旧运行记录保留。可复用数据构造提取为reconciliation_probe_fixtures.py，不重新开放旧public工具。
- 最终17模块149 tests passed，Ruff F通过。不是全仓库测试，也不是全库merge。
- 真实RelationMerger一次调用完成10组（一个真实Metric上下文+9个明确标注的受控副本），没有Assessment，completed恢复零调用；人工查看输出保留了十一月分子首项、十二月分子减项及分母三种作用。
- 抽象谓词、Metric依赖、value_concepts维度新建、合法conflict保留，四例真实Reconciliation均有独立接受Review并提交，Assessment各1次调用。总计19次真实调用，最大完整请求42,526字符。
- value样本曾请求尚在草稿中的新节点邻接，被范围门禁拒绝后恢复；没有未闭合的协议或输入超限错误。该样本原有节点全部未改，新维度创建成功。
- 本轮实际HTTP read timeout 300秒，SDK重试0；没有遇到超时，不宣称实际测试了所有key或发生了轮换。换key／脱敏路径另有离线故障注入测试。
- 最初一次真实启动前因环境加载顺序导致模型名缺失，0次API；已修正并保留记录。原graphs／checkpoint和后台runtime未切换。

## 9. 当前验证入口

当前代码的自动编译、重复编辑、格式错误、来源、权限和跨进程恢复由19模块186项专项测试覆盖，Ruff F通过。完整真实尝试、上游错误、保留失败、单例修复及最新通过状态，见[当前HTML](../../output/reconciliation_edit_compile_20261121/index.html)。首轮保留的旧工具协议与复制测试不再是当前执行契约。

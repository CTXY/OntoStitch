# Merged graph 的 Online 验证

## 检索范围

使用一个已冻结的 GraphState，不向图写入 test 知识。先做已有的召回和相关性判断，再取所有已选节点的一跳显式关系；业务谓词不需要加入遍历白名单。新增邻居不能成为下一跳起点。所有已召回表示均被排除的节点不因共享枢纽被重新引入。

公式、完整 targets、未解决 alternatives、分支关系分组及物理归属引用须完整保留。这是引用完整性补全，不是额外业务遍历；它可以覆盖一个相关性排除，以免截断计算定义。JOIN 路由保留原有独立机制，不代表业务关系可传递。

## 投影与上下文

公开检索接口允许任务子图省略不完整的普通 relation_metadata 分组，其他节点内容、边内容与来源必须精确相同。分支组不能被任意删减。用户明确缩小 Concept 匹配时，根据保留 targets 过滤原有 grounded_on，包括没有旧 target_id 标签的边；不据 refs 生成新边，也不修改全局图。

分析 packet 保留节点含义、绑定、公式、规则、边描述、分支和实际使用的关系契约。省略空展示字段、来源 bookkeeping、未使用契约和已由 table_ref/column_ref 表达的自动物理归属边；内部 GraphState 不变，业务字段说明仍保留。

## 判断边界

Rule 相关性须结合 condition 和业务作用域，不能只看 operation 标签。仅有通用操作相似而具体条件不适用时，不导出为推荐实现。澄清过滤不能把业务词直接等同于某张表、字段或行类型。SQL 输出仅含明确要求的字段，筛选和排序表达式不自动加入 SELECT。

## 评测协议

固定 ID 划分、模型、版本、图指纹和完整分母；保存全部失败、恢复和成本。Gold SQL 只供系统提出问题后的模拟用户及 SQL 执行评估器使用，不进入召回、相关性选择或 SQL 生成。此结果属于交互模拟 EA，不等同于无人交互 EA。

对照使用同一 SQL 生成器、完整 schema 和相同执行比较器，仅移除 guidance。执行错误或结果不匹配保留，不按正确性重采样、不拼接不同轮次的成功预测。运输失败可恢复，并记录未知 usage。稳定 run ID 允许复用同版本已完成的检索/交互步骤。

若根据 test 运行定位并修改通用行为，报告该事实；后续完整运行保证可复现，但不声称它是未用于开发的盲测。

## Filter 与 guidance 的职责边界

Filter 只输出问题保留决策与审计证据，不输出代表 option、guidance 或 MUST USE，
不负责 not_relevant。Organizer 负责提出有效问题；Filter 只有 keep、question_resolved、
database_equivalent 三种结果。keep 的问题原样交给用户/Simulator。显式消歧须引用原问题
或 evidence，并解释其他选项违反了什么；不能从业务词猜测物理绑定。
若文字中的值与数据库值证据在大小写、编码或单位等方面冲突，引用原文不足以消歧；
保留问题交给用户/Simulator，不能由 Filter 自行纠正或选择另一个值。

等价性基于当前数据库整体数据性质，而不是某个业务过滤子集上的偶然相等：
若 P(D) 足以保证两个候选操作在相同输入结构中互换不改变结果，则可以过滤问题。
完整数据上的唯一性、非 NULL、连接覆盖等必须实际检查；同时考虑 NULL、零分母、
重复次数和连接造成的粒度变化。近似数值相等不等于严格等价（用户显式容差除外）。
样本、截断、查询失败和不充分证据均保留问题。结论只针对当前数据库快照。
两个 JOIN 等价不代表必须 JOIN；Filter 不将观察转为任务指令。

## GenerationRule 的任务适用性

在用户/Simulator 回答完成后，先安全投影任务图，再调用已有 Guidance Agent：
同一调用中保留逐 Slot 的精确确认行为，并批量判断候选 Rule，不增加逐 Rule 调用。
候选来自本次召回相关 Rule 及明确确认引用；不重新扫描全图。未能安全投影的冲突
分支不作为同时成立的普通规则导出。

Rule packet 包括 ID、名称、condition/action/reason、operation intents、相关对象定义
与必要绑定。判断条件是否适用、行动是否服务当前任务、是否违背已确认选择、是否
依赖未选择分支或额外强加输出/过滤/JOIN/聚合。标签相同不足以证明适用。
没有澄清问题的 Rule 也可以适用。每个候选记录 include、reason、instruction；
排除只是本次不导出，不删除或否定全局知识。

SQL handoff 分为确认行为、适用规则、业务定义。Filter 证据仅保存在审计记录，
不成为 SQL 指令。确认行为由代码保留原文；Rule 指令保留其适用范围与限制。
完整 ontology_assets 继续保存。Checkpoint 身份包含新输出契约与 prompt 版本，
禁止将旧 Filter 代表选择当作新的用户确认。

## 真实验证

先固定源码快照，对 case31/66 从零执行：检查 Filter 输出无代表选择、无 MUST USE、
未决问题交给 Simulator，近似比例不能判严格等价，JOIN 等价不要求增加 JOIN。
同时用可控正反例验证 Rule 在没有澄清问题时仍可按条件采用，非当前任务的输出
布局不得扩展 SELECT。通过后采用同一快照、同一图与模型完整运行 44 个 test cases。
保留整个分母和全部错误，不按 EA 重试；只允许记录在案的技术失败恢复。
与历史结果比较时单独列出预检和完整运行成本，不将模拟用户调用排除出总成本。


## Guidance Rule覆盖完整性的上下文纠错

模型的输出可能符合静态JSON schema，却遗漏某个输入Rule。之前这种错误在run_llm_extraction返回后才校验，直接终止流程。Guidance现在将输入相关的Rule ID覆盖（缺失、未知、重复）、适用Rule instruction、Slot覆盖与确认行为格式校验作为output_validator传入异步解析器，复用已有的一次输出纠错。纠错保留原system/user输入与模型首次结果，反馈具体ID/错误，不重复注入schema，不补造适用性判定，也不删除未返回Rule。纠错仍失败则保留报错，不能生成部分guidance。

financial的case95通过原checkpoint保存的retrieval、三条points和三条已确认answers重放finalize；原checkpoint/workflow identity不修改，恢复使用的新prompt/runtime单独记录。其他52条SQL预测沿用缓存；只对此前无预测的case生成一次baseline SQL。该机制不依赖case内容，不根据EA进行重采样。

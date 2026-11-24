# 条件性节点写入契约：按需加载，不塞入每次关系检查

本页为完整设计参考。运行时拟议 write_contract 只返回当前 node_type、operation_kind 与受影响字段需要的部分；不是把本页全部注入所有初始 System。完整JSON schema与语义限制必须同时可读，不能让模型仅靠编译失败猜格式。

## 通用前提

更新按字段完整before值与依赖读取；完整 node_rewrite 按同一已有身份完整读取。物理对象只读，不用节点重构偷偷完成未确认的Identity合并。完整content五字段为 name、description、aliases、tags、properties；name非空，description可null，aliases/tags字符串列表。可选属性不用就省略，不用default:null猜允许null。来源以evidence IDs或固定before的真实source_routes表达，不写SourceReference/overrides/hash。

## Concept

完整定义与每套可能匹配保留，targets非空二维列表：外层每项完整可能匹配、内层联合匹配。引用准确物理ID，组内不重复；组端点与target_expressions共同判重。同端点不同条件不是重复。重排后重建规范0基索引，非空表达式不越界。名称/代码可表示业务对象，不自动成为独立名称属性，也不证明现实唯一性。

新增Concept沿当前项目写入政策使用Column/DimensionValue，不编造Table targets来通过绑定；已接受历史表示若与阶段规则不同，不借关系修复重新否定。定义/匹配新值受实际schema和catalog验证。relation_metadata仍在properties内，每项{id,edges,description}，至少两条真实相邻、同谓词同方向、scope可共同成立边；新建/去重/退役边后必须检查组语义。

## Metric

properties.expression必填且仅含expr/refs；expr非空计算表达式，不是完整SELECT；${key}与refs键集合相等，refs绑定真实已授权节点。裸列、纯CAST和+0/-0/*1//1恒等包装不能冒充计算。保留已有计数对象、总体、单位/粒度、NULL/零值、先后运算；不能按名称猜SUM/AVG或合并未知操作数。

sql_generation_plans可选，每项仅id/application/steps；id匹配[A-Za-z][A-Za-z0-9_]{0,99}且唯一，application非空去空白后<=500字符，steps为1–8条非空文本，每条去空白后<=600字符。说明与公式/依赖一致。公式变化同时修精确direct依赖，不能只删边迎合旧公式而丢真实已接受口径。

## GenerationRule

properties.condition/action/reason均为非空文本；reason字段不能写成rationale。triggers必填，仅question.phrases、sql.columns/operations，列表元素非空且至少一项有效信号；columns是实际允许列ID。operation_intents可选复数字符串列表，标签不能代替具体行为。

同一决策可能多步骤，不按SQL子句或intent数拆分。请求参数只在来源支持角色和取值范围时泛化，绑定写进condition/action；不新增未声明parameters。保留粒度、计数对象、并列/NULL策略与原有操作顺序。Rule作用关系不得暗示无条件触发或层级继承。

## Event

properties.definition必填非空，表述业务发生类型与角色关联，不能只用日期列/动词/JOIN制造事件。targets可选；给出时仍为完整可能匹配组，target_expressions是有效完整发生识别谓词，不是时间戳或ID等标量。保持参与者、时间/地点、对象在同一发生中的配对含义。

现有关系shared对“新建Event至少时间/地点”的准入比底层definition必填schema更强，方案保留为**新建/实质重构需证据支持的角色完整性规则**，不把它反向应用于未变已接受Event。若准备改此政策需单独版本化，不能在Prompt中一会要求、一会省略。

## 同一已有节点普通/冲突转换

node_rewrite是单一公开操作，输出普通完整content或>=2完整alternatives，不能同时给；冲突需conflict_points、实际分支支持和真实routes。normal/common单元不discard；已有不同分支合成普通内容时可以全部route=result，合成内容必须保留每项知识。新普通内容不能通过conflict_resolve选择旧分支再node_update串联假装实现。

重写以后同步检查起点/终点分支、内容消费者和Concept metadata。关系本身仍需条件化或限定时，不能因节点普通化就将旧branch边无条件化。正文相同但关系不同也不能只按正文去重；若现有表示不能表达应deferred，不为过存储判重编造差异。

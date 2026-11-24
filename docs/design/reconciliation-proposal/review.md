# 设计审查记录与待确认边界

本方案由三个sub-agents分别审查Signal guidance、Ontology/alternatives、上下文/版本恢复，主Agent统一公开schema、草稿协议、完整Prompt和示例。所有工作只写docs/output，不修改src、原checkpoint或后台进程，不调用API。

## 已根据审查补齐

| 审查发现 | 方案中的处理 |
| --- | --- |
| 直接owner含糊且被当通用公理 | 改为Concept内部“直接物理绑定”，解释现存全局单Concept政策、targets重合与未来放宽影响 |
| 匹配组与冲突分支易混淆 | 明确外层每项完整可能匹配、内层联合；相容方法普通化，真正竞争才alternatives |
| metadata被独立呈现 | 只放Concept.properties，选中边涉及的组级知识初始就保留 |
| 只选择旧alt不能合成新普通结果 | 统一单owner node_rewrite，新增trustedadapter实现项，禁止绕用Identity/新ID |
| 新工具无处获取真实source units | read_nodes(source_units)与write_contract都可按需返回固定before manifest |
| 旧问题被省略就被当解决 | Review逐旧问题prior_issue_reviews，明确closed/pending绑定 |
| 损坏edit被当成submit恢复 | target_tool+参数schema明确该次调用；坏edit不执行，不跳过未保存修改 |
| 版本变化可能清空无进展 | 按实际候选/决策+真实可见facts+policy判断，不用version/空格重置 |
| 缓存承诺夸大 | 只保证System/task前缀和稳定工具；working view更新会使后续prefix失效；未测provider命中 |
| 归档来源ID已在before退役 | 增加只读source快照命名空间，read_nodes和评估basis必须精确source_snapshot_id，不赋予活图写权限 |
| normal重写无法挂新证据 | 普通node_rewrite允许evidence_ids，冲突仍分branch/common，真实旧unit不能省略 |
| 整节点退役绕过单元保留 | 节点after不能为空，node_unit_routes逐真实unit到实际后继，分支弃用不走此捷径 |
| 六条消息可能掩盖正文增长 | 显式预算/分任务，不默默分页或删当前草稿和未解决basis |

## 不在本轮暗自改变的政策/能力

- Concept单物理ID最多一个直接Concept绑定是现存项目政策；本方案暂保留，不把互斥分支当自动豁免。
- 起点alternative_ids不能表示终点版本。若现有关系表示无法保义，任务deferred；新增target-version字段必须另审schema/检索/编译，不让模型即兴造字段。
- 同正文、不同边的分支不能只按正文去重；存储模型若不能表达需要单列能力改动，不能编造内容差别。
- 新node_rewrite/完整node_create、canonical editor、受控工具及新Review字段均未在生产实现；不能将本方案JSON直接交给当前旧工具。

## 方案校验边界

页面验证区给出可执行schema/正反例与布局检查。示例含真实Metric失败记录的原始关系/退役理由，但六条新协议请求为重建示意，明确不是实际新模型调用。Ontology文档中的NodeContent/GraphEdge例已用现有validator校验，两张图用GraphManager校验；它们不证明全部新流程实现完毕。

三位sub-agents已完成独立复核。发现的normal重写新证据、节点退役来源绕过、归档source可达、Signal/issue状态一致、claim越权、差异点假分支、write_contract缺锚点和教学图悬空端点均已在方案/离线schema中修正。公开schema成本按最终实际文本计量，未把六条消息等同固定体积。剩余表示能力/政策取舍与未实施门禁明确保留，不被“通过schema”覆盖。

## 本轮精简的追加独立复阅

两位sub-agents复核了十二份guidance、两个角色完整System及旧接口删除边界。发现并补回必须注入的语义：properties按键更新但给定键值整体替换；read_nodes四个保留选择器的含义；targets同端点不同条件不能直接去重；冲突普通化不能机械去分支范围广播关系。另明确edge_merge只吸收边/来源，需整合说明时配survivor的edge_update。Writer的评估输出与Assessment核查已由构造器分离。旧接口仍服务生产调用者，故本轮只移除拟议Prompt黑名单和冗余父总览，不删除活动生产代码。

## 用户样式统一复核

按本轮 abstract_predicate 的两个角色示例，统一共用语义、角色、工具/输出及全部十二类 Signal 的格式。构造器支持新的角色标题并保留角色隔离，模块间加入分隔线。其余十一类 Signal 的原业务语句均在重排时逐条核对保留；本轮校验聚焦生成文本、共享前缀、工具 schema 与 HTML，不重新调用模型或迁移生产 checkpoint。

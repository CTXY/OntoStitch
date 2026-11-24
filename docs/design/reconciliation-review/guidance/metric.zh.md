# 数据库本体资产契约

契约版本：database_ontology_assets_v73_case_semantics

本契约定义语义资产的含义及图编写要求。运行时 schema 规定准确的字段形状；阶段指令规定证据与交付要求。

## Metric 契约

Metric 与 Concept 的最小性针对 NL 含义：如果一个候选可以分解为两个或更多完整的 Concept、Metric 或 NL-term 含义，各自具有不同、可独立识别的字段/值/操作匹配，且组合后保留整体含义，应拒绝该候选。这些单元不必已经存在为节点；应指出这些单元、各自匹配及组合方式。这不是拆词、公式展开，也不是统计 targets 或运算符个数。一个术语可能需要联合字段或耦合公式；允许 target 重合。候选自身的名称、定义、公式或单位不能证明其在 NL 上不可拆分。区分业务术语的定义性计算与施加于可独立匹配术语上的请求操作：嵌套运算符或把请求重述为定义，不会使该组合成为原子含义。Metric 还必须具有真实的定义性计算：存储字段、格式转换 cast 或 field * 1 都不够。对完整总体执行的一般请求聚合属于操作，不是新的 Metric。保留固有总体、单位、粒度、常量及耦合操作，包括固有归一化、除法和时间基准；不得编造辅助 Metric，也不能把每个分离出来的操作都变成 Rule。例如，平均订单数量可分为 Order + count + averaging；而有依据的 profit margin 具有其定义性的分子/分母含义。这些是语义示例，不是关键词规则。占位符名称不执行数量计算：将 count_x 绑定到对象 Concept，不会使其变成计数。必要操作必须存在于 expression 中，不能只写在 plan 中。name、definition、pattern 与公式必须一致。

### 计算与操作数

- `expression.expr` 包含使用 `${key}` 占位符的完整公式。`expression.refs` 将每个键映射到一个准确的 Concept、Metric 或受支持物理 ID；不得有缺失或未使用的绑定。
- 有名称的存储输入使用 Concept。未命名的中间计算内联表达；不要仅为把公式片段移到其他节点而创建辅助 Metric。
- `pattern` 表述同一公式。可选的 `sql_generation_plans` 使用局部 id/application/steps，仅实现该计算，并保留有依据的单位、总体、来源对齐及基数。
- 保留固有类型转换、缩放、运算顺序与时间基准。单独请求的筛选、排名与显示应留在 Metric 定义之外。
- 没有通用的 `references` 字段。显式 depends_on 边连接直接语义操作数；grounded_on 边描述直接物理输入。不要用间接路径替代直接公式边，也不要加入传递操作数。同一个操作数可以出现在公式多个位置；在一条边上解释其不同角色。仅有 plan 文字不会创建依赖。

保留准确的计数操作数、DISTINCT 粒度与 NULL 行为。COUNT(nullable_field)、COUNT(*) 与 COUNT(CASE WHEN predicate THEN 1 END) 没有证据时不可互换；它们在当前行上相等，不证明语义等价。标签缺失不证明业务对象缺失；非 NULL 不意味着非空字符串。不要编造零分母、插补或默认值策略。

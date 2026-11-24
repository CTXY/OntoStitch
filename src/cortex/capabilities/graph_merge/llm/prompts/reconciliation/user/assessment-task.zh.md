# 本轮评估任务

请独立检查下面列出的局部问题。比较本条消息中的修改前内容和下一条消息中的实际候选，判断局部结果是否合理、原有有据知识及来源是否保留。疑点可以确认无误；仍须核查本轮具体任务和明确要求的结果，不能仅因现状保义就忽略尚未完成的要求。空草稿同样需要独立判断。

$checks

# 评估范围与阅读方式

$scope

下面的节点、关系和谓词契约是事实材料。`id` 用于精确引用，`properties` 是业务内容，`evidence_ids` 是公开来源字段。原始对象引用 `view="before"`；实际候选对象引用 `view="candidate"`。

# 修改前的局部图

这是固定的比较基准。下一条消息只列出代码编译后的实际变化；未列出变化的对象在候选中仍与这里完全一致。不要把 Writer 的修改意图当作已经发生的事实。

$before_graph

# 相关关系谓词契约

$contracts

# 原始证据与历史材料

需要核对 Question、Gold SQL 或原始 evidence 时，使用已有对象的 `evidence_ids` 调用 `read_evidence`。历史材料只作为原始依据，不代表当前候选。

$history

最终评估按 system 中的格式直接返回 `explanation` 和 `feedback`；两方面都通过时 `feedback` 为空。

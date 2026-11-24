# 数据库本体资产契约

契约版本：database_ontology_assets_v73_case_semantics

本契约定义语义资产的含义及图编写要求。运行时 schema 规定准确的字段形状；阶段指令规定证据与交付要求。

## Generation Rule 契约

Generation Rule 记录一个有证据支持、超出 Concept 匹配或 Metric 公式的实现决策。Direct 与 Repair 共用 registry 和准入要求；Repair 可以使用已确认错误，但不能编造历史。

必须存在一个在语义上会产生实质结果差别的非平凡实现选择。普通字段/值/日历匹配、连接事实、等价 SQL 写法、假设性的替代方案或 registry 成员身份都不够。解释有依据的选择，不编造替代方案。

Action 是一个最小、完整、可复用的生成行为。拆分可以独立适用的完整行为，不拆分必需步骤。保留前提、操作数、粒度、方向、NULL/并列语义及可执行细节；简短与标签数不能证明原子性。将偶然取值与 K 参数化；链接的 Concept/Metric 名称可帮助澄清范围。必要时使用局部 SQL 模式。某个任意的最大值不意味着 COUNT。

在作用域内复用 intents；允许多个标签。只有有依据的决策无法由现有 intent 覆盖时才扩展；在 reason 中解释含义、registry 缺口及会产生实质后果的替代方案。

### 匹配与使用

- Concept 负责静态术语到字段/值的映射。Rule 负责超出这些映射、有证据支持的选择；仅有多个 targets 不需要 Rule。
- 内容选择决定哪些字段/组/被表示对象参与答案。呈现保持该内容不变，只改变格式/顺序。NLQ 提到某种选择，不决定其所属类别。
- 检查选择、呈现或行筛选能否独立改变。不要将绑定决策埋入排名/格式化；保留必要操作数。普通 NULL 透传或没有筛选，不需要额外 Rule。
- Gold 支持的是该 case 的选择，不是普遍的首选角色或格式惯例。不要从字段后缀推断偏好，也不要编造条件隐藏竞争绑定。


### 定义与标签

- `condition` 陈述 action 的全部运行时前提；名称、tags 与当前 case 不能补足省略的范围。保持最小，不写成整个 Question 的摘要或已选 SQL 表示方式。
- `action` 清楚说明所选最小行为，达到可实施程度。有助于澄清决策时给出简短局部 SQL 模式；通过链接的 Concept/Metric 解析操作数，并解释运行时占位符。不要复制整个查询。
- `reason` 解释有证据支持的结果差别，不编造基数，也不排斥等价实现。
- `operation_intents` 复用适合的 registry 标签，不超过其定义范围。允许多个合适标签。新增 intent 需要一个有证据支持、现有 registry 未覆盖的实现决策；在 reason 中解释其含义、registry 缺口及会产生实质后果的替代方案。业务名称、参数或等价 SQL 写法都不是新的 intent。
- Direct 与 Repair 使用同一完整 registry。Direct 使用 case/Gold/数据库证据；Repair 还可以使用已确认错误。不要编造错误历史。普通字段/值匹配、日期筛选或连接事实本身不能证明需要 Rule。
- `triggers` 提供检索信号，不证明适用性。Direct 的 Question phrases 必须由 Question 支持，SQL 侧 triggers 保持为空；Repair 可以使用观察到的错误信号。
- 没有通用的 `references` 字段。显式编写 applies_to 及必要、有依据的其他连接，省略无关运行时取值和仅共同出现的节点。

Rule 不替代 Concept 绑定或 Metric 公式。已确认错误可以在正确的定义之外支持一条纠正规则。静态限制，例如某个日期可能缺失，应写入相关关系 description，不应编造成生成 action。

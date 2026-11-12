# v57：有限证据下的分类与业务关系（讨论提案）

日期：2026-11-05。

后续实施方案已收敛：见 [v58 复杂度审查与最小版本](ontology-relation-metadata-simplified-v58.md)。本文件的任意关系类型继承仅为原始提案，第一期改为仅支持 has_attribute 的一层特化。

## 已完成的窄范围修正

回退 v56 要求“子类必须有父类与成员条件之外的业务断言”的门禁及 subclass_value，恢复 case 支持的类别/属性选择。属性限制定义的业务类别不因没有额外文档或动作而被拒绝；NL 最小化、包含关系、端点含义与证据一致性仍检查。保留取消默认 SHA-256 的可读 Value ID 和第八个核心关系 has_attribute。

生成与 Assessment 改为业务关系优先：有明确角色时使用具体谓词；has_attribute 是通用回退而非所有属性的必选名字。业务边 description 先解释业务联系，再补必要且有证据的范围/限制；仅写“某记录存了某字段”不够。grounded_on、value_of、belongs_to 等物理边仍解释映射，不禁止它们说明物理信息。

本次未重跑模型，v56 已发布图保留原始实验身份，不手工改图或覆盖证据。

## 兄弟类别：建议按独立事实表达

设 C_i、C_j 均包含于 B：

- `C_i ⊆ B` 与 `C_j ⊆ B` 不决定兄弟关系。
- 互斥：`C_i ∩ C_j = ∅`，只在明确的同一范围内成立。
- 存在交集：`∃x, C_i(x) ∧ C_j(x)`。它证明可以共属，不证明每个成员共属、类别等价或所有组合都允许。
- 完整覆盖：`B = ∪_i C_i`，是另一项独立断言。互斥不意味着覆盖，多个子类的存在也不意味着覆盖。

建议只对有依据的类别对保存局部业务边，例如 disjoint_with、overlaps_with；定义为对称关系时使用现有 undirected 契约，不保存反向重复边。这些不是新核心关系，也未在本次自动注册。省略边表示未知，不推断为兼容或互斥。不强制生成所有类别两两关系。

父类可以概述分类维度，例如资质和授课模式，但不能在 School 上笼统写“所有子类互斥”。某组已知兄弟的关系也不能自动扩展到未来新增子类。若未来确需表达有界分类组的 partition，再另行设计，不在当前新增分组节点/Constraint/qualifier。

仅凭字段不同不能证明所有类别组合都可能；仅凭同字段不同值，最多可在同一条记录的同一字段作用域内推导互斥，不能直接扩展成跨历史时期的业务对象互斥。同一学校跨时间处于不同状态不是同一时点的冲突。

本次只读检查公开 BIRD 数据，确认存在同一学校记录满足 Charter=1 且 Virtual='P'。因此这份数据范围内 Charter school 与 Partially virtual school 存在交集。这里不将行数、样本列表或探测 SQL 放进业务边 description。

## has_attribute 作为关系家族：建议，尚未实现

当前 EdgeType 没有 extends，不能仅在 prompt 中写“继承”便假装已有结构支持。可考虑新增可选的类型级 extends，以 has_attribute 为属性关系的基类：

```text
has_website          extends has_attribute
has_operating_status extends has_attribute
```

含义为 `∀u,v: specific(u,v) ⇒ has_attribute(u,v)`。每条实际边只保存具体谓词，不为同一事实再添加通用边；显示、检查和检索按类型层级识别其通用属性角色。父契约的合法端点和要求不得被放宽，须检查未知父类型、循环与不兼容覆盖。此方案需要 schema、验证、契约保存、inspect 及消费者共同支持，不能只新增字段。

located_in 可以是独立的业务关系：County 若表示地理区域，不应仅因为它与一列对应就强制把 located_in 归入属性关系。不能让所有业务谓词继承 has_attribute。是否添加单继承字段，以及需要支持多少家族，留待讨论。

## description 的业务表达示例

- has_website：关联学校与其网站访问地址。不能无依据增加“官方”“学校运营”“唯一地址”等限定。
- located_in：表明学校在县这一地理行政区域上的所在地归属；不自动等于管理学区或历史开校发生地。
- Charter school is_a School：按 Charter 资质界定的学校类别；不自动与授课模式类别互斥。
- Charter school overlaps_with Partially virtual school：两类存在交集，一所学校可以兼具 Charter 资质与部分虚拟授课方式。来源和观察范围需可追溯。

业务陈述和记录范围不互斥，但不能用“recorded on school record”代替业务陈述。未知且非必要的基数、缺失性或历史限制可以不写；不能为了满足格式补造。

## 标准参考

- [W3C Class Disjointness](https://www.w3.org/TR/owl2-primer/#Class_Disjointness)：多重类别成员资格与显式互斥声明分开。
- [W3C Property Hierarchies](https://www.w3.org/TR/owl2-primer/#Property_Hierarchies)：具体属性关系可以是一般关系的子属性。本文仅借鉴其语义，不声称现有系统实现了 OWL 推理或 EdgeType 继承。

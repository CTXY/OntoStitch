# v56：子类准入、核心属性关系与可读 Value ID

日期：2026-11-05。

> 历史实验记录：2026-11-05 已回退本文的“额外业务断言”子类准入及 subclass_value 门禁。可读 Value ID、核心 has_attribute 保留。下文记录当时实现与实跑，不能作为当前抽取政策；当前设计见 ontology-graph-design.md §2.7 与 ontology-business-semantics-v57-proposal.md。

## 决定

本项目将“逻辑上成立的集合包含”与“是否物化独立子类”分开。`C is_a B` 必须满足同作用域、可比较类别以及 `∀x, C(x) ⇒ B(x)`。此外，独立子类须承载至少一项有证据的业务断言 φ，不能只重复父类知识 K_B 与成员条件 P_C：

```text
Supported_E[∀x, C(x) ⇒ φ(x)]
K_B ∪ {B(x), P_C(x)} ⊭ φ(x)
```

P_C 仅为成员条件，不能将候选全部业务知识纳入它。φ 可以是额外或更具体的属性、角色、关系、限制、行为；不要求独占属性、新增字段或 GenerationRule。物理绑定、别名、选择条件重述不构成 φ；不得为维持类别补造断言。重叠与可变性均不决定是否为子类。

无见证时保留对象、属性/取值及必要业务关系，暂不建立该子类。这是项目的建模准入策略，不是“OWL 属性限制不能定义类”。不应用于普通基础 Concept，不增加覆盖率或全图压缩指标。完整规范见 ontology-graph-design.md §2.7。

## 代码落实

- 共享 Concept 定义、关系说明和 Assessment prompt 使用同一准入标准。
- Concept 的 quality_requirements 新增独立 `subclass_value`；普通非子类 Concept 通过该项，不需要额外知识。失败/不确定需要正常反馈，SQL 正确不能覆盖这一结果。
- 同时保留 NL 最小化、端点含义、绑定/公式与全图一致性检查，不把子类价值等同于字符串长度或普通 minimality。
- `has_attribute` 提升为第八个核心关系：Concept/Event → 属性角色 Concept。它不直接指向 Column/Value，不自动意味着实例属性非空、单值或取值域完整。物理实现仍由 grounded_on、value_of 表达。
- 八个核心关系不是封闭清单。Learning 可在一次 edit_graph 中提交新的 relation_contracts 与 edge_operations；已有同名契约不可重定义，历史退休谓词限制不变。现有测试保留任意新谓词注册/拒绝未注册谓词的实际工具路径。
- 新 Value ID 默认取消 SHA-256，采用值的可逆 percent 编码。示例：`dimension_value.schools.Virtual.P`、`dimension_value.schools.County.San%20Joaquin`。句点、百分号和 Unicode 无损编码，空字符串与空格、原始 `~`、形似旧哈希的真实字符串保持可区分。SQL 使用真实 value，不能直接用编码 ID。
- 复用已有经验证的 owner/typed value 时保留其旧 ID。旧值及其引用不批量改写，旧快照 schema 指纹不绕过。数值与字符串沿用已有词法身份政策，类型碰撞仍报错。

## 关键实现位置

- `llm/prompts/asset_contract.py`、`learning/assessment.md`、`learning/relations.md`、`learning/values.md`
- `training/quality_contract.py`
- `schema/roots/cortex_root.json`、`schema/relation_vocabulary.py`
- `core/ids.py`、`contracts/references.py`、`contracts/semantic_mapping.py`
- `graph/values.py`、`builders/dimension_values.py`

没有针对 School、Charter、Virtual、County、case ID、SQL 文本的确定性语义规则；领域名称仅出现在文档/数据示例中。属性与子类角色由 Agent 按证据判断。图 schema 未新增 Constraint、Attribute 节点基类或 qualifier。

## case60 运行

通过正式 sql-extract 运行，数据集为公开 BIRD california_schools question 60。工作区：`output/ontology_v56_case60_attribute_review`。

- r1 受网络沙箱限制，模型连接失败；保留失败记录，不将其视作知识质量失败或成功。
- r2 在允许的网络环境中重新运行；关闭此命令的额外 LangSmith tracing，未更改全局配置。
- 使用只读数据库、标准 Learning/Assessment/SQL replay/accept/seal；不使用 review_only，不手改最终图，不运行全局 merge。
- 图与正式验收结论以该目录 result/progress/evidence 为准。交互图由原始 Accepted snapshot 渲染；业务视图仅投影关联节点及必要连接键。

## 回归覆盖

可逆 ID（空值表示、空格/标点/percent/Unicode、长值、旧哈希形状）、旧 ID 复用与引用解析、has_attribute 无需本地注册、新业务谓词需契约、非法端点，以及 subclass_value 失败/缺失不能被正确 SQL 覆盖。模型实跑用于判断语义表现，单元测试不伪造“模型理解正确”的结论。

## 正式结果与 review

- 状态：stable_correct；Accepted revision：`d9068dfef32f4fd1bda85679ce03c8e1`。
- 两次连续正确 replay；18 个 Assessment 单元全 quality valid，六个 Concept 的 subclass_value 均通过。正式 accept/seal 证据匹配显示快照，无 review_only。
- 六个 Concept：School、School website、Charter school designation、Virtual instruction type、Partially virtual instruction、School county。无 Metric、Event、GenerationRule，也无学校 is_a 子类边。
- School 通过 has_attribute 连接网站、Charter designation、Virtual instruction type 和 County；Partially virtual instruction 通过本地 category_of 契约连接 Virtual instruction type，其 targets 为 `[["dimension_value.schools.Virtual.P"]]`。County 的对应 Value 为 `dimension_value.schools.County.San%20Joaquin`。
- Charter designation 是属性本身，绑定 schools.Charter；值 1 的当前请求匹配由 case/evidence 和生成 SQL 落实，不把每个字面值都强制变成 Value/Concept 节点。

最终 SQL：

```sql
SELECT Website
FROM schools
WHERE Virtual = 'P'
  AND Charter = 1
  AND County = 'San Joaquin';
```

实跑中自动移除了普通 nullable 投影 Rule，并修正超出证据的描述；未手改图。最终业务视图 14 节点、18 边，完整图 100 节点、104 边。业务视图附带保留属性列的已保存 Value，不生成新的语义边。

全量回归 901 passed（2 个既有依赖 warning）；专项 112 passed；语义门控测试 19 passed；Ruff 通过。源记录、失败 r1、r2 各轮证据、最终结果与 verified_run_summary.json 均保留。

[业务交互图](http://127.0.0.1:8771/case60_r2/business_graph.html) · [完整交互图](http://127.0.0.1:8771/case60_r2/graph.html) · [运行与评估首页](http://127.0.0.1:8771/index.html)

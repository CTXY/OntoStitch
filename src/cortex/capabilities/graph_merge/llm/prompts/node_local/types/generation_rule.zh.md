# 本组类型 Guidance：GenerationRule

## 1. 整合目标

整合同一个可复用的 SQL 生成决策，比较其结果义务、适用条件与完整行为。不同 SQL 写法可能等价；同一来源中共同出现的操作不一定属于同一决策。名称、检索信号和意图标签均不能证明等价。

## 2. 字段填写规范

| 字段 | 填写要求 |
| --- | --- |
| `properties.condition` | **非空字符串**；决策前可识别的业务/请求条件，不用 case ID 或循环表述作条件。 |
| `properties.action` | **非空字符串**；完整可实施的行为，保留来源已有操作数、计算次序和边界；参数化时说明绑定，必要时给 SQL 模式。 |
| `properties.reason` | **非空字符串**；保留有依据的决策理由及结果差别，字段名不是 `rationale`。 |
| `properties.operation_intents` | 可选字符串列表，归并相容标签；字段名为复数，标签不能代替 action。 |
| `properties.triggers` | **必填对象**；检索信号格式见下文，不是执行条件。 |

`triggers` 仅允许以下结构，三个列表可分别为空，但不能全空；列表元素为非空字符串：

```json
{
  "question": {"phrases": ["原问题中有依据的短语"]},
  "sql": {"columns": [], "operations": []}
}
```

`triggers.sql.columns` 使用本轮目录中真实列 ID。归并来源已有信号并去重，不新增 `keywords` 等字段、不捏造信号。命中 trigger 不表示应同时执行全部条件分支。

---

## 3. 合并、泛化与冲突

- 同一决策的相容说明、必要操作数信息与等价 SQL 模式可整合。不能因精简而把来源拥有的计算或结果义务推给未定义的“上游”。
- 请求参数（如 K、时间窗或阈值）只有在来源支持其角色及取值范围时才可泛化；在 condition/action 中说明如何绑定，不新增 `parameters` 字段。自然语言 K 不等于 Metric 的 `${key}` 节点引用。
- `COUNT(*)` 与 `COUNT(DISTINCT patient_id)` 须比较计数对象与唯一性；`RANK`、`DENSE_RANK` 与 `LIMIT` 须比较并列策略。证明等价才合并，证实同范围行为不同才保留完整竞争分支，未知则补证据。
- 条件化整合须完整保留各条件下的行为，例如是否保留未匹配对象决定 JOIN 策略；不能把“先聚合再 JOIN”泛化为任意顺序。
- 每个冲突分支保留自己的 condition、action、reason、triggers 与 intent；不混搭分支。

### 完整内容示例

假设两个来源分别请求前 3、前 5 个客户，且确认 K 为请求参数、粒度和并列策略一致，可写为以下 `NodeContent`：

```json
{
  "name": "选取前 K 个客户",
  "description": "按客户销售额降序选取请求数量的客户，销售额并列时按客户 ID 升序。",
  "aliases": [],
  "tags": ["客户排名"],
  "properties": {
    "condition": "请求按既定每客户销售额选取前 K 个客户，K 为请求给出的正整数；来源采用相同并列策略。",
    "action": "在每客户一行的销售额结果上按销售额降序、客户 ID 升序排序，再取请求 K 行；生成 SQL 时将 K 替换为请求值。",
    "reason": "保持选取客户的结果义务；代入 K=3 或 K=5 可还原原行为。",
    "operation_intents": ["top_k_selection"],
    "triggers": {"question": {"phrases": ["前"]}, "sql": {"columns": [], "operations": []}}
  }
}
```

该策略须来自实际输入，不作为所有排名规则的默认策略。

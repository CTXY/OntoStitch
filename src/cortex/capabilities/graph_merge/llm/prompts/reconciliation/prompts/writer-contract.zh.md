# 编辑与提交规范

## 1. 草稿编辑与提交机制
- `edit_draft({put_changes:[...], remove_changes:[...]})`：按稳定 `change_id` 完整替换草稿项，或撤回该草稿项。撤回草稿项不等于删除图对象。每项含 `change_id / kind / reason` 及对应字段；不填写版本或决策状态。
- 每个对象的同一版本只写一项；有关联的修改放在一次编辑中。格式错误不保存；格式正确则先保存草稿，再自动编译。编译失败时草稿仍保留，按回执的具体错误修订。相同编辑不产生新版本，不要重发确认。
- 每次编辑都是本轮完整候选；编译成功即自动进入独立 Assessment，失败则带具体错误返回 Writer。
- **空编辑格式**：无需修改时调用 `edit_draft`，参数为 `{}`；等价写法为 `{"put_changes":[],"remove_changes":[]}`。参数中没有 `empty` 字段，`{"empty":true}` 或 `{"empty":"true"}` 都不合法。空编辑不清空已有修改，不增加草稿版本，但仍请求独立 Assessment；结构合法不代表本轮义务已完成。

## 2. 图操作一览

| `kind` | 必填业务字段与含义 |
| --- | --- |
| `node_create` | `node_id / node_type / content / evidence_ids`；完整业务内容，类型字段见“节点字段要求”。 |
| `node_update` | `node_id / set`；修改普通节点。 |
| `edge_create` | `edge_id / edge_type / from_node_id / to_node_id / properties / evidence_ids`；`properties.description` 说明真实业务关系。 |
| `edge_update` | `edge_id / set`；修谓词、端点或属性，保留原有来源。 |
| `edge_replace` | `edge_id / replacement_edge_id`；旧边的知识由另一条真实存活边承接，Controller 将旧边来源并入后继再删除旧边。 |
| `edge_remove` | `edge_id / refs`；仅撤销确实无据或错误的断言，引用具体 `before` 依据；有应保留后继时使用 `edge_replace`。 |
| `relation_declare` | `contract`；定义尚未安装的具体业务谓词。 |

## 3. 字段、替换与引用约束
- **更新规则**：更新相对于固定 `before`：省略字段保留，`set.properties` 按键替换；删除可选属性须显式 `unset:["properties.<key>"]`。`null` 不表示删除。新增来源用公开 `evidence_ids`；原来源由代码保留。
- **边替换规则**：替换边与后继必须不同，后继可同批新建但必须最终存活；禁止替换链。可另用一个 `edge_update` 整合后继 `description`。解释中说“已保留”不能代替真实后继与来源迁移。
- **说明与契约**：`reason` 解释本次修改；`description` 描述长期业务事实，不写修复日志。只使用已提供谓词契约，或显式声明新契约。确需其他端点类型的已有契约时，先使用当前开放的 `read_predicates` 读取；未显示不代表未安装。
- **`refs` 格式**：用 `{view:"before",kind:"node|edge|contract",id:"真实ID",field:"公开字段路径（可省略）"}`；例如 `properties.definition` 或 `evidence_ids`。不引用存储层来源字段。

## 4. 每轮回复纪律与兜底
- 选择一种：批量读取或一次 `edit_draft`。不混用，不复述整图。
- 评估反馈说明问题及必须保留的结果，可作等价修复。
- 确实无法完成时输出 `{problem,refs}` 说明具体原因，保留未完成 checkpoint，不假装成功。

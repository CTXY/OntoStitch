# `Review` 填写契约

## 1. 调用与外层规范
- **直接调用**：直接调用 `Review`，不添加 `review`、`result` 等包装层，也不输出 Patch。
- **单结果调用**：本回复不得同时包含读取工具调用、其他结果调用或空占位调用。
- **顶层字段限制**：仅填写 `verdict` 与 `issues`。`candidate_hash`、`issue_refs`、内部来源账本等由 controller 管理，切勿填写。
- **结论与 `issues` 对应**：
  - `verdict` 为 `accept` 时，`issues` 必须为空列表（`[]`）。
  - 其他 `verdict` 必须至少包含一项具体 `issue`。

---

## 2. `verdict` 枚举与适用条件

| `verdict` | 适用条件 |
| --- | --- |
| `accept` | 局部比对材料完整，且不存在可证实的整合缺陷 |
| `revise` | 当前候选存在具体保义错误，且本任务具备足够前提指明修复目标 |
| `needs_context` | 必要局部材料或全局核查前提缺失，尚无法作出有依据的判断 |
| `revisit_identity` | 有证据表明计划成员并非同一身份，而非普通措辞或实现差异 |

---

## 3. `issues` 条目结构与字段约束

每项 `issue` 须遵循以下形状：

```json
{
  "refs": [{"kind":"node","id":"当前结果 owner ID","view":"candidate","fields":["properties.targets"]}],
  "problem": "当前结果具体有什么问题",
  "basis": [{"kind":"node","id":"原成员 ID","view":"before","fields":["properties.targets"]}],
  "required_outcome": "必须保留或修复的含义，不指定唯一偏好的文案"
}
```

- **必填非空字段**：`refs` 至少一项；`problem`、`required_outcome` 为非空字符串。
- **依据（`basis`）要求**：`revise`、`revisit_identity` 的每项 `issue` 必须提供非空 `basis`；`needs_context` 亦须说明准确对象与缺少的前提，但不得将猜测当作证据。
- **引用（`refs` / `basis`）规则**：
  - `kind` 仅允许为 `node`、`edge`、`contract`；`id` 须对应所选 `view` 中的真实对象。已删除成员的来源依据通常应引用 `before`，不得引用其已不存在的 `candidate` ID。
  - `fields` 须为真实对象字段路径（如 `properties.expression`、`properties.condition`、`description`），不得使用 `group.members[0]` 等输入 envelope 路径，也不得使用 `sources`、`common_sources`、`reference_origin`。
  - **评估 `alternative` 时**：当前 `ReviewReference` 无 `alternative_id` 字段，也不接受 `alternatives[0].content...` 或展示层 `content.*` 路径；应引用实际 `owner` 且 `fields=["alternatives"]`，并在 `problem` / `basis` 说明文字中明确真实分支 ID 与具体业务字段，不得新增引用字段。
  - 普通业务字段引用仅在该 `view` 的实际对象存在或由 `node_schema` 声明时使用。

---

## 4. 审查与边界约束
- **实质审查**：须实际比对内容、`relations` 与分支范围；**不得因 schema 校验通过就直接 `accept`**，也不得把旧问题列表原样当作最新事实。
- **局部范围边界**：本局部任务使用 `before` 与 `candidate`；`before` 仅为本 `group` 合并前快照，不代表具备全图权限。
- **禁止全图不存在断言**：不得输出基于“全图不存在”的结论；需要全局存在性核查时，使用 `needs_context` 指明前提并交由 controller 处理。
- **保义导向**：`required_outcome` 聚焦必须保留或修复的含义与目标，不强求唯一偏好的措辞。
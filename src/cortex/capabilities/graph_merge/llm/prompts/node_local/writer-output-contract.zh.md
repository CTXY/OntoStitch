# `submit_patch` 填写契约

本节为**节点阶段窄契约**；`relation_followups` 是交给后续关系阶段的移交字段。按本轮工具 schema 填写，不使用通用图编辑操作。

---

## 1. 调用与提交模式

- **直接提交**：参数平铺，不加 `patch` / `arguments` / `result` 包装。结果调用须是该回复唯一调用；读取可在另一回复成组发起。
- **正常结果**：必填 `explanation`，恰好一项 `integrate_groups`；可附 `relation_followups`。`failure` 省略或为 JSON `null`，不可为字符串 `"null"`。
- **无法整合**：仅填具体 `failure` 与 `explanation`，说明内容整合所缺的前提或身份反例。单纯待关系阶段处理的问题使用 followups，不用 failure。
- **逐层必填**：顶层、`integrate_groups[0]` 和每条 `source_routes` 各自的 `explanation` 都必填，不能互相替代。
- **字段边界**：不提交节点/边编辑、`sources`、`common_sources`、`overrides` 或内部版本账本。`properties` 中的节点引用仅用本组成员或已提供的 `endpoint_directory` ID；`relation_metadata` 仅用本轮原边 ID，缺少前提时报告，不猜全局 ID。

---

## 2. `integrate_groups` 必填内容

| 字段 | 填写要求 |
| --- | --- |
| `owner_id` | 精确复制计划 owner |
| `member_ids` | 至少 2 个、无重复、包含 owner，与计划成员集合完整一致 |
| `content` 或 `alternatives` | 二选一：完整普通结果，或至少两个完整分支 |
| `conflict_points` | 冲突结果必填且非空，准确说明未解决差异 |
| `source_routes` | manifest 中每个 `unit_id` 恰好一次，不遗漏、不增加、不重复 |
| `common_evidence_ids` | 可选；仅用于原知识在每个完整分支中均保留的已知证据 |
| `explanation` | 非空，简述整合决定 |

### 完整内容

`content` 及每个分支的 `content` 均须含 **name、description、aliases、tags、properties**：name 非空；description 为字符串或 null；aliases/tags 为字符串列表；properties 满足本轮类型 schema。

**content 为完整替换，不是字段增量。** 不包含 id、node_type 或 evidence_ids；不能依靠省略字段继承知识，也不能只提交差异或 overrides。

### 分支与差异点

每项 alternative 包含 `alternative_id`、完整 `content`、可选 `evidence_ids`。分支 ID 非空且唯一；所有证据 ID 必须已知。

每个分支须至少满足一项支持资格：自有 evidence_ids 非空；来源路由指向该分支；存在 common 路由。**仅填写 common_evidence_ids 不满足资格检查**；有资格仍需语义上支持完整分支。

`conflict_points` 使用以下结构；point_id 唯一，各文本非空，每个 point 至少两个 choices，引用实际输出分支并至少区分两个分支：

```json
[
  {
    "point_id": "p1",
    "aspect": "有争议的口径",
    "description": "同一范围下无法统一的具体差别",
    "choices": [
      {"alternative_ids": ["A"], "statement": "立场 A"},
      {"alternative_ids": ["B"], "statement": "立场 B"}
    ]
  }
]
```

---

## 3. `source_routes`

每项填写 `source_unit_id`、`destination`、非空 `explanation`。unit ID 精确复制 manifest，不能用节点或 evidence ID 代替。

| destination | 合法用途 |
| --- | --- |
| `{"kind":"result"}` | 原知识保留于普通结果；不可用于冲突结果 |
| `{"kind":"alternatives","alternative_ids":["A"]}` | 原知识保留于指定完整输出分支；列表非空且只引用实际分支 |
| `{"kind":"common"}` | 原单元知识在每个完整输出分支均保留；不可用于普通结果 |
| `{"kind":"discard"}` | 仅限有具体纠正或完整冗余依据的旧 alternative；normal_content/common 单元不可丢弃 |

只有 kind=alternatives 携带 alternative_ids。路由旧分支到保留结果时使用 result/alternatives/common；不得仅因措辞不同而丢弃其知识或来源。正文重复但仍携带需保留的来源或关系时，路由到保留结果，不用 discard；不得借此丢弃有依据的关系。

---

## 4. `relation_followups`

仅在本轮已发现具体关系语义问题时填写，通常省略：

```json
[
  {
    "edge_ids": ["E1"],
    "problem": "当前节点整合后，该关系的哪项语义仍需处理",
    "required_outcome": "后续关系阶段需完成的保义目标"
  }
]
```

edge_ids 精确引用**固定输入中的原边 ID**，非空且无重复，不猜编译后的 ID；两项文本非空。它记录移交事项，不执行边修改，也不表示关系已经修复。不报告普通 ID 重定向、端点未来合并或严格重复边。

---

## 5. 修订

每次提交完整替换当前草稿，始终相对同一份合并前输入；被拒草稿不是新基线。修复绑定当前草稿的全部有效问题；旧候选反馈不得冒充当前事实，已知仍需移交的 followups 随完整结果保留。

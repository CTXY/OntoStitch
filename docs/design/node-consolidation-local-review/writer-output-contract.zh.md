# `submit_patch` 填写契约

本节须与公开工具 schema 配合使用：schema 规定字段形状，本节规定互斥、覆盖、引用与整合边界。仅使用本轮提供的窄工具字段，不发送通用 `MergePatch` 中其他阶段的操作。

---

## 1. 调用外层与提交模式
- **参数平铺**：直接将字段作为 `submit_patch` 的 `arguments`，不添加 `patch`、`arguments`、`result` 等自定义包装。
- **单结果调用**：一个回复只允许一个结果调用，不得与读取工具或第二个 `submit_patch` 混用；读取调用可在单独回复中并行。
- **成功提交**：
  - 须包含非空 `explanation`；省略 `failure` 或填写 JSON `null`（不可填写字符串 `"null"`）。
  - 恰好一项 `integrate_groups` 以完整提交本 `group`；无边修改时省略边操作字段，不得用空候选绕过整合。
- **失败上报**：无法安全完成时，仅提交具体的 `failure` 与 `explanation`，不附带整合或边操作字段；明确指出缺少哪个对象的什么前提，或给出具体身份反例。
- **禁止额外字段**：未声明的额外字段会被拒绝；严禁输出 `sources`、`common_sources`、`overrides`、`merge_status`、内部哈希或版本账本。

---

## 2. `integrate_groups` 字段规范

| 字段 | 类型与必填要求 | 填写规则 |
| --- | --- | --- |
| `owner_id` | 字符串，必填 | 精确复制本 `group` 的计划 `owner` |
| `member_ids` | 字符串列表，必填 | 至少 2 个、无重复、包含 `owner`，集合与计划完整一致（非仅列变更成员） |
| `content` | 完整 `NodeContent`，普通结果使用 | 与非空 `alternatives` 互斥；冲突结果省略此字段 |
| `alternatives` | 完整分支列表，冲突结果使用 | 至少 2 个；普通结果省略或为空列表 |
| `conflict_points` | 冲突结果必填且非空 | 说明输出分支间真实未解决的差异（格式见第 4 节） |
| `common_evidence_ids` | 已知 evidence ID 列表，可选 | 仅列原知识在每个分支均获保留的证据；普通结果通常由 `source_routes` 自动继承，无需重复列来源 |
| `source_routes` | 来源路由列表，必填 | 本 `group` manifest 的每个 `unit_id` 恰好出现一次（不遗漏、不增加、不重复） |
| `explanation` | 非空字符串，必填 | 简要说明等价、互补、泛化或冲突的决策及关键保留方式 |

---

## 3. 完整 `NodeContent`（非字段增量）

`content` 与每个 `alternatives[i].content` 均须填写以下 5 项：

```json
{
  "name": "非空代表名称",
  "description": "完整业务说明；schema 允许 null，但不能因此丢掉输入知识",
  "aliases": [],
  "tags": [],
  "properties": {}
}
```

- `name` 为非空字符串；`description` 为字符串或 JSON `null`；`aliases`、`tags` 为字符串列表；`properties` 为对象。
- `properties` 的必填字段与类型由本轮 `node_schema` 及类型 guidance 决定；上文 `{}` 仅为结构示例，非完整内容。
- `NodeContent` 不含 `id`、`node_type`、`evidence_ids` 或来源字典（`owner` 与类别由任务确定，来源在外层指定）。
- **整块替换语义**：整个 `content` 为完整替换——省略某个旧 property 不代表继承，所有仍有效的业务字段须体现在结果中。这与 `edge_updates.properties` 的“按键更新”不同，切勿混用两种替换语义。

---

## 4. `alternative` 与 `conflict_points`

### `alternative` 结构与约束
```json
{
  "alternative_id": "branch-A",
  "content": {
    "name": "完整名称",
    "description": "该分支完整定义与条件",
    "aliases": [],
    "tags": [],
    "properties": {"本类型所需字段": "完整内容"}
  },
  "evidence_ids": []
}
```

- `alternative_id` 为非空字符串且在本结果内唯一；可沿用旧 ID 表示同一分支或使用新的唯一 ID，但不得引用不存在的旧输出 ID。
- `content` 须为该分支完整版本，不得仅填差异属性或自行构造 `overrides` / `common` content 包装。
- **支持资格前置检查**：每个分支至少满足其一——（1）该分支自有 `evidence_ids` 非空；（2）某条 `source_routes` 的 `alternatives` 去向包含该分支；（3）存在 `common` 去向的来源单元。**仅填写 `integrate_groups.common_evidence_ids` 不满足此项检查**；旧输入无 `evidence ID` 时可通过来源路由保留，切勿编造来源过校验，资格达标后仍须独立评估语义支持度。
- **合并与去重**：仅措辞、`aliases` 或 `tags` 不同的等价完整内容应合并为一个版本并归并来源，不人为制造冲突；不对旧分支机械求笛卡尔积，不跨分支拼接 `action`、`reason`、表达式或配对关系；若旧冲突已被等价去重或有据纠正完全解决，可输出普通 `content`。

### `conflict_points` 结构与约束
```json
[
  {
    "point_id": "p1",
    "aspect": "有争议的定义、匹配解释或计算口径",
    "description": "明确说明在哪个重叠范围内、哪些断言不能同时成立",
    "choices": [
      {"alternative_ids": ["branch-A"], "statement": "第一种完整立场的关键差别"},
      {"alternative_ids": ["branch-B"], "statement": "第二种完整立场的关键差别"}
    ]
  }
]
```

- `point_id` 在本节点内唯一；`aspect`、`description`、`statement` 均非空。
- 每个 `point` 至少两个 `choices`，每个 `choice` 的 `alternative_ids` 非空且引用实际输出分支，整体至少区分两个分支。
- `statement` 为差异说明，不可替代分支 `content`；不得保留引用已删除分支的旧 `conflict_points`。

---

## 5. 来源路由（`source_routes`）格式

```json
{
  "source_unit_id": "精确复制 source_unit_manifest 中的 unit_id",
  "destination": {"kind": "alternatives", "alternative_ids": ["branch-A"]},
  "explanation": "这份原完整知识如何保留在所列完整分支中"
}
```

| 输出形态 | 允许的 `destination` | 额外条件与限制 |
| --- | --- | --- |
| 普通 `content` | `{"kind":"result"}` | 完整普通结果保留该单元知识 |
| 普通或冲突结果 | `{"kind":"discard"}` | 仅限旧 `alternative`；须有具体纠正或完整冗余依据，不得因偏好丢弃 |
| `alternatives` | `{"kind":"alternatives","alternative_ids":["branch-A"]}` | 仅引用实际输出分支，可指向多个完整保留该单元的分支 |
| `alternatives` | `{"kind":"common"}` | 原单元实质知识在**每一个**完整输出分支均获保留 |

- 仅 `kind="alternatives"` 携带非空 `alternative_ids`，其他去向省略该字段。
- 普通结果不使用 `common`/`alternatives`；冲突结果不使用 `result`。
- `normal_content` 与 `common` 单元不得 `discard`；将旧 `alternative` 合并入普通结果应使用 `result`。
- `common` 为保留路径，不自动证明各分支新增断言，分支证据仍须准确归属。
- `source_unit_id` 须严格复制 manifest 真实句柄，不得用 node ID、evidence ID、`node/null` 或自造序号代替。

---

## 6. 必要出边操作

仅操作本 `group` 出边，使用输入提供的稳定边句柄，不猜测编译后新 ID。**前提条件**：须由 controller 明确保证机械整合后的句柄解析（现有通用 runtime 不可仅凭本段文字视为已具备该能力）；若无解析保证且需操作受映射影响的边，应报告缺少该前提；映射不唯一或语义决定缺失时反馈具体问题，不搜索全图找 ID。

| 操作 | 必填字段 | 特殊语义与约束 |
| --- | --- | --- |
| `edge_updates` | `id`；需变更的公开字段 | `from_node_id`/`to_node_id` 为端点字段，非 `properties`；`properties` 按键更新，所给键的值整体替换、其他键继承；新增来源用 `add_evidence` |
| `edge_merges` | `source_ids`、`survivor_id`、`explanation` | `source_ids` 为被吸收的**边 ID**（非 evidence ID），至少 1 项；`survivor` 为既有边（非新节点）；无 `properties` 字段 |
| `create_edges` | `id`、`edge_type`、`from_node_id`、`to_node_id`、完整 `properties`、非空 `evidence_ids` | `id` 须新鲜；从结果 owner 出发指向允许端点，谓词与端点类型匹配；不创建可用机械重定向替代的重复新边 |
| `retirements` | 非空 `before`、`after`、`explanation` | 元素为 `{"kind":"edge","id":"..."}`；`after` 为真实语义后继；仅明确纠正错误断言时可空，不无故丢弃关系 |

- `retirements` 的 `before`/`after` 元素不填 `alternative_id`（当前退役路径不支持单独退役分支；分支关系范围通过合法边操作与来源路由处理）。
- 每条边须含非空 `properties.description`，角色、条件与关系含义写入此处，不新增 `role`、`condition`、`qualifier`、`qualifiers` 属性。
- `properties.alternative_ids`（若存在）须为**边的起点节点**实际输出分支的非空、无重复 ID 列表（非终点分支或旧分支 ID）；普通起点不带此字段，不用空列表表示删除作用域。
- 无必要边修改时直接继承原边，不以相同端点为由盲目合并；同一边不可既作待删除对象又无解释更新；同一 `survivor` 的属性更新与等价 `edge_merges` 可配合，但须保留完整语义与作用域。

---

## 7. 修订轮次保留要求
- 每次提交均为相对同一份合并前局部输入的**完整草稿替换**，非在被拒草稿上追加；漏掉先前必要操作即视为撤回。
- 对照当前草稿绑定的**全部**未解决错误进行修复，不遗漏仍有效的问题。
- 当 `current draft` 与 `last valid candidate` 不同时，依据 controller 给出的 `draft`/`candidate`/`issues` 绑定解释各自事实，不将旧候选当作当前版本。
- 缺少安全修复所需的局部材料时使用 `failure`；不重复提交同一被拒 Patch，也不靠删减知识规避校验。
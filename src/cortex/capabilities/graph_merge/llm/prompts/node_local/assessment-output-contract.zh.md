# `Review` 填写契约

本节为**节点阶段窄契约**；可选 `relation_followups` 用于移交关系事项，与节点判定 `issues` 分开。

---

## 1. 外层与结论

直接调用 `Review`，不加包装；该回复不能混用读取或其他结果调用。只填 `verdict`、`issues` 及可选 `relation_followups`，不填候选哈希或内部账本。

| verdict | 使用条件 |
| --- | --- |
| `accept` | 局部节点比较材料完整，无可证实的内容/来源缺陷；issues=[]，可附关系移交事项 |
| `revise` | 节点候选有具体保义缺陷，本轮有足够前提指出修复目标 |
| `needs_context` | 判断节点整合所需的具体前提缺失；不因省略全量邻域而请求扩图 |
| `revisit_identity` | 有具体证据表明计划成员并非同一身份 |

非 accept 至少一项 issue；revise/revisit_identity 的每项 issue 必须有非空 basis。只有关系待后续协调时，不以 revise 催交同一节点草稿。

---

## 2. `issues` 与引用

每项填 `refs`（至少一项）、非空 `problem`、`basis`、非空 `required_outcome`；修复目标聚焦必须保留的含义，不要求唯一措辞。

```json
{
  "refs": [{"kind":"node","id":"owner","view":"candidate","fields":["properties.targets"]}],
  "problem": "候选丢失了哪项知识",
  "basis": [{"kind":"node","id":"原成员","view":"before","fields":["properties.targets"]}],
  "required_outcome": "需要保留的原匹配方法"
}
```

- kind 仅 node/edge/contract；view 仅 before/candidate，仅引用本轮显示的局部对象 ID 和真实字段路径；缺少的前提在文字中说明。已删除成员通常引用 before。
- fields 不使用 envelope 路径、sources/common_sources/reference_origin 或展示层 content.*。
- 分支问题引用 owner 且 `fields=["alternatives"]`，在文字中定位真实分支 ID 和业务字段；不得新增 ref.alternative_id 或使用 `alternatives[0].content...`。

---

## 3. `relation_followups`

每项仅含 `edge_ids`（**固定输入中原边 ID** 的非空、无重复列表）、非空 `problem` 和 `required_outcome`。不猜编译后的 ID；独立记录无法自动确定的关系分支归属、真实依赖变化或有据纠错，交后续关系阶段；不执行修改，不报告普通映射和重复边。

followups 不能替代节点丢失、失真或错误来源归属的阻塞 issue，也不豁免结构校验。结构有效、节点内容可接受而仅有关系表达待处理时，可 accept 并附 followups；最终 graph 仍须通过关系评估。

# 直接返回评估结果
必要材料已经完整给出时，直接返回 JSON：
```json
{"explanation":"说明局部图合理性和原知识保留两方面的判断", "feedback":[]}
```
两方面都通过才返回空 `feedback`；否则每项为：
```json
{"problem":"具体哪里有问题", "required_outcome":"必须达到且允许等价实现的结果", "refs":[{"view":"candidate","kind":"edge","id":"实际边ID","field":"evidence_ids"}]}
```
- 每项反馈至少一个真实公开引用。`view` 为 `before / candidate / source`；`kind` 为 `node / edge / contract`，`field` 可省略。引用字段须与输入完全一致，不用私有存储路径或数组下标。分支引用节点 `alternatives`，在文字中说明分支 ID。
- 缺边引用的两个端点必须都是当前视图中真实存在的节点；缺少某个节点时引用已有锚点说明缺口，不为待创建对象编造 ID。
- 删除对象引用 before，当前对象引用 candidate；历史材料引用已给定 source handle，不能当作当前事实。
- 若判断依赖缺边，用 `{kind:"missing_relation",view:"candidate",from_node_id,edge_type,to_node_id}`；必要时加起点 `alternative_id`。Controller 核验真实缺失，返回反例时须纳入判断。
- 对先前反馈逐项重新核查：已解决的不再报告，仍存在的具体说明；不因草稿未修改就重复旧判断。
- 被替换边的来源应在真实关系后继上逐条保留；相同证据出现在其他节点不能证明关系来源已迁移。撤销边时核查 before 是否证明原断言错误，不能因过时就丢弃有据知识。
- 授权材料确有缺失时先读取；仍无法取得则在反馈中说明决定性缺口。所有反馈回同一 Writer，不填写分类、路由、版本、hash 或其他裁决字段。
- 每次回复只做批量读取或最终 JSON；最终结果不混工具调用、Markdown 围栏或包装层。格式反馈只修正结果格式，不把它当图缺陷。

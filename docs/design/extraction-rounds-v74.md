# Extraction 5×5 轮次与无模型调用上限

日期：2026-11-14。

## 用户要求与实际配置

- outer loop：`max_rounds: 5`。
- inner loop：`max_iterations: 5`。
- `agent_max_steps: null`：不限制每个 Learning episode 的模型调用总数。
- `agent_episode_timeout: null`：不以 episode 墙钟时间中断。
- 模型：本次实验显式覆盖为 `deepseek-v4-pro`，包括 Learning、检索、SQL generation、Diagnosis、Assessment；不改共享 .env，以免影响并行项目任务。
- 单次网络请求仍有连接/响应 timeout，属于基础设施错误，不当作语义拒绝或耗尽训练轮次。SDK transport retries=0，全部真实调用留痕。

5 是上限，不强制已正确案例继续修复。正常交付、真实无法继续、基础设施错误仍有不同状态；不将草稿自动接受。

## 实现

`SqlTrainingConfig` 默认 5/5，模型调用 cap 与 episode timeout 默认 null。显式传入有限 `agent_max_steps` 的旧配置仍可用，用于兼容已有有界测试；此次真实实验不使用它。

无上限模式不再暴露 `model_calls_left` 或强制最后一次 handoff，也不会因三个工具错误/三次重复读取立即中止。重复读取仍提示使用已有证据，必要时仅提供 edit/handoff 来推进；工具字段校验、只读数据库约束、并行读取上限等保留。框架要求整数 recursion_limit，在无上限模式使用 sys.maxsize，使框架默认值不构成实际模型调用预算。

handoff.summary 现有的 2000 字符限制、edit_id 的现有字符/长度限制同步进入实际工具 schema，避免隐式校验造成额外失败。它们是内容格式要求，不是调用次数限制。

## 验证

144 个相关测试通过。新增测试确认超过旧 10 次调用仍可正常交付、连续工具错误可继续纠正，以及工具 schema 暴露的约束与后端一致。

真实测试目录：`output/extraction_pro_v74`。使用当前配置的 `ciyuanzhiyan.com`，首个真实响应的 response_metadata.model_name 为 `deepseek-v4-pro`。cases 36/63 从空语义图运行，不提供额外业务修复提示。默认连续正确 SQL replay 要求为 3，不沿用前次实验的 1。结果和全部 usage 在运行完成后追加。

## 本次真实运行结果

两例全部已完成本次尝试，未设置任何实验总调用预算。所有已返回响应均报告模型名 deepseek-v4-pro。

| Case | Learning | 检索/SQL | Assessment | 全部调用 | 已报告 tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| 36 | 8 | 6 | 1（502） | 15 | 328,387 |
| 63 | 10（末次502） | 0 | 0 | 10 | 223,724 |
| 总计 | 18 | 6 | 1 | 25 | 552,111 |

case 36 成功交付 4 个语义节点（3 Concept、1 GenerationRule），handoff summary 458 字符；新图完成 3 次连续正确 SQL replay。完整 Assessment 被代理返回的 `502 Upstream service temporarily unavailable` 中断，尚未接受该语义图。

case 63 返回了 9 次成功 Learning 响应，第 10 次请求被同类 502 中断，未完成语义图抽取。恰好第 10 次出错不代表 10 次上限：manifest 与实际上下文均无模型调用预算，测试也验证了可超过旧上限。

本次两例均在修复循环之前遭遇基础设施错误，没有耗尽 5×5 轮次，也没有有效的 ontology invalid 结论。不重复盲目调用；case 36 的 Assessment checkpoint 可在服务恢复后继续。

已报告输入 tokens 475,152，输出 76,959。2 次失败请求无 usage，实际完整计费 token 未知。144 个相关测试和 Ruff 检查通过。此次更换模型、工具 schema 和运行预算，不构成只改变模型的严格 A/B 实验。

审查页面：`http://127.0.0.1:8774/extraction_pro_v74/index.html`。

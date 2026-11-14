# Extraction 与统一规范的最小同步

日期：2026-11-14。基线提交：96c98466425521aaa8ec4cd693f4e99aec8408a4。

## 修改范围

仅更新现有定义、关系指导和质量 rubric，不新增节点/关系类型、工具、质量检查项或评估阶段，不改变 SQL replay、接受门槛和 case-level 工作流。

1. 在共享 Concept 定义中明确允许任务相关的具体业务对象，不要求枚举全部实例；节点指代须明确。
2. 在共享关系指导及 support rubric 中说明：未逐字描述、未在 SELECT 中投影或未观察到，本身不否定合理业务解释。真实绑定、定义计算、Event 创建与 Rule 要求保持不变。
3. Concept/Metric 最小化拒绝必须给出具体保义拆分，不要求证明绝对不可分；inconclusive 保留给无法判断实际含义或强制要求的材料性缺口。
4. 普通业务边不沿 is_a 在父子类间自动搬移，除非有独立依据或明确允许的规则。

不增加 Source differences、完整关系契约副本、全图检查章节、共同维度必做检查或通用推理器。

## 运行时路径

- `asset_contract.py.CONCEPT_DEFINITION` 由类型 guide 与 Assessment 共用。
- `asset_contract.py.RELATION_SCOPE_RULE` 由关系 guide、Event guide 与 Assessment 共用。
- `quality_contract.py` 的相关 rubric 按实际 unit requirements 进入 Assessment payload，不向每个 unit 广播全部检查。
- `learning/assessment.md` 仅替换已有证据缺口措辞，不新增判断层级。
- 契约版本更新为 `database_ontology_assets_v73_case_semantics`，已有上下文 fingerprint 会随之失效。

自动检查直接比较渲染后共享文本，而非仅比较源 Markdown。新增语义指导使 Event guide 约 7,920 字符，原 7,800 字符预算调整为 8,000；compact schema 的结构和相对压缩检查保持不变。

## 小范围真实测试

选择已授权的 california_schools cases 36、63，分别从空语义图启动生产 Extraction。使用当前配置 deepseek-v4-flash、ciyuanzhiyan.com；无 LangSmith 外发，SDK retries=0。正式有界运行每 case 最多 40 次实际模型调用，Learning 每 episode 20 步、最多 1 轮/1 次修订，SQL validation transport retry=0，连续正确 replay 要求为 1。此为有界测试配置，不代表默认生产的连续 3 次验证。

完整模型输入、输出与 provider usage 保存在 `output/extraction_contract_v73`。不向模型额外提供针对预期图的修复反馈。最终结果、实际接收内容核验与完整成本在该目录的 review 页面及 JSON 报告中记录；未完成前不视为通过。

### 初次运行与恢复说明

初次 r1 实验每 episode 仅 10 步，两例均未完成显式 handoff。且实验启用 streaming 时，ThreadedChatModel 的检索路径在当前 SDK 下将 Stream 误作完整响应，导致 ValueError。未将其认定为 ontology 质量问题。一次原输入诊断复现了此错误。r2 关闭实验流式模式并恢复 20 步预算，从空图重新运行；不改生产 adapter 或其他流程。r1 和诊断调用均计入完整实验成本，无法取得的 provider usage 不估算。

### 真实测试的限制与补充评估

r2 case 36 生成了语义草稿，但最终 handoff summary 超过工具已有的 2,000 字符限制，在此前两个无效读取之后触发连续错误退出；控制器没有接受草稿。随后仅物理上下文的 SQL replay 返回第一个管理员槽位，与 Gold 的三个槽位不一致。不能将此归因于 ontology 最小化语义检查，也不能称为通过。

r2 case 63 正常进入 Assessment，但代理返回 502；从同一 checkpoint 恢复 Assessment 后仍返回 502，没有重复抽取。对 case 36 全草稿的独立语义评估请求也遇到 502。

因此补充两次有界的内容评估：case 36 仅评估实际生成的存储计数 Concept 与 Excellence Rate Metric；case 63 仅评估实际生成的管理员 Concept、学校/管理员关系及 charter number 关系。使用生产 Assessment prompt、payload 构造和输出校验；其余相关知识作为上下文。未提供该草稿的 SQL application evidence，不据此发布或称为完整 workflow 通过。这是实验范围缩小，不改变生产 Assessment 调度或接受标准。

### 最终记录

97 个相关测试通过，Ruff 通过。实际调用输入中的共享 Concept/关系文本一致，Assessment payload 已包含新的 support 和最小化措辞。case 63 的三个局部 unit 语义质量为 valid；application 证据缺失仍被正确区分，没有据此接受或发布。case 36 的最小化真实评估未取得有效返回，不声称通过。

全部尝试共 68 次模型调用，61 次返回可计量 usage：输入 1,279,384、输出 284,874，共 1,564,258 reported tokens；7 次失败无 usage，实际总计费 token 未知。两次 r2 从零运行共 41 次调用，reported tokens 1,067,629；其他调用为初次失败、诊断、checkpoint 恢复与局部评估。模型 deepseek-v4-flash，接口 ciyuanzhiyan.com。未证明质量改善或 token 降低，不外推大规模稳定性。

完整审查页面：`http://127.0.0.1:8774/extraction_contract_v73/index.html`。页面明确区分未接受草稿、有限评估和流程失败；所有模型输入输出、尝试和成本均保留。

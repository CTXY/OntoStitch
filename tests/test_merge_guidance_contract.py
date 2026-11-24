"""An available semantic guide must not hide a missing field schema."""
import json
from importlib.resources import files

from langchain_core.messages import AIMessage
from test_graph_integration import catalog, concept, state
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.schema import load_root_schema


def test_assessment_can_get_type_schema_on_demand():
    ctx = IntegrationContext(state(), state([*catalog(), concept('A', 'a')]), load_root_schema())
    ctx.candidate = ctx.union_state()
    guide = {'types': ['concept']}
    chat = Chat([
        AIMessage(content='', tool_calls=[{'name': 'read_guidance', 'id': 'schema', 'args': {'topic': 'concept'}}]),
        AIMessage(content='', tool_calls=[{'name': 'Review', 'id': 'review', 'args': {'verdict': 'accept'}}]),
    ])
    IncrementalIntegrationAgent(chat).invoke('node_assess', ctx, {'node_guidance': guide, 'evidence': []})
    answer = json.loads(chat.requests[1][-1].content)
    assert 'definition' in answer['fields'] and 'targets' in answer['fields']
    assert len(chat.requests) == 2


def test_relation_contract_does_not_request_blanket_branch_content_audits():
    from cortex.capabilities.graph_merge.reconciliation.policy import reconciliation_system
    prompt = reconciliation_system('assessment', ['related_decisions'])
    assert '不重做 extraction 审计' in prompt
    assert '不重新审计已接受 Rule 的原子性' in prompt
    assert 'Evaluate name/action/condition/reason and other required fields on each complete resolved alternative.' not in prompt


def test_node_boundary_defers_edge_organization_not_embedded_knowledge():
    root = files('cortex.capabilities.graph_merge.llm.prompts')
    shared = root.joinpath('incremental_content_shared.md').read_text()
    writer = root.joinpath('incremental_consolidate.md').read_text()
    reviewer = root.joinpath('incremental_node_assess.md').read_text()
    assert '将边组织留到 Reconciliation，不免除本阶段保留嵌入关系知识的责任' in shared
    assert '保留内容中嵌入的关系、层级和配对事实' in writer
    assert '报告该具体丢失，不要求删除本来有效的边' in reviewer
    assert '纯粹的边组织属于 Reconciliation' in reviewer


def test_concept_matching_is_not_implicitly_replaced_by_business_edges():
    root = files('cortex.capabilities.graph_merge.llm.prompts')
    shared = root.joinpath('incremental_content_shared.md').read_text()
    reviewer = root.joinpath('incremental_node_assess.md').read_text()
    assert '相容的输入匹配组保留为独立项' in shared
    assert '业务属性边或表示边不是隐含可执行匹配方法' in shared
    assert 'grounding owner 约束针对直接边' in shared
    assert '不盲目并集不兼容或互斥的方法' in shared
    assert '完整含义、条件、匹配方法' in reviewer

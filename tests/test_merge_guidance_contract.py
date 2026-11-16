"""An available semantic guide must not hide a missing field schema."""
import json
from importlib.resources import files

import pytest
from langchain_core.messages import AIMessage
from test_graph_integration import catalog, concept, state
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.schema import load_root_schema


@pytest.mark.parametrize('with_fields', [False, True])
def test_assessment_can_get_missing_schema_without_repeating_guidance(with_fields):
    ctx = IntegrationContext(state(), state([*catalog(), concept('A', 'a')]), load_root_schema())
    ctx.candidate = ctx.union_state()
    guide = {'types': ['concept'], 'guidance': 'Already supplied preservation instructions.'}
    if with_fields:
        guide['fields_by_type'] = {'concept': {k: v.model_dump(mode='json')
            for k, v in ctx.schema.effective_node_fields('concept').items()}}
    chat = Chat([
        AIMessage(content='', tool_calls=[{'name': 'read_guidance', 'id': 'schema', 'args': {'topic': 'concept'}}]),
        AIMessage(content='', tool_calls=[{'name': 'NodeReview', 'id': 'review', 'args': {'verdict': 'accept'}}]),
    ])
    IncrementalIntegrationAgent(chat).invoke('node_assess', ctx, {'node_guidance': guide, 'evidence': []})
    answer = json.loads(chat.requests[1][-1].content)
    if with_fields:
        assert answer['already_supplied'] is True
        assert 'fields' not in answer
    else:
        assert answer['guidance_already_supplied'] is True
        assert 'definition' in answer['fields'] and 'targets' in answer['fields']
        assert 'guidance' not in answer
    assert len(chat.requests) == 2


def test_relation_contract_does_not_request_blanket_branch_content_audits():
    prompt = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('incremental_relation_shared.md').read_text()
    assert '不重新审计其事实、公式、理由或原子性' in prompt
    assert 'read missing content only for a specific relation decision or necessary node reconstruction' in prompt
    assert 'Evaluate name/action/condition/reason and other required fields on each complete resolved alternative.' not in prompt


def test_node_boundary_defers_edge_organization_not_embedded_knowledge():
    root = files('cortex.capabilities.graph_merge.llm.prompts')
    shared = root.joinpath('incremental_content_shared.md').read_text()
    writer = root.joinpath('incremental_consolidate.md').read_text()
    reviewer = root.joinpath('incremental_node_assess.md').read_text()
    assert 'Deferring graph organization to Reconciliation does not defer or waive preservation of those facts' in shared
    assert 'defer their graph organization, not the facts themselves' in writer
    assert 'deleting an edge is not a substitute for preserving the input knowledge' in reviewer
    assert "Pure edge organization remains Reconciliation's work" in reviewer


def test_concept_matching_is_not_implicitly_replaced_by_business_edges():
    root = files('cortex.capabilities.graph_merge.llm.prompts')
    shared = root.joinpath('incremental_content_shared.md').read_text()
    reviewer = root.joinpath('incremental_node_assess.md').read_text()
    assert 'Preserve compatible input matching groups as separate items' in shared
    assert 'business attribute/representation edge is not an implicit executable matching method' in shared
    assert 'Grounding ownership concerns direct edges' in shared
    assert 'Do not blindly union incompatible or exclusive methods' in shared
    assert 'Check source matching groups and their conditions using the shared Concept contract' in reviewer

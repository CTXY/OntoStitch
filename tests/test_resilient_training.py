"""Credential hygiene and unbounded merge episodes retain semantic validation."""
import json

import pytest
from langchain_core.messages import AIMessage
from test_agent_runtime import ScriptModel
from test_graph_integration import catalog, concept, state

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.schema import load_root_schema


def test_key_slots_are_ordered_private_and_validate_duplicates(tmp_path):
    path = tmp_path / 'pool.keys'
    path.write_text('# local configuration\nfirst-test-credential\n\nsecond-test-credential\n')
    slots = read_key_slots(path)
    assert [s.index for s in slots] == [0, 1]
    assert slots[1].api_key.get_secret_value() == 'second-test-credential'
    assert 'test-credential' not in repr(slots)
    path.write_text('duplicate\nduplicate\n')
    with pytest.raises(ValueError, match='duplicate'):
        read_key_slots(path)


def test_uncapped_merge_can_correct_format_beyond_twelve_calls():
    context = IntegrationContext(state(), state([*catalog(), concept('A', 'a')]), load_root_schema())
    # Malformed output is never accepted; the thirteenth valid result can finish.
    model = ScriptModel([*[AIMessage(content='not JSON') for _ in range(12)],
                         AIMessage(content=json.dumps({'groups': [], 'distinctions': []}))])
    agent = IncrementalIntegrationAgent(model, max_calls=None)
    result = agent.invoke('identity', context, {'incoming_seeds': []})
    assert result.groups == () and result.distinctions == ()
    assert len(model._calls) == 13

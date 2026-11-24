"""Current node prompts preserve embedded meaning while limiting graph edits."""
from cortex.capabilities.graph_merge.consolidation.contract import node_system


def test_node_boundary_defers_relation_repairs_without_granting_global_graph_edits():
    writer = node_system('writer', 'concept')
    assert 'original outgoing relations and brief endpoint meanings' in writer
    assert 'Relations between different result subgroups survive' in writer
    assert 'Other genuine relationship repairs use relation_followups' in writer
    assert 'do not predict their identities' in writer
    assert 'one draft and one atomic Assessment' in writer


def test_concept_matching_remains_explicit_content_in_current_type_guidance():
    writer = node_system('writer', 'concept')
    assessment = node_system('assessment', 'concept')
    assert 'targets' in writer and 'definition' in writer
    assert 'targets' in assessment and 'definition' in assessment
    assert 'source' in assessment.lower() and 'matching' in assessment.lower()

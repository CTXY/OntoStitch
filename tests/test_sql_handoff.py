from cortex.capabilities.online_context.output.sql_handoff import render_sql_handoff


def test_sql_view_preserves_resolved_constraints_without_promoting_equivalent_or_unselected_rules():
    payload={'interaction_guidance':[{'source':'user_choice','decision_question':'Which key?','should_be':'Use customer_id','must_not_be':['Use customer_name']},{'source':'database_equivalent','decision_question':'Which join direction?','should_be':'FORCE_AN_EXTRA_JOIN','must_not_be':[]}], 'ontology_assets':[{'type':'concept','summary':'Customer is identified by customer_id.'},{'type':'metric','summary':'Revenue = price * quantity.'},{'type':'generation_rule','summary':'UNSELECTED_EXTRA_OUTPUT'}]}
    text=render_sql_handoff(payload)
    assert 'Use customer_id' in text and 'Use customer_name' in text
    assert 'Customer is identified' in text and 'Revenue = price * quantity' in text
    assert 'FORCE_AN_EXTRA_JOIN' not in text and 'UNSELECTED_EXTRA_OUTPUT' not in text
    assert len(payload['interaction_guidance'])==2 and len(payload['ontology_assets'])==3


def test_default_online_handoff_keeps_rule_record_but_not_unconfirmed_rule_action():
    from cortex.application.online_context import InteractionGuidance
    from cortex.capabilities.online_context.output.projection import build_downstream_guidance
    from cortex.schema import GraphNode,GraphSpace
    concept=GraphNode(id='object',node_type='concept',name='object',properties={'targets':[['column.id']]})
    rule=GraphNode(id='rule',node_type='generation_rule',name='A prior presentation',properties={'condition':'A different requested output set','action':'UNCONFIRMED_EXTRA_COLUMNS','reason':'A source task requested these fields.'})
    choice=InteractionGuidance(guidance_id='confirmed',decision_question='Which representation?',should_be='Use column.id',source='user_choice')
    handoff=build_downstream_guidance(question='Return the ID',initial_sql=None,graph=GraphSpace(nodes=(concept,rule)),relevant_semantic_node_ids={'object','rule'},interaction_guidance=(choice,),resolved_guidance_content='Use column.id')
    assert len(handoff.ontology_assets)==2
    assert any(a.get('action')=='UNCONFIRMED_EXTRA_COLUMNS' for a in handoff.ontology_assets)
    assert 'UNCONFIRMED_EXTRA_COLUMNS' not in handoff.guidance_content
    assert 'Use column.id' in handoff.guidance_content

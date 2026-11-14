from cortex.capabilities.online_context.output.sql_handoff import render_sql_handoff


def test_sql_view_preserves_resolved_constraints_without_promoting_equivalent_or_unselected_rules():
    payload={'interaction_guidance':[{'source':'user_choice','decision_question':'Which key?','should_be':'Use customer_id','must_not_be':['Use customer_name']},{'source':'database_equivalent','decision_question':'Which join direction?','should_be':'FORCE_AN_EXTRA_JOIN','must_not_be':[]}], 'ontology_assets':[{'type':'concept','summary':'Customer is identified by customer_id.'},{'type':'metric','summary':'Revenue = price * quantity.'},{'type':'generation_rule','summary':'UNSELECTED_EXTRA_OUTPUT'}]}
    text=render_sql_handoff(payload)
    assert 'Use customer_id' in text and 'Use customer_name' in text
    assert 'Customer is identified' in text and 'Revenue = price * quantity' in text
    assert 'FORCE_AN_EXTRA_JOIN' not in text and 'UNSELECTED_EXTRA_OUTPUT' not in text
    assert len(payload['interaction_guidance'])==2 and len(payload['ontology_assets'])==3

"""Local Agent context stays bounded without restricting explicit graph reads."""

from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.tools import build_tools
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import (
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)


def event(key):
    return GraphNode(
        id=key,
        node_type="event",
        name=key,
        properties={"definition": f"An occurrence named {key}."},
        sources=(SourceReference(source_id=key),),
    )


def evidence(key):
    return EvidenceItem(
        id=f"evidence:{key}",
        source=SourceReference(source_id=key),
        case_id=key,
        content={"question": f"Question for {key}"},
    )


def test_local_evidence_directory_does_not_include_unrelated_global_cases():
    old = state([event(f"old{i}") for i in range(40)])
    context = IntegrationContext(
        old,
        state([event("new")]),
        load_root_schema(),
        evidence=[evidence(n.id) for n in old.graph.nodes] + [evidence("new")],
        top_k=1,
    )
    payload = context.payload("identity")
    assert len(payload["nodes"]) == 2
    assert len(payload["evidence_directory"]) == 2
    known = {s for row in payload["evidence_directory"] for s in row["source_ids"]}
    distant = next(n for n in old.graph.nodes if n.id not in known)
    inspect, read_evidence, _ = build_tools(context)
    assert inspect.invoke({"mode": "nodes", "ids": [distant.id]})["items"]
    result = read_evidence.invoke({"ids": [distant.sources[0].source_id]})
    assert result["items"][0]["content"] == {"question": f"Question for {distant.id}"}
    assert not result["missing_ids"]


def test_default_relation_read_is_local_but_catalogue_is_explicitly_readable():
    incoming = state([*catalog(), concept("A", "a")], [edge("ga", "A", "a")])
    context = IntegrationContext(state(), incoming, load_root_schema())
    context.payload("identity")
    inspect = build_tools(context)[0]
    result = inspect.invoke({"mode": "relations"})
    assert [e["edge_type"] for e in result["items"]] == ["grounded_on"]
    physical = inspect.invoke(
        {"mode": "relations", "ids": [context.input_node_map["table"]]}
    )
    assert physical["total"] == 3
    assert {e["edge_type"] for e in physical["items"]} == {"belongs_to"}


def test_default_candidate_relations_include_created_nodes_but_not_distant_nodes():
    old = state([*catalog(), concept("distant", "b")], [edge("gd", "distant", "b")])
    context = IntegrationContext(old, state(), load_root_schema())
    created = concept("created", "a")
    context.candidate = GraphState(
        graph=GraphSpace(
            nodes=(*context.nodes.values(), created),
            edges=(*context.edges.values(), edge("new-grounding", "created", "a")),
        )
    )
    result = build_tools(context)[0].invoke({"mode": "relations", "view": "candidate"})
    assert [e["id"] for e in result["items"]] == ["new-grounding"]


def test_directory_and_contracts_include_related_edges_beyond_initial_page():
    schema = load_root_schema()
    used = schema.edge_type_index()["has_attribute"].model_copy(
        update={"id": "custom_attribute"}
    )
    unused = used.model_copy(update={"id": "unused_attribute"})
    nodes = [
        *catalog(),
        concept("root", "a"),
        *[concept(f"c{i}", "b") for i in range(61)],
    ]
    relations = [edge(f"e{i}", "root", f"c{i}", "custom_attribute") for i in range(61)]
    relations[-1] = relations[-1].model_copy(
        update={"sources": (SourceReference(source_id="late-edge"),)}
    )
    incoming = GraphState(
        graph=GraphSpace(
            nodes=tuple(nodes),
            edges=tuple(relations),
            relation_contracts=(used, unused),
        )
    )
    context = IntegrationContext(
        state(), incoming, schema, evidence=[evidence("late-edge")]
    )
    payload = context.payload("identity")
    assert payload["more_relations"]
    assert not any(e.get("sources") for e in payload["relations"])
    assert payload["evidence_directory"] == [
        {"id": "evidence:late-edge", "source_ids": ["late-edge"]}
    ]
    assert [c["id"] for c in payload["local_contracts"]] == ["custom_attribute"]


def test_rule_candidates_recall_shared_binding_through_different_concepts():
    # Different words and intents can still contain a shared implementation choice.
    from cortex.schema import GraphEdge
    def rule(key, name, intent):
        return GraphNode(id=key, node_type="generation_rule", name=name,
                         properties={"operation_intents": [intent], "condition": "A matching value is requested.",
                                     "action": "Select the supported representation.", "reason": "The source selects it.",
                                     "triggers": {"question": {"phrases": ["value"]}, "sql": {"columns": [], "operations": []}}})
    old = state([*catalog(), concept("old_term", "a"), concept("unrelated", "b"),
                 rule("related", "Encode a label", "presentation"),
                 rule("lexical", "Choose contact", "column_binding_choice")], [
        GraphEdge(id="use_related", edge_type="applies_to", from_node_id="related", to_node_id="old_term", properties={"description": "Uses this operand."}),
        GraphEdge(id="use_other", edge_type="applies_to", from_node_id="lexical", to_node_id="unrelated", properties={"description": "Uses this operand."}),
    ])
    incoming = state([*catalog(), concept("new_term", "a"), rule("seed", "Choose contact", "column_binding_choice")], [
        GraphEdge(id="use_seed", edge_type="applies_to", from_node_id="seed", to_node_id="new_term", properties={"description": "Uses this operand."}),
    ])
    context = IntegrationContext(old, incoming, load_root_schema(), top_k=10)
    candidates = context.candidates(context.input_node_map["seed"])["items"]
    ids = [row["id"] for row in candidates]
    assert ids.index("related") < ids.index("lexical")
    assert "shared physical/semantic references" in next(r for r in candidates if r["id"] == "related")["signals"]
    # Retrieval adds no identity decision, generated edge or inferred source fact.
    assert not any(set(pair) == {"related", context.input_node_map["seed"]} for pair in context.mandatory)


def test_prior_phase_reads_do_not_hide_definitions_from_new_conversation():
    context = IntegrationContext(state([event('old')]), state([event('new')]), load_root_schema())
    payload = context.payload('identity')
    context.read_ids.update(context.nodes)
    seed = context.input_node_map['new']
    inspect = build_tools(context)[0]
    first = inspect.invoke({'mode': 'candidates', 'ids': [seed]})
    assert first['items'] and all('node' in x for x in first['items'])
    second = inspect.invoke({'mode': 'candidates', 'ids': [seed]})
    assert all(x.get('already_in_context') and 'node' not in x for x in second['items'])
    fresh = build_tools(context)[0].invoke({'mode': 'candidates', 'ids': [seed]})
    assert all('node' in x for x in fresh['items'])
    preloaded = build_tools(context, visible_source_ids={n['id'] for n in payload['nodes']})[0]
    assert all('node' not in x for x in preloaded.invoke({'mode': 'candidates', 'ids': [seed]})['items'])


def test_business_edges_are_visible_before_grounding_and_pagination_agrees():
    columns = [GraphNode(id=f'field{i}', node_type='column', name=f'field{i}', properties={'table_ref': 'table', 'column_name': f'field{i}'}) for i in range(65)]
    nodes = [*catalog(), *columns, *[concept(f'c{i}', f'field{i}') for i in range(65)]]
    edges = [edge(f'b{i}', f'c{i}', f'field{i}') for i in range(65)]
    edges.append(edge('business', 'c0', 'c1', 'is_a'))
    context = IntegrationContext(state(), state(nodes, edges), load_root_schema())
    payload = context.payload('identity')
    assert payload['relations'][0]['edge_type'] == 'is_a'
    tool = build_tools(context)[0]
    first = tool.invoke({'mode': 'relations', 'limit': 100})
    assert first['items'][0]['edge_type'] == 'is_a'
    assert first['items'][0]['id'] == payload['relations'][0]['id']

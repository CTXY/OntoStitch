from test_graph_integration import catalog, concept, edge, state

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.graph.edit_contract import Change, Element
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask, build_task_context
from cortex.capabilities.graph_merge.llm.packets import compact_node_packet
from cortex.schema import GraphSpace, GraphState, load_root_schema


def test_retired_archived_records_are_explicitly_distinguished_from_live_copies():
    original = state([*catalog(), concept("A", "a"), concept("B", "b")], [edge("old-edge", "A", "B", "has_feature")])
    ctx = IntegrationContext(state(), original, load_root_schema())
    before = ctx.union_state()
    a, b = (ctx.input_node_map[k] for k in ("A", "B"))
    old_edge = ctx.input_edge_map["old-edge"]
    replacement = next(n for n in before.graph.nodes if n.id == a).model_copy(update={"id": "live-node"})
    relation = next(e for e in before.graph.edges if e.id == old_edge).model_copy(update={"id": "live-edge", "from_node_id": "live-node"})
    current = before.model_copy(update={"graph": before.graph.model_copy(update={
        "nodes": tuple(n for n in before.graph.nodes if n.id != a) + (replacement,),
        "edges": tuple(e for e in before.graph.edges if e.id != old_edge) + (relation,),
    })})
    lineage = LineageIndex(ctx)
    lineage.observe(before, current, [
        Change(before=(Element(kind="node", id=a),), after=(Element(kind="node", id="live-node"),), explanation="Accepted model rewrite."),
        Change(before=(Element(kind="edge", id=old_edge),), after=(Element(kind="edge", id="live-edge"),), explanation="Accepted assertion rewrite."),
    ])
    snapshot = current.model_dump_json()
    packet = build_task_context(ctx, current, LocalTask("review", "node_assess", {"live-node"}), origins=lineage, before=current)
    assert a in packet["source_presence"]["archived_only_node_ids"]
    assert old_edge in packet["source_presence"]["archived_only_edge_ids"]
    assert b not in packet["source_presence"]["archived_only_node_ids"]
    assert compact_node_packet(packet)["source_presence"] == packet["source_presence"]
    assert current.model_dump_json() == snapshot


def test_shared_edge_is_present_in_each_selected_branch_while_a_subset_stays_local():
    from ontology_v2_fixtures import discussion_state
    original = discussion_state()
    owner = next(n for n in original.conflict_nodes if n.node_type == "generation_rule")
    target = next(n for n in original.graph.nodes if n.node_type == "concept")
    all_ids = [a.alternative_id for a in owner.alternatives]
    actions = {GraphManager(load_root_schema()).resolve_alternative(owner, a).properties["action"] for a in owner.alternatives}
    assert len(actions) > 1
    observed = []
    for scope in (None, all_ids, all_ids[:1]):
        relation = edge("shared", owner.id, target.id, "applies_to")
        if scope is not None:
            relation = relation.model_copy(update={"properties": {**relation.properties, "alternative_ids": scope}})
        current = original.model_copy(update={"graph": original.graph.model_copy(update={
            "edges": tuple(e for e in original.graph.edges if not (e.from_node_id == owner.id and e.edge_type == "applies_to")) + (relation,),
        })})
        ctx = IntegrationContext(current, GraphState(graph=GraphSpace()), load_root_schema())
        packet = build_task_context(ctx, current, LocalTask("branches", "node_assess", {owner.id}), before=current)
        view = next(row for row in packet["current_conflict_views"] if row["node_id"] == owner.id)
        membership = {row["alternative_id"]: "shared" in row["active_outgoing_edge_ids"] for row in view["alternatives"]}
        observed.append(membership)
        assert compact_node_packet(packet)["current_conflict_views"] == packet["current_conflict_views"]
    assert observed[0] == observed[1] == {key: True for key in all_ids}
    assert observed[2] == {key: key == all_ids[0] for key in all_ids}


def test_relation_packet_shows_complete_focus_branches_without_mutation():
    from ontology_v2_fixtures import discussion_state
    from cortex.capabilities.graph_merge.reconciliation.context import relation_context
    original = discussion_state()
    owner = next(n for n in original.conflict_nodes if n.node_type == 'generation_rule')
    target = next(n for n in original.graph.nodes if n.node_type == 'concept')
    ids = [a.alternative_id for a in owner.alternatives]
    common = edge('common-operand', owner.id, target.id, 'applies_to')
    scoped = edge('local-operand', owner.id, target.id, 'applies_to').model_copy(update={
        'properties': {'description': 'A branch-local operand.', 'alternative_ids': ids[:1]}})
    current = original.model_copy(update={'graph': original.graph.model_copy(update={
        'edges': original.graph.edges + (common, scoped)})})
    snapshot = current.model_dump_json()
    ctx = IntegrationContext(current, GraphState(graph=GraphSpace()), load_root_schema())
    packet = relation_context(ctx, current, LocalTask('relation', 'reconcile', {owner.id}))
    view = next(v for v in packet['current_conflict_views'] if v['node_id'] == owner.id)
    for row, alternative in zip(view['alternatives'], owner.alternatives):
        full = GraphManager.resolve_alternative(owner, alternative)
        assert row['resolved_content']['properties']['action'] == full.properties['action']
        assert row['resolved_content']['properties']['action']
        assert row['resolved_content'] == full.model_dump(mode='json')
        assert 'common-operand' in row['active_outgoing_edge_ids']
        assert ('local-operand' in row['active_outgoing_edge_ids']) == (alternative.alternative_id == ids[0])
    assert 'common action/reason may be null' in packet['conflict_record_semantics']
    assert current.model_dump_json() == snapshot

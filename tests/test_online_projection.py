"""Session graph projection retains coherent, provenance-bearing candidates."""

import pytest
from ontology_v2_fixtures import formula_edge

from cortex.application.online_context import ChoicePoint, InteractionOption
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.online_context.output.projection import project_graph_state
from cortex.schema import (
    ConflictChoice,
    ConflictPoint,
    GraphNode,
    GraphSpace,
    GraphState,
    NodeContent,
    SourceReference,
    export_inline_graph,
    load_root_schema,
)


def choice_point(alternatives):
    return ChoicePoint(
        choice_point_id="definition",
        decision_question="Which complete definitions apply?",
        options=tuple(
            InteractionOption(
                option_id=alt.alternative_id,
                statement=f"Use {alt.alternative_id}",
                candidate_sources=("memory_graph",),
                graph_refs=(alt.alternative_id,),
            )
            for alt in alternatives
        ),
    )


@pytest.fixture
def state():
    manager = GraphManager(load_root_schema())
    table = GraphNode(
        id="table.orders",
        node_type="table",
        name="orders",
        properties={"table_name": "orders"},
    )
    columns = tuple(
        GraphNode(
            id=f"column.{key}",
            node_type="column",
            name=key,
            properties={"table_ref": table.id, "column_name": key},
        )
        for key in ("a", "b", "c")
    )
    owner = GraphNode(id="metric.revenue", node_type="metric", name="Revenue")
    candidates = [
        (
            key,
            NodeContent(
                name="Revenue",
                description="Revenue definition",
                aliases=(),
                tags=(),
                properties={
                    "pattern": "Shared AB" if key != "c" else "Other C",
                    "expression": {"expr": "SUM(${x})", "refs": {"x": column.id}},
                },
            ),
            (SourceReference(source_id=f"case.{key}"),),
        )
        for key, column in zip(("a", "b", "c"), columns, strict=True)
    ]
    points = (
        ConflictPoint(
            point_id="group",
            aspect="definition",
            description="AB or C",
            choices=(
                ConflictChoice(alternative_ids=("a", "b"), statement="AB"),
                ConflictChoice(alternative_ids=("c",), statement="C"),
            ),
        ),
        ConflictPoint(
            point_id="remaining",
            aspect="column",
            description="A or BC",
            choices=(
                ConflictChoice(alternative_ids=("a",), statement="A"),
                ConflictChoice(alternative_ids=("b", "c"), statement="BC"),
            ),
        ),
    )
    common, conflict = manager.compact_conflict(
        owner, candidates, conflict_points=points
    )
    return manager.prepare_state(
        GraphState(
            graph=GraphSpace(
                nodes=(table, *columns, common),
                edges=tuple(
                    formula_edge(common.id, column.id, key)
                    for key, column in zip(("a", "b", "c"), columns, strict=True)
                ),
            ),
            node_conflicts=(conflict,),
        ),
        strict=True,
    )[0]


def project(state, selected, point=None):
    conflict = state.node_conflicts[0]
    return project_graph_state(
        schema=load_root_schema(),
        state=state,
        selected_node_ids={node.id for node in state.graph.nodes},
        choice_points=(point or choice_point(conflict.alternatives),),
        selected_by_point={} if selected is None else {"definition": selected},
    )


def test_multi_choice_recompacts_common_fields_and_edges(state):
    before = state.model_dump(mode="json")
    projected, trace = project(state, ("a", "b"))
    conflict = projected.node_conflicts[0]
    assert {a.alternative_id for a in conflict.alternatives} == {"a", "b"}
    owner = next(node for node in projected.graph.nodes if node.id == conflict.node_id)
    assert owner.properties["pattern"] == "Shared AB"
    assert all("properties.pattern" not in a.overrides for a in conflict.alternatives)
    assert {s.source_id for s in owner.sources} == {"case.a", "case.b"}
    assert [point.point_id for point in conflict.conflict_points] == ["remaining"]
    assert conflict.conflict_points[0].choices[1].alternative_ids == ("b",)
    edges = [e for e in projected.graph.edges if e.from_node_id == owner.id]
    assert {edge.to_node_id for edge in edges} == {"column.a", "column.b"}
    assert all(
        set(e.properties.get("alternative_ids", ())) <= {"a", "b"} for e in edges
    )
    original = state.node_conflicts[0]
    original_node = next(n for n in state.graph.nodes if n.id == owner.id)
    for alt in conflict.alternatives:
        old = next(
            a for a in original.alternatives if a.alternative_id == alt.alternative_id
        )
        assert GraphManager.resolve_alternative(
            owner, alt
        ) == GraphManager.resolve_alternative(original_node, old)
    assert trace[0].removed_graph_refs == ("c",)
    assert GraphState.model_validate(export_inline_graph(projected)) == projected
    GraphManager(load_root_schema()).validate_state(projected)
    assert state.model_dump(mode="json") == before


def test_single_choice_materializes_and_clears_branch_edge_metadata(state):
    projected, _ = project(state, ("a",))
    assert not projected.node_conflicts
    owner = next(n for n in projected.graph.nodes if n.id == "metric.revenue")
    assert owner.merge_status == "normal"
    assert owner.properties["expression"]["refs"] == {"x": "column.a"}
    assert owner.sources == (SourceReference(source_id="case.a"),)
    assert all("alternative_ids" not in e.properties for e in projected.graph.edges)


def test_unanswered_or_unsafe_mapping_preserves_conflict(state):
    untouched, _ = project(state, None)
    assert untouched.node_conflicts == state.node_conflicts
    partial = choice_point(state.node_conflicts[0].alternatives[:2])
    unsafe, trace = project(state, ("a",), partial)
    assert unsafe.node_conflicts == state.node_conflicts
    assert trace[0].removed_graph_refs == ()


@pytest.mark.parametrize("selection", ["single", "subset", "unanswered"])
def test_current_contract_discussion_graph_projection(selection, state):
    before = state.model_dump(mode="json")
    ids = tuple(a.alternative_id for a in state.node_conflicts[0].alternatives)
    selected = (
        None
        if selection == "unanswered"
        else ids[:1]
        if selection == "single"
        else ids[:2]
    )
    projected, _ = project(state, selected)
    GraphManager(load_root_schema()).validate_state(projected)
    assert GraphState.model_validate(export_inline_graph(projected)) == projected
    if selection == "single":
        assert not projected.node_conflicts
    elif selection == "subset":
        assert len(projected.node_conflicts[0].alternatives) == 2
    else:
        assert projected.node_conflicts == state.node_conflicts
    assert state.model_dump(mode="json") == before


def test_task_guidance_keeps_inline_variants_in_node_details(state):
    from cortex.capabilities.online_context.output.projection import (
        build_downstream_guidance,
    )

    before = state.model_dump(mode="json")
    result = build_downstream_guidance(
        question="Revenue?",
        initial_sql=None,
        graph=state.graph,
        relevant_semantic_node_ids={"metric.revenue"},
        interaction_guidance=(),
        resolved_guidance_content="Use the confirmed definition.",
    )
    assert len(result.ontology_assets) == 1
    asset = result.ontology_assets[0]
    assert asset["node_id"] == "metric.revenue"
    assert asset["status"] == "source_only"
    assert "alternatives" not in asset and "common" not in asset
    assert "overrides" not in result.guidance_content
    assert "Unresolved ontology alternatives" not in result.guidance_content
    assert state.model_dump(mode="json") == before

    resolved, _ = project(state, ("a",))
    result = build_downstream_guidance(
        question="Revenue?",
        initial_sql=None,
        graph=resolved.graph,
        relevant_semantic_node_ids={"metric.revenue"},
        interaction_guidance=(),
        resolved_guidance_content="Use A.",
    )
    assert result.ontology_assets[0]["status"] == "reference"
    assert "column.a" in result.ontology_assets[0]["expression_refs"].values()
    assert "column.b" not in result.guidance_content


def test_task_guidance_does_not_promote_dependencies_to_business_rules(state):
    from cortex.capabilities.online_context.output.projection import (
        build_downstream_guidance,
    )

    # Add a semantic dependency that must remain inspectable, but is not relevant.
    from cortex.schema import GraphEdge

    extra = GraphNode(
        id="concept.extra",
        node_type="concept",
        name="Unrelated customers",
        properties={"targets": [["column.a"]]},
    )
    graph = GraphSpace(
        nodes=(*state.graph.nodes, extra),
        edges=(
            *state.graph.edges,
            GraphEdge(
                id="extra",
                edge_type="depends_on",
                from_node_id="metric.revenue",
                to_node_id=extra.id,
                properties={
                    "description": "This concept is available as a semantic input."
                },
            ),
        ),
    )
    result = build_downstream_guidance(
        question="Revenue?",
        initial_sql=None,
        graph=graph,
        relevant_semantic_node_ids={"metric.revenue"},
        interaction_guidance=(),
        resolved_guidance_content="",
    )
    assert {a["node_id"] for a in result.ontology_assets} == {"metric.revenue"}
    result = build_downstream_guidance(
        question="Revenue?",
        initial_sql=None,
        graph=graph,
        relevant_semantic_node_ids={"metric.revenue"},
        confirmed_graph_refs={extra.id},
        unselected_graph_refs={"metric.revenue"},
        interaction_guidance=(),
        resolved_guidance_content="",
    )
    assert {a["node_id"] for a in result.ontology_assets} == {extra.id}


def test_saved_handoff_and_agent_use_same_node_scoped_guidance(state):
    from copy import deepcopy

    from cortex.capabilities.nl2sql.service import (
        agent_graph,
        public_task,
        task_context,
    )

    graph = state.graph.model_dump(mode="json")
    task = {
        "question": "Revenue?",
        "graphs": {"retrieved": graph, "processed": graph},
        "context": {"guidance": {"guidance_content": "Old alternatives dump"}},
        "context_result": {
            "downstream_guidance": {
                "usage": "reference",
                "guidance_content": "Old alternatives dump",
            },
            "relevant_semantic_node_ids": ["metric.revenue"],
        },
    }
    original = deepcopy(task)
    display = public_task(task)
    handoff = task_context(task)
    assert display["context"] == handoff
    assert "Old alternatives dump" not in handoff["guidance"]["guidance_content"]
    assert handoff["guidance"]["ontology_assets"][0]["status"] == "source_only"
    supplied_graph = agent_graph(task, handoff)
    assert all(not n.get("alternatives") for n in supplied_graph["nodes"])
    assert "metric.revenue" not in {n["id"] for n in supplied_graph["nodes"]}
    assert display["graphs"] == original["graphs"] and task == original


def test_selected_concept_target_filters_explicit_grounding_without_legacy_target_id():
    from test_graph_integration import catalog, edge, state as make_state
    from cortex.capabilities.online_context.output.projection import _target_ref
    node=GraphNode(id='concept.code',node_type='concept',name='classification code',properties={'targets':[['a'],['b']]})
    original=make_state([*catalog(),node],[edge('binding-a',node.id,'a'),edge('binding-b',node.id,'b')])
    point=ChoicePoint(choice_point_id='binding',decision_question='Which representation?',options=tuple(
        InteractionOption(option_id=f'option-{i}',statement=f'Use representation {i}',candidate_sources=('memory_graph',),graph_refs=(_target_ref(node.id,i),)) for i in (1,2)))
    projected,_=project_graph_state(schema=load_root_schema(),state=original,selected_node_ids={node.id},choice_points=(point,),selected_by_point={'binding':('option-2',)})
    retained=next(n for n in projected.graph.nodes if n.id==node.id)
    assert retained.properties['targets']==[['b']]
    bindings=[e for e in projected.graph.edges if e.from_node_id==node.id and e.edge_type=='grounded_on']
    assert [e.id for e in bindings]==['binding-b']
    assert node.properties['targets']==[['a'],['b']]

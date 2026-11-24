from graph_merge_fixtures import aligned_preview
"""Physical identity survives batching, publication, and legacy graph export."""

import importlib.util
from pathlib import Path

import pytest

from cortex.application.retrieval import ColumnSemanticContext
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.operations import remap_node
from cortex.capabilities.graph_merge.graph.publication import (
    commit_result,
)
from cortex.capabilities.graph_merge.graph.edit_contract import (
    IntegrationProposal,
)
from cortex.capabilities.retrieve_db.retrieval.index import RepresentationIndex
from cortex.capabilities.sql_training.merge_evaluation import compose_case_graphs
from cortex.capabilities.sql_training.physical_catalog import (
    audit_physical_ids,
    canonicalize_physical_graph,
)
from cortex.schema import (
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)
from graph_merge_fixtures import catalog, concept, edge, state


def test_composition_shares_physical_nodes_and_maps_each_case():
    first = state(
        [*catalog(), concept("same", "a")], [edge("support", "same", "a")]
    ).graph
    second = state(
        [*catalog(), concept("same", "b")], [edge("support", "same", "b")]
    ).graph
    result = compose_case_graphs((("first", first), ("second", second)), state())
    assert len(result.catalog.nodes) == len(catalog())
    assert len(result.state.graph.nodes) == 2 * len(first.nodes)
    assert "table.objects" in {n.id for n in result.catalog.nodes}
    assert (
        result.mappings["first"]["nodes"]["same"]
        != result.mappings["second"]["nodes"]["same"]
    )
    for source, target in [("first", "a"), ("second", "b")]:
        mapped = result.mappings[source]
        relation = next(
            e for e in result.state.graph.edges if e.id == mapped["edges"]["support"]
        )
        assert relation.from_node_id == mapped["nodes"]["same"]
        assert relation.to_node_id == mapped["nodes"][target]
    audit_physical_ids(GraphState(graph=result.catalog))


def test_empty_base_publishes_canonical_physical_ids():
    graph = state(
        [*catalog(), concept("owner", "a")], [edge("support", "owner", "a")]
    ).graph
    composition = compose_case_graphs((("case", graph),), state())
    context = IntegrationContext(
        GraphState(graph=composition.catalog), composition.state, load_root_schema()
    )
    proposal = IntegrationProposal()
    preview = aligned_preview(context)
    result = commit_result(context, proposal, preview, run_id="physical-ids")
    audit_physical_ids(result.state)
    assert {"table.objects", "column.objects.a"} <= {
        n.id for n in result.state.graph.nodes
    }


def test_graph_renaming_retains_semantic_references_sources_and_literal_text():
    metric = GraphNode(
        id="metric",
        node_type="metric",
        name="count",
        properties={
            "expression": {"expr": "COUNT(${a})", "refs": {"a": "a"}},
        },
    )
    semantic = concept("meaning", "a").model_copy(
        update={
            "description": "Literal incoming.table.example must remain text",
        }
    )
    original = state(
        [*catalog(), semantic, metric],
        [edge("support", "meaning", "a"), edge("formula", "metric", "a")],
    )
    normalized, mapping = canonicalize_physical_graph(original)
    assert (
        next(n for n in normalized.graph.nodes if n.id == "meaning").description
        == semantic.description
    )
    assert (
        next(n for n in normalized.graph.nodes if n.id == "metric").properties[
            "expression"
        ]["refs"]["a"]
        == "column.objects.a"
    )
    restored = normalized.graph.model_copy(
        update={
            "nodes": tuple(
                remap_node(n, {v: k for k, v in mapping.items()})
                for n in normalized.graph.nodes
            ),
            "edges": original.graph.edges,
        }
    )
    assert restored.nodes == original.graph.nodes
    audit_physical_ids(normalized)


def test_conflicting_catalog_facts_and_slug_collisions_fail():
    first = state(catalog()).graph
    changed = first.model_copy(
        update={
            "nodes": tuple(
                n.model_copy(
                    update={"properties": {**n.properties, "data_type": "TEXT"}}
                )
                if n.id == "a"
                else n
                for n in first.nodes
            )
        }
    )
    with pytest.raises(ValueError, match="Conflicting physical"):
        compose_case_graphs((("first", first), ("second", changed)), state())
    collision = GraphState(
        graph=GraphSpace(
            nodes=(
                GraphNode(
                    id="one",
                    node_type="table",
                    name="one",
                    properties={"table_name": "a b"},
                ),
                GraphNode(
                    id="two",
                    node_type="table",
                    name="two",
                    properties={"table_name": "a_b"},
                ),
            )
        )
    )
    with pytest.raises(ValueError, match="collision"):
        canonicalize_physical_graph(collision)


def test_physical_sources_are_combined_without_losing_case_support():
    first = state(catalog()).graph
    second = first.model_copy(
        update={
            "nodes": tuple(
                n.model_copy(update={"sources": (SourceReference(source_id="second"),)})
                for n in first.nodes
            )
        }
    )
    result = compose_case_graphs((("first", first), ("second", second)), state())
    table = next(n for n in result.catalog.nodes if n.id == "table.objects")
    assert {s.source_id for s in table.sources} == {"second"}


def test_legacy_composition_keeps_old_checkpoint_inputs_byte_stable():
    from cortex.capabilities.sql_training.merge_evaluation import _collision_safe_state

    graph = state([*catalog(), concept("meaning", "a")]).graph
    first = _collision_safe_state("one", graph, state())
    second = _collision_safe_state("two", graph, first)
    result = compose_case_graphs((("one", graph), ("two", graph)), state(), legacy=True)
    assert result.state.graph.nodes == (*first.graph.nodes, *second.graph.nodes)
    assert result.state.graph.edges == (*first.graph.edges, *second.graph.edges)


def test_external_column_context_survives_opaque_owner_ids():
    original = state(catalog())
    opaque = GraphState(
        graph=original.graph.model_copy(
            update={
                "nodes": tuple(
                    remap_node(n, {"table": "incoming.table.hash"})
                    for n in original.graph.nodes
                )
            }
        )
    )
    context = ColumnSemanticContext(
        column_id="column.objects.a",
        table_name="objects",
        column_name="a",
        description="Business meaning",
    )
    indexed = RepresentationIndex.build(opaque, column_context=(context,))
    document = next(d for d in indexed.documents if d.document_id == "node:a")
    assert (
        document.structured_signals["column_contexts"][0]["description"]
        == "Business meaning"
    )


def test_export_is_atomic_and_preserves_original(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "canonical_export",
        Path(__file__).parents[1] / "scripts/canonicalize_merge_graph.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "old.json"
    source.write_text(state(catalog()).model_dump_json())
    before = source.read_bytes()
    output = tmp_path / "new"
    module.export_graph(source, output)
    assert source.read_bytes() == before
    audit_physical_ids(
        GraphState.model_validate_json((output / "merged_state.json").read_text())
    )
    with pytest.raises(ValueError, match="new output"):
        module.export_graph(source, output)


def test_shared_columns_combine_metadata_by_source():
    first = state(catalog()).graph

    def annotated(source):
        return first.model_copy(
            update={
                "nodes": tuple(
                    n.model_copy(
                        update={
                            "properties": {
                                **n.properties,
                                "source_metadata": {source: {"description": source}},
                            }
                        }
                    )
                    if n.id == "a"
                    else n
                    for n in first.nodes
                )
            }
        )

    result = compose_case_graphs(
        (("first", annotated("database")), ("second", annotated("external"))), state()
    )
    column = next(n for n in result.catalog.nodes if n.id == "column.objects.a")
    assert set(column.properties["source_metadata"]) == {"database", "external"}


def test_dimension_values_preserve_encoded_identity():
    value = GraphNode(
        id="opaque-value",
        node_type="dimension_value",
        name="value",
        properties={"column_ref": "a", "value": "A.B %/~中"},
    )
    graph = state([*catalog(), value])
    canonical, mapping = canonicalize_physical_graph(graph)
    assert mapping[value.id] == "dimension_value.objects.a.A%2EB%20%25%2F%7E%E4%B8%AD"
    assert (
        next(n for n in canonical.graph.nodes if n.id == mapping[value.id]).properties[
            "column_ref"
        ]
        == "column.objects.a"
    )


def test_shared_physical_edges_deduplicate_and_keep_all_case_mappings():
    from cortex.capabilities.sql_training.merge_evaluation import _physical_subgraph

    graph = state(
        [*catalog(), concept("meaning", "a")], [edge("support", "meaning", "a")]
    ).graph
    result = compose_case_graphs((("one", graph), ("two", graph)), state())
    physical = result.catalog
    assert len(physical.edges) == len(_physical_subgraph(graph).edges)
    for source in ["one", "two"]:
        actual_ids = {e.id for e in result.state.graph.edges}
        assert set(result.mappings[source]["edges"].values()) <= actual_ids
    ctx = IntegrationContext(
        GraphState(graph=physical), result.state, load_root_schema()
    )
    assert set(ctx.physical_map.values()) == {n.id for n in physical.nodes}

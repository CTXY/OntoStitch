"""New writes are column/value matches; legacy reads stay available."""
import pytest
from pydantic import ValidationError
from test_agent_runtime import session_for

from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.semantic_mapping import (
    ConceptAuthoringProperties,
    ConceptProperties,
)


def test_authoring_schema_rejects_table_but_reads_historical_matches():
    assert ConceptProperties(targets=[["table.records"]]).targets == [["table.records"]]
    with pytest.raises(ValidationError):
        ConceptAuthoringProperties(targets=[["table.records", "column.records.id"]])
    assert ConceptAuthoringProperties(targets=[["column.records.organization_id", "column.records.local_id"]]).targets
    assert ConceptAuthoringProperties(targets=[["column.records.id"], ["column.other_records.id"]]).targets


def test_table_target_write_is_atomic_and_does_not_persist(case_graph):
    session = session_for(case_graph)
    revision = session.graph.working_revision
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = edit.invoke({"edit_id": "table_match", "expected_revision": revision,
                          "upserts": [{"id": "concept.record", "node_type": "concept", "name": "record",
                                       "properties": {"targets": [["table.orders"]]}}]})
    assert result["status"] == "rejected"
    assert "CONCEPT_TARGET_KIND" in str(result)
    assert session.graph.working_revision == revision


def test_legacy_concept_cannot_gain_new_table_grounding(case_graph):
    session = session_for(case_graph)
    asset = NodeAsset.model_validate({"id": "concept.legacy_record", "node_type": "concept", "name": "record",
                                      "properties": {"targets": [["table.orders"]]}})
    snap = session.graph.save([*session.graph.working.assets, asset], parent_revision=session.graph.working_revision)
    session.graph.restore(snap.revision, snap.revision)
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = edit.invoke({"edit_id": "table_edge", "expected_revision": snap.revision,
                          "edge_operations": [{"operation": "create_edge", "edge": {
                              "id": "edge.legacy_table", "edge_type": "grounded_on",
                              "from_node_id": asset.id, "to_node_id": "table.orders",
                              "properties": {"description": "The legacy record source."}}}]})
    assert result["status"] == "rejected"
    assert "CONCEPT_TABLE_BINDING" in str(result)
    assert session.graph.working_revision == snap.revision


def test_conflict_alternative_cannot_bypass_authoring_target_scope(case_graph):
    from cortex.schema import GraphNode
    from cortex.schema.graph import NodeContent

    session = session_for(case_graph)
    revision = session.graph.working_revision
    node = GraphNode(id="concept.record", node_type="concept", name="record",
                     properties={"targets": [["column.orders.id"]]})
    versions = [node, node.model_copy(update={"properties": {"targets": [["table.orders"]]}})]
    contents = [NodeContent.model_validate({key: getattr(n, key) for key in NodeContent.model_fields}) for n in versions]
    owner, conflict = session.graph.manager(revision).compact_conflict(
        node, [("column", contents[0], ()), ("table", contents[1], ())])
    proposal = conflict.model_dump(mode="json")
    for alternative in proposal["alternatives"]:
        alternative.pop("sources", None)
    raw = {key: getattr(owner, key) for key in ("id", "node_type", "name", "description", "aliases", "tags", "properties")}
    edit = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = edit.invoke({"edit_id": "table_conflict", "expected_revision": revision,
                          "conflict_operations": [{"operation": "set_conflict", "node": raw, "conflict": proposal}]})
    assert result["status"] == "rejected", result
    assert "CONCEPT_TARGET_KIND" in str(result)
    assert session.graph.working_revision == revision

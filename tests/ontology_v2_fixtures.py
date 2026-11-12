"""Current-contract fixtures with explicitly authored relationships."""

from cortex.application.ontology_graph import GraphManager
from cortex.schema import (
    ConflictChoice,
    ConflictPoint,
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    NodeContent,
    SourceReference,
    load_root_schema,
)


def formula_edge(owner, operand, alternative_id=None):
    return GraphEdge(
        id=f"edge.{owner}.{alternative_id or operand}",
        edge_type="grounded_on",
        from_node_id=owner,
        to_node_id=operand,
        properties={
            "description": "The field supplies the formula operand.",
            **({"alternative_ids": [alternative_id]} if alternative_id else {}),
        },
    )


def discussion_state():
    """Two complete user-counting behaviors, preserving explicit case provenance."""
    manager = GraphManager(load_root_schema())
    table = GraphNode(
        id="table.users",
        node_type="table",
        name="users",
        properties={"table_name": "users"},
    )
    column = GraphNode(
        id="column.users.Id",
        node_type="column",
        name="Id",
        properties={"table_ref": table.id, "column_name": "Id"},
    )
    concept = GraphNode(
        id="concept.user",
        node_type="concept",
        name="User",
        description="A user identified by users.Id.",
        properties={"targets": [[column.id]]},
    )
    owner = GraphNode(
        id="generation_rule.count_users",
        node_type="generation_rule",
        name="Count users",
    )
    contents = []
    for key, action in (
        ("distinct", "Count DISTINCT users.Id so repeated users contribute once."),
        ("rows", "Count users.Id for every qualifying joined row."),
    ):
        contents.append(
            (
                key,
                NodeContent(
                    name=owner.name,
                    description="Reusable user-count behavior.",
                    aliases=(),
                    tags=(),
                    properties={
                        "operation_intents": ["count_aggregation"],
                        "triggers": {"question": {"phrases": ["how many users"]}},
                        "condition": "The request asks how many users have qualifying posts.",
                        "action": action,
                        "reason": "The counted unit determines repeated-user handling.",
                    },
                ),
                (SourceReference(source_id=f"case.{key}"),),
            )
        )
    common, conflict = manager.compact_conflict(
        owner,
        contents,
        conflict_points=(
            ConflictPoint(
                point_id="dp_count_method",
                aspect="counted unit",
                description="Count users once or count qualifying rows.",
                choices=(
                    ConflictChoice(
                        alternative_ids=("distinct",), statement="Unique users"
                    ),
                    ConflictChoice(alternative_ids=("rows",), statement="Every row"),
                ),
            ),
        ),
    )
    edges = (
        GraphEdge(
            id="edge.user_identity",
            edge_type="grounded_on",
            from_node_id=concept.id,
            to_node_id=column.id,
            properties={"description": "users.Id identifies User."},
        ),
        GraphEdge(
            id="edge.rule_user",
            edge_type="applies_to",
            from_node_id=owner.id,
            to_node_id=concept.id,
            properties={"description": "This behavior counts users."},
        ),
    )
    return manager.prepare_state(
        GraphState(
            graph=GraphSpace(nodes=(table, column, concept, common), edges=edges),
            node_conflicts=(conflict,),
        ),
        strict=True,
    )[0]

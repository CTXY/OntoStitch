"""Bounded corrections to one fixed node group; no graph-wide replanning."""

from cortex.capabilities.graph_merge.graph.source_units import PatchContractError


def partition_groups(group, integrations, keep_separate_ids):
    """Validate a complete partition inside one fixed input group.

    Identity membership is a proposal. Each whole input node belongs to one
    integration or remains an explicit singleton; this grants no wider scope.
    """
    from collections import Counter

    members = list(group["member_ids"])
    separate = set(keep_separate_ids)
    if len(separate) != len(keep_separate_ids) or not separate <= set(members):
        raise PatchContractError(
            "invalid_keep_separate_ids",
            owner_id=group["owner_id"],
            path="keep_separate_ids",
            extra=sorted(separate - set(members)),
            required_action="List distinct original group members to retain as standalone nodes.",
        )
    counts = Counter(keep_separate_ids)
    result = []
    for integration in integrations:
        ids = list(integration.member_ids)
        counts.update(ids)
        result.append({"owner_id": integration.owner_id, "member_ids": ids})
    missing = sorted(set(members) - counts.keys())
    extra = sorted(counts.keys() - set(members))
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    if missing or extra or duplicates:
        raise PatchContractError(
            "partition_coverage_mismatch",
            owner_id=group["owner_id"],
            path="integrate_groups",
            missing=missing,
            extra=extra,
            duplicates=duplicates,
            required_action="Cover every original member exactly once across subgroup member_ids and keep_separate_ids; use no outside IDs.",
        )
    for index, part in enumerate(result):
        ids = part["member_ids"]
        if len(ids) < 2:
            raise PatchContractError(
                "partition_group_too_small",
                owner_id=part["owner_id"],
                path=f"integrate_groups[{index}].member_ids",
                required_action="Use at least two members per integration; list standalone nodes in keep_separate_ids.",
            )
        expected = (
            group["owner_id"]
            if group["owner_id"] in ids
            else next(key for key in members if key in ids)
        )
        if part["owner_id"] != expected:
            raise PatchContractError(
                "partition_owner_mismatch",
                owner_id=part["owner_id"],
                path=f"integrate_groups[{index}].owner_id",
                expected_owner_id=expected,
                required_action="Use the original owner in its subgroup; other subgroups use their first member in the original input order. Each integration needs at least two members.",
            )
    return result


def validate_internal_removals(base, group, removals):
    edges = {edge.id: edge for edge in base.graph.edges}
    members = set(group["member_ids"])
    found = set()
    for removal in removals:
        if not removal.reason.strip():
            raise PatchContractError(
                "empty_relation_removal_reason",
                owner_id=group["owner_id"],
                path="relation_removals",
                edge_id=removal.edge_id,
            )
        edge = edges.get(removal.edge_id)
        if (
            removal.edge_id in found
            or edge is None
            or not {edge.from_node_id, edge.to_node_id} <= members
        ):
            raise PatchContractError(
                "node_relation_removal_outside_group",
                owner_id=group["owner_id"],
                path="relation_removals",
                edge_id=removal.edge_id,
                required_action="Remove only distinct original relations whose two endpoints are members of this fixed group.",
            )
        found.add(removal.edge_id)
    return found


def structural_diagnostics(base, candidate, aliases, schema):
    """Recognize schema-forbidden self relations created by this alias map.

    Keep the actual relation in the preview. These diagnostics authorize a
    validation-only filter, never deletion or acceptance of the business graph.
    """
    from cortex.schema import GraphSpace, GraphState, validate_graph_state
    from cortex.schema.relation_contracts import effective_schema

    # The current ontology's irreflexive relation is is_a. Use the same schema
    # validator to establish that the actual self-relation is forbidden.
    old = {edge.id: edge for edge in base.graph.edges}
    result = []
    for edge in candidate.graph.edges:
        original = old.get(edge.id)
        if (
            original is None
            or edge.from_node_id != edge.to_node_id
            or original.from_node_id == original.to_node_id
            or original.from_node_id not in aliases
            or original.to_node_id not in aliases
        ):
            continue
        probe = GraphState(
            graph=GraphSpace(
                nodes=candidate.graph.nodes,
                edges=(edge,),
                relation_contracts=candidate.graph.relation_contracts,
            )
        )
        try:
            validate_graph_state(probe, effective_schema(schema, candidate.graph))
        except ValueError as error:
            if str(error) != f"edge {edge.id!r} requires distinct endpoints":
                continue
            result.append(
                {
                    "code": "alias_created_self_relation",
                    "edge_id": edge.id,
                    "owner_id": edge.from_node_id,
                    "original_from_node_id": original.from_node_id,
                    "original_to_node_id": original.to_node_id,
                    "from_node_id": edge.from_node_id,
                    "to_node_id": edge.to_node_id,
                    "edge_type": edge.edge_type,
                    "required_outcome": "Assess the original meanings and relation. Ask Writer either to remove an erroneous/redundant internal relation with its knowledge disposition, or to keep incorrectly grouped members separate. A self-relation alone does not prove different identity.",
                }
            )
    return result

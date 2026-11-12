"""Explicit test-owned contracts for metric context relationships."""

from cortex.schema.models import EdgeType, FieldDefinition


def metric_context_contract(name="custom_measures"):
    return EdgeType(
        id=name, name=name,
        description="A supported metric-to-field contextual association, distinct from direct formula input.",
        from_node_types=["metric"], to_node_types=["column"],
        direction_semantics="Source metric is associated with the target physical field; no transitivity or operand inference.",
        evidence_requirement="An explicit description and any branch scope must support the association.",
        fields={"settings": FieldDefinition(type="object", required=False)},
    )

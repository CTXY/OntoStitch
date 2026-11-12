"""Readable values retain exact lexical identity and reuse verified old nodes."""

import pytest

from cortex.capabilities.sql_training.builders.dimension_values import (
    build_dimension_value_assets,
)
from cortex.capabilities.sql_training.contracts.references import (
    physical_reference_catalog,
)
from cortex.capabilities.sql_training.core.ids import (
    canonical_value_reference,
    decode_dimension_value,
    make_node_id,
)
from cortex.capabilities.sql_training.graph.values import ValueWrite, value_asset
from cortex.capabilities.sql_training.models import NodeAsset


@pytest.mark.parametrize("value", ["P", "A B", "A_B", "A/B", "A+B", "A.B", "A%20B", "", " ", "~", "中文", "x" * 300, "v_" + "a" * 64])
def test_value_identity_roundtrip_without_loss_or_double_encoding(value):
    identity = make_node_id("dimension_value", "items", "kind", value)
    component = identity.split(".", 3)[3]
    assert decode_dimension_value(component) == value
    assert canonical_value_reference("items", "kind", component) == identity
    assets = build_dimension_value_assets([{"table_name": "items", "column_name": "kind", "value": value}])
    catalog = physical_reference_catalog({"items": ["kind"]}, dimension_values=assets)
    assert catalog.resolve(identity) == identity


def test_simple_values_are_readable_and_distinct_lexemes_stay_distinct():
    assert make_node_id("dimension_value", "schools", "Virtual", "P") == "dimension_value.schools.Virtual.P"
    assert make_node_id("dimension_value", "schools", "County", "San Joaquin") == "dimension_value.schools.County.San%20Joaquin"
    values = ["A B", "A_B", "A+B", "A/B", "A.B", "A%20B", "", " ", "~"]
    assert len({make_node_id("dimension_value", "items", "kind", v) for v in values}) == len(values)


def test_existing_opaque_value_is_reused_and_new_reference_resolves_to_it():
    old_id = "dimension_value.items.kind.v_" + "a" * 64
    old = build_dimension_value_assets([{"table_name": "items", "column_name": "kind", "value": "P"}])[0].model_copy(update={"id": old_id})
    table = NodeAsset.model_validate({"id": "table.items", "node_type": "table", "name": "items", "properties": {"table_name": "items"}})
    column = NodeAsset.model_validate({"id": "column.items.kind", "node_type": "column", "name": "kind", "properties": {"references": ["table.items"], "column_name": "kind"}})
    result = value_asset(ValueWrite(column_id=column.id, value="P", aliases=["partial"]), [table, column, old])
    assert result.id == old_id
    assert "partial" in result.aliases
    catalog = physical_reference_catalog({"items": ["kind"]}, dimension_values=[old])
    assert catalog.resolve("dimension_value.items.kind.P") == old_id
    assert catalog.resolve(old_id) == old_id
    assert canonical_value_reference("items", "kind", old_id.split(".", 3)[3]) == old_id

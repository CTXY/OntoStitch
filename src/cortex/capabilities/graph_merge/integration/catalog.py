"""Exact physical catalogue selection; no SQL execution or semantic guessing."""


def select_catalog_nodes(nodes, table, column_names):
    tables = [
        n
        for n in nodes.values()
        if n.node_type == "table"
        and (n.id == table or n.properties.get("table_name") == table)
    ]
    # An exact ID disambiguates otherwise identical table names.
    if table in nodes and nodes[table].node_type == "table":
        tables = [nodes[table]]
    if len(tables) != 1:
        return [], {
            "error": "Use an exact table name or an unambiguous table ID",
            "matching_table_ids": sorted(n.id for n in tables),
        }
    owner = tables[0]
    if column_names is None:
        return [owner], {"table_id": owner.id}
    if not column_names:
        return [], {"error": "column_names must contain at least one exact column name"}
    requested = set(column_names)
    selected = [
        n
        for n in nodes.values()
        if n.node_type == "column"
        and n.properties.get("table_ref") == owner.id
        and n.properties.get("column_name") in requested
    ]
    return selected, {
        "table_id": owner.id,
        "missing_columns": sorted(
            requested - {n.properties["column_name"] for n in selected}
        ),
    }

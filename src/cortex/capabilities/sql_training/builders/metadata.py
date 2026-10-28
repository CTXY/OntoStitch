"""Attach original column semantics without replacing catalog facts."""

from cortex.infrastructure.online_context.column_context import DatabaseDescriptionLoader


def attach_column_metadata(columns, table_names, description_directory=None):
    external = {}
    if description_directory is not None:
        for item in DatabaseDescriptionLoader().load(description_directory):
            key = (item.table_name, item.column_name)
            if key in external:
                raise ValueError(f"duplicate column metadata: {key}")
            external[key] = {
                name: value for name, value in item.model_dump().items()
                if name in {"business_name", "description", "data_format", "value_description"} and value
            }
    for column in columns:
        metadata = {}
        owner = next(ref for ref in column.properties.references if ref.startswith("table."))
        key = (table_names[owner], column.properties.column_name)
        if key in external:
            metadata[f"external:{key[0]}.csv"] = external[key]
        column.properties.source_metadata = metadata
    return columns


def compact_column_metadata(metadata):
    """Disclose short source meanings; full text stays readable on the node."""
    result = {}
    for source, fields in metadata.items():
        summary = {key: value[:400] for key, value in fields.items() if key in {"business_name", "description"}}
        if summary:
            if any(len(fields[key]) > 400 for key in summary):
                summary["detail"] = "Summary truncated; inspect this Column for full source metadata."
            result[source] = summary
    return result

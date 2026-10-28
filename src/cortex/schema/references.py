"""Declared dependency IDs shared by validation, context and graph assembly."""

from __future__ import annotations


def content_reference_ids(properties: dict) -> set[str]:
    refs = set()
    declared = properties.get("references", [])
    if isinstance(declared, list):
        refs.update(r for r in declared if isinstance(r, str))
    expression = properties.get("expression")
    if isinstance(expression, dict) and isinstance(expression.get("refs"), dict):
        refs.update(r for r in expression["refs"].values() if isinstance(r, str))
    targets = properties.get("targets", [])
    if isinstance(targets, list):
        for target in targets:
            if isinstance(target, dict):
                refs.update(
                    content_reference_ids({"references": target.get("references")})
                )
    return refs


def property_reference_ids(properties: dict) -> set[str]:
    """All declared references, including non-edge retrieval and catalog slots."""
    refs = content_reference_ids(properties)
    for key in (
        "table_ref",
        "column_ref",
        "value_ref",
        "default_time_column",
    ):
        if isinstance(properties.get(key), str):
            refs.add(properties[key])
    for key in (
        "value_refs",
        "primary_keys",
        "foreign_keys",
        "important_columns",
        "available_time_columns",
    ):
        if isinstance(properties.get(key), list):
            refs.update(r for r in properties[key] if isinstance(r, str))
    for group in properties.get("unique_keys", ()):
        if isinstance(group, list):
            refs.update(r for r in group if isinstance(r, str))
    triggers = properties.get("triggers")
    if isinstance(triggers, dict) and isinstance(triggers.get("sql"), dict):
        refs.update(r for r in triggers["sql"].get("columns", ()) if isinstance(r, str))
    return refs


def remap_references(properties: dict, mappings: dict[str, str]) -> dict:
    import copy

    result = copy.deepcopy(properties)
    if isinstance(result.get("properties"), dict):
        result["properties"] = remap_references(result["properties"], mappings)
    for key in (
        "table_ref",
        "column_ref",
        "value_ref",
        "default_time_column",
    ):
        if isinstance(result.get(key), str):
            result[key] = mappings.get(result[key], result[key])
    if isinstance(result.get("references"), list):
        result["references"] = [mappings.get(ref, ref) for ref in result["references"]]
    for key in (
        "value_refs",
        "primary_keys",
        "foreign_keys",
        "important_columns",
        "available_time_columns",
    ):
        if isinstance(result.get(key), list):
            result[key] = [
                mappings.get(ref, ref) if isinstance(ref, str) else ref
                for ref in result[key]
            ]
    if isinstance(result.get("unique_keys"), list):
        result["unique_keys"] = [
            [mappings.get(ref, ref) for ref in group] for group in result["unique_keys"]
        ]
    expression = result.get("expression")
    if isinstance(expression, dict) and isinstance(expression.get("refs"), dict):
        expression["refs"] = {
            key: mappings.get(ref, ref) for key, ref in expression["refs"].items()
        }
    for target in result.get("targets", ()):
        if isinstance(target, dict) and isinstance(target.get("references"), list):
            target["references"] = [
                mappings.get(ref, ref) for ref in target["references"]
            ]
    for pair in result.get("column_pairs", ()):
        if isinstance(pair, dict):
            for key in ("from_column_ref", "to_column_ref"):
                if isinstance(pair.get(key), str):
                    pair[key] = mappings.get(pair[key], pair[key])
    triggers = result.get("triggers")
    if isinstance(triggers, dict) and isinstance(triggers.get("sql"), dict):
        columns = triggers["sql"].get("columns")
        if isinstance(columns, list):
            triggers["sql"]["columns"] = [mappings.get(ref, ref) for ref in columns]
    return result

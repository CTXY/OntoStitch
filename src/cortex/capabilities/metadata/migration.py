"""Read legacy snapshots as flat metadata; never rewrite the stored revision."""

import json
from copy import deepcopy

from .references import column_reference, object_id


def _text(value):
    return json.dumps(value, ensure_ascii=False)


def _grain(value):
    if not isinstance(value, dict):
        return value
    parts = [value.get("description")]
    if value.get("key_columns"):
        parts.append("Identifying columns: " + ", ".join(value["key_columns"]))
    return "\n".join(filter(None, parts)) or None


def _domain(value):
    if not value:
        return []
    parts = []
    if value.get("allowed_values") is not None:
        parts.append("Documented allowed values: " + _text(value["allowed_values"]))
    for field, label in [
        ("code_meanings", "Code"),
        ("special_values", "Special value"),
    ]:
        for item in value.get(field, []):
            text = f"{label} {_text(item['value'])}: {item['meaning']}"
            if item.get("condition"):
                text += f" (when {item['condition']})"
            parts.append(text)
    if value.get("null_meaning"):
        parts.append("NULL: " + value["null_meaning"])
    if value.get("dictionary_relation"):
        parts.append("Dictionary relation: " + value["dictionary_relation"])
    return parts


def _migrate_flat(value):
    if not isinstance(value, dict) or value.get("schema_version") not in {"1.0", "1.1"}:
        return value
    value = deepcopy(value)
    value["schema_version"] = "2.0"
    if "evidence" in value:
        value["sources"] = value.pop("evidence")
    sources = value.setdefault("sources", [])
    tables = value.get("tables", [])
    objects = [value["database"], *tables, *value.get("relations", [])]
    objects.extend(column for table in tables for column in table.get("columns", []))
    for obj in objects:
        refs = list(obj.get("source_refs", []))
        for item in obj.pop("provenance", {}).values():
            refs.extend(item.get("source_refs", []))
        for item in obj.pop("annotations", []):
            refs.extend(item.get("evidence_ids", []))
        obj["source_refs"] = list(dict.fromkeys(refs))
    ddl = {}
    for table in tables:
        definition = table.pop("ddl", None)
        if definition:
            ddl[table["name"]] = definition
        namespace = table.pop("schema_name", None)
        if namespace and namespace != "main":
            table["description"] = "\n".join(
                filter(
                    None, [table.get("description"), f"Original namespace: {namespace}"]
                )
            )
        table["grain"] = _grain(table.get("grain"))
        scope = table.get("scope")
        if isinstance(scope, dict):
            table["scope"] = (
                "\n".join(
                    filter(
                        None,
                        [
                            scope.get("description"),
                            "Temporal organization: " + scope["temporal_mode"]
                            if scope.get("temporal_mode")
                            else None,
                        ],
                    )
                )
                or None
            )
        if table.get("profile"):
            table["profile"].pop("grain_duplicate_groups", None)
        for column in table.get("columns", []):
            representation = column.get("representation")
            if isinstance(representation, dict):
                column["representation"] = (
                    "; ".join(
                        f"{key}: {val}"
                        for key, val in representation.items()
                        if val is not None
                    )
                    or None
                )
            notes = [
                column.get("description"),
                *_domain(column.pop("value_domain", None)),
            ]
            grain = _grain(column.pop("value_grain", None))
            if grain:
                notes.append("Value scope: " + grain)
            column["description"] = "\n".join(filter(None, notes)) or None
    if ddl:
        source = next(
            (
                s
                for s in sources
                if s["kind"] == "database_schema" and isinstance(s.get("content"), dict)
            ),
            None,
        )
        if source is None:
            ref = "legacy-schema"
            used = {s["id"] for s in sources}
            while ref in used:
                ref += "-old"
            source = {
                "id": ref,
                "kind": "database_schema",
                "locator": value["database"]["source"],
                "content": {},
            }
            sources.append(source)
        source["content"].setdefault("ddl", {}).update(ddl)
    return value


def _migrate_names(value):
    value = _migrate_flat(value)
    if not isinstance(value, dict) or value.get("schema_version") != "2.0":
        return value
    value = deepcopy(value)
    tables = value.get("tables", [])
    table_names = {t["id"]: t["name"] for t in tables}
    column_names = {c["id"]: c["name"] for t in tables for c in t.get("columns", [])}
    for table in tables:
        table.pop("id")
        table["primary_keys"] = [column_names[c] for c in table.get("primary_keys", [])]
        table["unique_keys"] = [
            [column_names[c] for c in key] for key in table.get("unique_keys", [])
        ]
        for fk in table.get("foreign_keys", []):
            fk["columns"] = [column_names[c] for c in fk["columns"]]
            fk["referenced_table"] = table_names[fk["referenced_table"]]
            fk["referenced_columns"] = [
                column_names[c] for c in fk["referenced_columns"]
            ]
        for column in table.get("columns", []):
            column.pop("id")
    value["schema_version"] = "3.0"
    return value


def _migrate_foreign_keys(value):
    value = _migrate_names(value)
    if not isinstance(value, dict) or value.get("schema_version") != "3.0":
        return value
    value = deepcopy(value)
    for table in value.get("tables", []):
        for fk in table.get("foreign_keys", []):
            fk.pop("id", None)
    value["schema_version"] = "3.1"
    return value


def _migrate_values(value):
    value = _migrate_foreign_keys(value)
    if not isinstance(value, dict) or value.get("schema_version") != "3.1":
        return value
    value = deepcopy(value)
    for table in value.get("tables", []):
        for column in table.get("columns", []):
            description = column.get("description") or ""
            marker = "\nValue explanation: "
            if column.get("value_description") is None and marker in description:
                column["description"], column["value_description"] = description.split(
                    marker, 1
                )
                column["description"] = column["description"] or None
            elif column.get("value_description") is None and description.startswith(
                "Value explanation: "
            ):
                column["description"] = None
                column["value_description"] = description[len("Value explanation: ") :]
            profile = column.get("profile")
            if profile:
                for field in ("min", "max"):
                    if isinstance(profile.get(field), (str, dict, list)):
                        profile.pop(field)
    value["schema_version"] = "3.2"
    return value


def migrate_snapshot(value):
    value = _migrate_values(value)
    if not isinstance(value, dict) or value.get("schema_version") != "3.2":
        return value
    value = deepcopy(value)
    refs = {
        object_id("column", table["name"], column["name"]): column_reference(
            table["name"], column["name"]
        )
        for table in value.get("tables", [])
        for column in table.get("columns", [])
    }
    for relation in value.get("relations", []):
        for field in ("source_columns", "target_columns"):
            relation[field] = [refs.get(ref, ref) for ref in relation[field]]
    value["schema_version"] = "3.3"
    return value

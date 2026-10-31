"""Explicit normalized JSON interchange loader, without format guessing."""

import json
from pathlib import Path

from cortex.capabilities.metadata.loading import LoaderResult, MetadataLoader
from cortex.capabilities.metadata.management import MetadataChange
from cortex.capabilities.metadata.models import object_id


class NormalizedMetadataLoader(MetadataLoader):
    def extract(self, snapshot, source):
        result = LoaderResult()
        if Path(source.locator).suffix.lower() != ".json":
            result.supported = False
            result.issues.append("Normalized loader expects JSON with an entries list")
            return result
        try:
            data = json.loads(source.content)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise TypeError("Expected an object with an entries list")
        except (ValueError, TypeError) as exc:
            result.issues.append(str(exc))
            return result
        tables = {table.name: table for table in snapshot.tables}
        for number, entry in enumerate(data["entries"], 1):
            try:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("metadata"), dict
                ):
                    raise TypeError("Each entry must contain a metadata object")
                table = tables.get(entry.get("table"))
                if table is None:
                    raise ValueError("Unknown table")
                obj = table
                # Only an omitted column means table-level metadata.
                if "column" in entry:
                    column_name = entry["column"]
                    if not isinstance(column_name, str) or not column_name:
                        raise ValueError("column must be a nonempty exact column name")
                    obj = next(
                        (c for c in table.columns if c.name == column_name), None
                    )
                    if obj is None:
                        raise ValueError("Unknown column")
                changes = [
                    MetadataChange(
                        object_id=object_id("table", table.name)
                        if obj is table
                        else object_id("column", table.name, obj.name),
                        field=field,
                        value=value,
                        source_refs=[source.id],
                    )
                    for field, value in entry["metadata"].items()
                ]
                result.batches.append(changes)
            except (ValueError, TypeError) as exc:
                result.issues.append(f"entry {number}: {exc}")
        return result

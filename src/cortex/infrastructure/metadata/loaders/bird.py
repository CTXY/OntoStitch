"""BIRD loader: deterministic CSV/column resolution, then batched LLM interpretation."""

from pathlib import Path

from cortex.capabilities.metadata.bird_import import (
    BirdColumnText,
    BirdDescriptionInterpreter,
)
from cortex.capabilities.metadata.loading import LoaderResult, MetadataLoader
from cortex.capabilities.metadata.models import BuildOptions, object_id
from cortex.infrastructure.bird_descriptions import parse_bird_descriptions


class BirdMetadataLoader(MetadataLoader):
    def __init__(self, model=None, *, options=None):
        self.model = model
        self.options = options or BuildOptions()

    def extract(self, snapshot, source):
        path = Path(source.locator)
        if path.suffix.lower() != ".csv":
            return LoaderResult(
                supported=False,
                issues=["BIRD loader expects database_description/<table>.csv"],
            )
        table = next((t for t in snapshot.tables if t.name == path.stem), None)
        if table is None:
            return LoaderResult(issues=[f"Unknown BIRD table: {path.stem}"])
        rows, issues = parse_bird_descriptions(source.content)
        columns = {c.name: c for c in table.columns}
        stripped_names = {}
        for column in table.columns:
            stripped_names.setdefault(column.name.strip(), []).append(column)
        records = []
        for row in rows:
            if not row["description"] and not row["value_description"]:
                continue
            name = row["column_name"]
            column = columns.get(name)
            if column is None:
                matches = stripped_names.get(name.strip(), [])
                if len(matches) == 1:
                    column = matches[0]
            if column is None:
                issues.append(
                    f"row {row['line']}: unknown or missing original_column_name {name!r}"
                )
                continue
            records.append(
                BirdColumnText(
                    object_id=object_id("column", table.name, column.name),
                    source_ref=source.id,
                    content={
                        key: value
                        for key, value in {
                            "column_description": row["description"],
                            "value_description": row["value_description"],
                        }.items()
                        if value
                    },
                )
            )
        if not records:
            return LoaderResult(issues=issues)
        if self.model is None:
            raise ValueError(
                "BIRD loader requires a model to interpret nonempty descriptions"
            )
        result = BirdDescriptionInterpreter(self.model).convert(
            snapshot, records, options=self.options
        )
        result.issues = issues + result.issues
        return result

from __future__ import annotations

import re
import unicodedata

import sqlglot
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict
from sqlglot.optimizer.scope import traverse_scope

from cortex.infrastructure.llm import run_llm_extraction
from cortex.schema import GraphSpace


class InitialSqlOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sql: str


class GraphPhysicalSchemaRenderer:
    def render(self, graph: GraphSpace) -> str:
        nodes = {node.id: node for node in graph.nodes}
        columns: dict[str, list[str]] = {}
        for edge in graph.edges:
            if edge.edge_type != "belongs_to":
                continue
            source, target = nodes.get(edge.from_node_id), nodes.get(edge.to_node_id)
            if (
                not source
                or not target
                or source.node_type != "column"
                or target.node_type != "table"
            ):
                continue
            name = str(source.properties.get("column_name") or source.name)
            data_type = str(source.properties.get("data_type") or "")
            business_label = str(source.description or source.name).strip()
            rendered = f"{name} {data_type}".strip()
            if business_label and _normalize_label(business_label) != _normalize_label(name):
                rendered = f"{rendered} [{business_label}]"
            columns.setdefault(target.id, []).append(rendered)
        lines = []
        for table in sorted((n for n in graph.nodes if n.node_type == "table"), key=lambda n: n.id):
            table_name = str(table.properties.get("table_name") or table.name)
            lines.append(f"TABLE {table_name} ({', '.join(sorted(columns.get(table.id, [])))})")
        return "\n".join(lines)


class InitialSqlGenerator:
    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def generate(self, *, question: str, schema_context: str, dialect: str) -> str:
        output = await run_llm_extraction(
            llm=self._llm,
            prompt=(
                "Generate one best-effort baseline SQL statement for retrieval signal analysis. "
                "Use only the supplied physical schema. Do not assume ontology assets. Preserve "
                "each maximal proper-name literal from the Question as one equality value. Choose "
                "the column whose business meaning matches the whole named entity, not a column "
                "for an organization, category, or type suggested only by a suffix inside that "
                "name. Distinguish entity-name columns from categorical type columns. For a "
                "within-group Top-N or superlative request, keep the partition dimension separate "
                "from the entity being ranked: aggregate at the ranked entity's identifier grain "
                "inside each requested group, rather than aggregating only at the group grain."
            ),
            output_model=InitialSqlOutput,
            human_payload={
                "question": question,
                "physical_schema": schema_context,
                "dialect": dialect,
            },
        )
        return expand_table_aliases(output.sql.strip(), dialect=dialect)


def expand_table_aliases(sql: str, *, dialect: str) -> str:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return sql
    for scope in traverse_scope(tree):
        physical_counts: dict[str, int] = {}
        for table in scope.tables:
            physical_counts[table.name] = physical_counts.get(table.name, 0) + 1
        aliases = {
            table.alias: table
            for table in scope.tables
            if table.alias and table.alias != table.name and physical_counts.get(table.name) == 1
        }
        for column in scope.columns:
            table = aliases.get(column.table)
            if table is not None:
                column.set("table", table.this.copy())
        for table in aliases.values():
            table.set("alias", None)
    return tree.sql(dialect=dialect)


def _normalize_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


__all__ = ["GraphPhysicalSchemaRenderer", "InitialSqlGenerator", "expand_table_aliases"]

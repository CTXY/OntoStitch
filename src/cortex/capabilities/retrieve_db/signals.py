from __future__ import annotations

import re
from collections.abc import Iterable

import sqlglot
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict
from sqlglot import expressions as exp

from cortex.application.retrieve_db import (
    EntityMention,
    SqlColumnReference,
    SqlLiteralSignal,
    SqlSignals,
)
from cortex.capabilities.query_signals import (
    GraphPhysicalSchemaRenderer,
    InitialSqlGenerator,
)
from cortex.capabilities.retrieve_db.graph_index import GraphIndex
from cortex.capabilities.retrieve_db.normalization import normalize_text
from cortex.infrastructure.llm import run_llm_extraction

DEFAULT_OPERATIONS = (
    "select",
    "where",
    "group by",
    "order by",
    "limit",
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "lag",
    "lead",
    "rank()",
    "dense_rank()",
    "row_number()",
    "count(distinct",
    "like",
    "=",
    "/",
    "* 100",
    "nullif(",
)

_ROLE_TYPES = {
    "metric": ("metric",),
    "dimension": ("column", "concept"),
    "dimension_value": ("dimension_value", "concept"),
    "time": ("column", "concept"),
    "operator": (),
    "other": (),
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MentionItem(_Model):
    text: str
    role: str = "other"


class MentionOutput(_Model):
    mentions: tuple[MentionItem, ...] = ()


class MentionExtractor:
    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def extract(self, question: str, index: GraphIndex) -> tuple[EntityMention, ...]:
        output = await run_llm_extraction(
            llm=self._llm,
            prompt=(
                "Extract verbatim atomic ontology mentions from the database question. Roles are "
                "metric, dimension, dimension_value, time, operator, or other. Do not infer a "
                "business intent taxonomy and do not invent graph identifiers."
            ),
            output_model=MentionOutput,
            human_payload={"question": question, "available_node_types": index.node_types},
        )
        normalized_question = normalize_text(question)
        values: dict[tuple[str, str], EntityMention] = {}
        for item in output.mentions:
            text = item.text.strip()
            normalized = normalize_text(text)
            role = item.role if item.role in _ROLE_TYPES else "other"
            if not normalized or normalized not in normalized_question:
                continue
            expected = tuple(t for t in _ROLE_TYPES[role] if t in index.node_types)
            key = (normalized, role)
            values[key] = EntityMention(
                mention_id=f"pending-{len(values) + 1}",
                text=text,
                normalized_text=normalized,
                role=role,
                expected_node_types=expected,
            )
        for node_type in ("metric", "concept", "dimension_value", "column"):
            for label, node_ids in index.exact_names_by_type.get(node_type, {}).items():
                if len(label) >= 2 and label in normalized_question:
                    role = _role_for_type(node_type)
                    values.setdefault(
                        (label, role),
                        EntityMention(
                            mention_id=f"pending-{len(values) + 1}",
                            text=index.node_by_id[node_ids[0]].name,
                            normalized_text=label,
                            role=role,
                            expected_node_types=_ROLE_TYPES[role],
                        ),
                    )
            for label, _node_ids in index.exact_aliases_by_type.get(node_type, {}).items():
                if len(label) >= 2 and label in normalized_question:
                    role = _role_for_type(node_type)
                    values.setdefault(
                        (label, role),
                        EntityMention(
                            mention_id=f"pending-{len(values) + 1}",
                            text=label,
                            normalized_text=label,
                            role=role,
                            expected_node_types=_ROLE_TYPES[role],
                        ),
                    )
        return tuple(
            item.model_copy(update={"mention_id": f"mention-{position}"})
            for position, item in enumerate(values.values(), 1)
        )


class SqlSignalExtractor:
    def __init__(self, operations: Iterable[str] = DEFAULT_OPERATIONS) -> None:
        self.operations = tuple(dict.fromkeys(normalize_text(value) for value in operations))

    def extract(
        self,
        sql: str,
        *,
        dialect: str,
        operations: Iterable[str] | None = None,
    ) -> SqlSignals:
        tree = sqlglot.parse_one(sql, read=dialect)
        operation_vocabulary = (
            self.operations
            if operations is None
            else tuple(
                dict.fromkeys(
                    normalize_text(value) for value in operations if normalize_text(value)
                )
            )
        )
        aliases = {
            table.alias: table.name
            for table in tree.find_all(exp.Table)
            if table.alias and table.name
        }
        tables = tuple(sorted({table.name for table in tree.find_all(exp.Table) if table.name}))
        columns = tuple(
            SqlColumnReference(
                table_name=aliases.get(column.table, column.table) or None,
                column_name=column.name,
            )
            for column in tree.find_all(exp.Column)
            if column.name
        )
        literals: list[SqlLiteralSignal] = []
        for predicate in tree.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like):
            column = predicate.left if isinstance(predicate.left, exp.Column) else None
            literal = predicate.right if isinstance(predicate.right, exp.Literal) else None
            if literal is None:
                continue
            literals.append(
                SqlLiteralSignal(
                    value=str(literal.this),
                    table_name=(aliases.get(column.table, column.table) or None)
                    if column
                    else None,
                    column_name=column.name if column else None,
                    operator=predicate.key.lower(),
                )
            )
        return SqlSignals(
            initial_sql=sql,
            operations=tuple(value for value in operation_vocabulary if _matches(sql, value)),
            table_names=tables,
            column_refs=tuple(dict.fromkeys(columns)),
            literals=tuple(literals),
        )


def _matches(sql: str, operation: str) -> bool:
    value = re.sub(r"\s+", " ", sql.strip().lower())
    if operation == "=":
        return re.search(r"(?<![<>!=])=(?!=)", value) is not None
    if operation == "/":
        return "/" in value
    if operation == "* 100":
        return re.search(r"\*\s*100(?:\.0+)?(?![\w.])", value) is not None
    pattern = re.escape(operation).replace(r"\ ", r"\s+")
    if operation[0].isalnum() or operation[0] == "_":
        pattern = rf"(?<![\w]){pattern}"
    if operation[-1].isalnum() or operation[-1] == "_":
        pattern = rf"{pattern}(?![\w])"
    return re.search(pattern, value) is not None


def _role_for_type(node_type: str) -> str:
    return {
        "metric": "metric",
        "concept": "dimension_value",
        "dimension_value": "dimension_value",
        "column": "dimension",
    }[node_type]


__all__ = [
    "DEFAULT_OPERATIONS",
    "GraphPhysicalSchemaRenderer",
    "InitialSqlGenerator",
    "MentionExtractor",
    "SqlSignalExtractor",
]

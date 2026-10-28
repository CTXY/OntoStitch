from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import expressions
from sqlglot.dialects import Dialect

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.models import (
    NodeAsset,
    SemanticMappingTarget,
)

_RELATIVE_TIME_TERM = re.compile(
    r"(?:今年|去年|前年|明年|后年|本年|上年|下年|"
    r"本季度|上季度|下季度|本月|上月|下月|本周|上周|下周|"
    r"今天|今日|昨天|昨日|明天|近期|最近|过去|近来)"
)
_SCHEMA_COLUMN_LINE = re.compile(r"^\s*-\s*([^\n:]+?)\s*:", re.MULTILINE)
_SCHEMA_COLUMN_DETAIL_LINE = re.compile(
    r"^\s*-\s*([^\n:]+?)\s*:\s*([^\n]*)$", re.MULTILINE
)
_NUMERIC_TYPE = re.compile(
    r"\b(?:tinyint|smallint|mediumint|int|integer|bigint|decimal|numeric|real|"
    r"double|float|number|money)\b",
    re.IGNORECASE,
)
_BOOLEAN_TYPE = re.compile(r"\b(?:bool|boolean)\b", re.IGNORECASE)


def relative_time_mentions(value: str) -> tuple[str, ...]:
    """Return deterministic relative-time mentions governed outside Concept assets."""

    return tuple(dict.fromkeys(match.group(0) for match in _RELATIVE_TIME_TERM.finditer(value)))


def _parse_binding(expression: str, dialect: str = "sqlite"):
    tree = sqlglot.parse_one(f"SELECT {expression}", dialect=dialect)
    if not isinstance(tree, expressions.Select) or any(value for key, value in tree.args.items() if key != "expressions"):
        raise ValueError("Concept binding must be an expression, not a query or query clause")
    return tree


def normalize_semantic_mapping_candidate(
    asset: NodeAsset,
    *,
    table_schema: dict[str, str],
    physical_catalog: Any = None,
    dialect: str = "sqlite",
) -> NodeAsset:
    """Qualify unqualified target columns when their physical table is unambiguous."""

    normalized = asset.model_copy(deep=True)
    if physical_catalog is None:
        physical_catalog = _physical_catalog(table_schema)
    physical_names_by_ref = _physical_names_by_ref(table_schema)
    schema_tables_by_column = _schema_tables_by_column(table_schema)
    for target_index, target in enumerate(normalized.properties.targets):
        canonical_references = [
            _canonicalize_physical_ref(reference, physical_catalog=physical_catalog)
            for reference in target.references
        ]
        target_changed = canonical_references != target.references
        target.references = canonical_references
        try:
            tree = _parse_binding(target.sql_expression, dialect)
        except Exception:  # noqa: BLE001
            continue
        reference_tables_by_column = _reference_tables_by_column(target.references)
        for column in tree.find_all(expressions.Column):
            if column.table or not column.name:
                continue
            column_name = str(column.name)
            candidate_tables = reference_tables_by_column.get(column_name.casefold(), set())
            if not candidate_tables:
                candidate_tables = schema_tables_by_column.get(column_name.casefold(), set())
            if len(candidate_tables) != 1:
                continue
            column.set("table", next(iter(candidate_tables)))
        tree, _ = _canonicalize_expression_columns(
            tree, physical_catalog=physical_catalog,
            physical_names_by_ref=physical_names_by_ref,
        )
        if dialect == "sqlite":
            tree, _ = coerce_sqlite_dqs_literals(tree, physical_catalog=physical_catalog)
        # Serialize the same projection AST used by validation. Preserve literal values
        # and identifier spelling; only remove unnecessary identifier quoting.
        keywords = {word for key in Dialect.get_or_raise(dialect).tokenizer_class.KEYWORDS for word in key.split()}
        for identifier in tree.find_all(expressions.Identifier):
            identifier.set("quoted", not bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier.name)) or identifier.name.upper() in keywords)
        target.sql_expression = ", ".join(item.sql(dialect=dialect) for item in tree.expressions)
        expected_references = _expression_references(
            tree,
            physical_catalog=physical_catalog,
            table_schema=table_schema,
        )
        declared_references = set(target.references)
        declared_columns = {
            reference for reference in declared_references if reference.startswith("column.")
        }
        expected_columns = {
            reference for reference in expected_references if reference.startswith("column.")
        }
        if expected_columns.issubset(declared_columns):
            invalid_numeric_values = (
                _all_expression_dimension_value_references(
                    tree,
                    physical_catalog=physical_catalog,
                )
                - expected_references
            )
            if invalid_numeric_values.intersection(target.references):
                target.references = [
                    reference
                    for reference in target.references
                    if reference not in invalid_numeric_values
                ]
                declared_references = set(target.references)
                target_changed = True
            governed_values = {
                reference
                for reference in expected_references
                if reference.startswith("dimension_value.")
            }
            missing_values = governed_values - declared_references
            if missing_values:
                target.references = [*target.references, *sorted(missing_values)]
                target_changed = True
        if target_changed:
            payload = target.model_dump(mode="python")
            normalized.properties.targets[target_index] = SemanticMappingTarget.model_validate(
                payload
            )
    return normalized


def validate_semantic_mapping_candidate_contract(
    asset: NodeAsset,
    *,
    table_schema: dict[str, str] | None = None,
    physical_catalog: Any = None,
) -> None:
    if asset.node_type != NodeType.SEMANTIC_MAPPING:
        raise ValueError("Semantic Mapping contract requires a semantic_mapping asset")
    term = asset.name.strip()
    if not term:
        raise ValueError("Semantic Mapping term must be non-empty")
    if not asset.properties.targets:
        raise ValueError("Semantic Mapping requires at least one target")
    if physical_catalog is None:
        physical_catalog = _physical_catalog(table_schema or {})
    for index, target in enumerate(asset.properties.targets, start=1):
        _validate_target(
            target,
            index=index,
            physical_catalog=physical_catalog,
            table_schema=table_schema or {},
        )


def validate_semantic_mapping_gold_grounding(
    asset: NodeAsset,
    *,
    gold_physical_refs: set[str] | None,
) -> None:
    """Reject bindings absent from physical references prepared from Gold SQL."""

    if gold_physical_refs is None:
        return
    candidate_columns = {
        reference
        for target in asset.properties.targets
        for reference in target.references
        if reference.startswith("column.")
    }
    unsupported = candidate_columns - gold_physical_refs
    if unsupported:
        raise ValueError(
            "Semantic Mapping target Columns are not directly realized in Gold SQL: "
            f"unsupported={sorted(unsupported)}"
        )


def validate_semantic_mapping_schema_grounding(
    asset: NodeAsset,
    *,
    table_schema: dict[str, str],
    physical_catalog: Any = None,
) -> None:
    """Verify candidate column references against the supplied schema snapshot."""

    if not table_schema:
        return
    if physical_catalog is None:
        physical_catalog = _physical_catalog(table_schema)
    unresolved: set[str] = set()
    for target in asset.properties.targets:
        for reference in target.references:
            if not reference.startswith(("column.", "dimension_value.")):
                continue
            if _resolve_physical_ref(reference, physical_catalog) is None:
                unresolved.add(reference)
    if unresolved:
        raise ValueError(
            "Semantic Mapping references are not grounded in the supplied schema: "
            f"unresolved={sorted(unresolved)}"
        )


def _reference_tables_by_column(references: list[str]) -> dict[str, set[str]]:
    tables_by_column: dict[str, set[str]] = {}
    for reference in references:
        if not reference.startswith("column."):
            continue
        parts = reference.split(".")
        if len(parts) != 3:
            continue
        _, table_name, column_name = parts
        tables_by_column.setdefault(column_name.casefold(), set()).add(table_name)
    return tables_by_column


def _schema_tables_by_column(table_schema: dict[str, str]) -> dict[str, set[str]]:
    tables_by_column: dict[str, set[str]] = {}
    for table_name, schema in table_schema.items():
        for column_name in _SCHEMA_COLUMN_LINE.findall(str(schema)):
            tables_by_column.setdefault(column_name.casefold(), set()).add(
                str(table_name)
            )
    return tables_by_column


def _physical_catalog(table_schema: dict[str, str]) -> Any:
    # Imported lazily to avoid a module cycle: references.py imports this contract.
    from cortex.capabilities.sql_training.contracts.references import (
        physical_reference_catalog,
    )

    return physical_reference_catalog(table_schema)


def _physical_names_by_ref(table_schema: dict[str, str]) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for table_name, schema in table_schema.items():
        table = str(table_name)
        for column_name in _SCHEMA_COLUMN_LINE.findall(str(schema)):
            column = str(column_name)
            result[make_node_id(NodeType.COLUMN, table, column)] = (table, column)
    return result


def _resolve_physical_ref(reference: str, physical_catalog: Any) -> str | None:
    resolver = getattr(physical_catalog, "resolve", None)
    if callable(resolver):
        resolved = resolver(reference)
        if isinstance(resolved, str):
            return resolved
    if reference in physical_catalog:
        return reference
    return None


def _canonicalize_physical_ref(reference: str, *, physical_catalog: Any) -> str:
    resolved = _resolve_physical_ref(reference, physical_catalog)
    if resolved is not None:
        return resolved
    if reference.startswith("column."):
        parts = reference.split(".", 2)
        if len(parts) == 3 and all(parts[1:]):
            return make_node_id(NodeType.COLUMN, parts[1], parts[2])
        return reference
    if not reference.startswith("dimension_value."):
        return reference
    parts = reference.split(".", 3)
    if len(parts) != 4 or not all(parts[1:]):
        return reference
    _, table_name, column_name, value = parts
    if re.fullmatch(r"v_[0-9a-f]{64}", value):
        return reference
    return make_node_id(NodeType.DIMENSION_VALUE, table_name, column_name, value)


def _validate_target(
    target: Any,
    *,
    index: int,
    physical_catalog: Any,
    table_schema: dict[str, str],
) -> None:
    expression = str(target.sql_expression or "").strip()
    if not expression:
        raise ValueError(f"Semantic Mapping target {index} sql_expression must be non-empty")
    try:
        # ``sql_expression`` is the executable physical binding of the Concept term, not
        # a query filter: a composite term (for example a full address) binds several
        # qualified columns in one target. Parse it as a projection list so every
        # column is captured; framing it as ``SELECT 1 WHERE <expr>`` silently drops
        # every column after the first in a comma-separated binding.
        tree = _parse_binding(expression)
    except Exception as exc:
        raise ValueError(
            f"Semantic Mapping target {index} sql_expression is invalid: {exc}"
        ) from exc
    tree, _ = coerce_sqlite_dqs_literals(tree, physical_catalog=physical_catalog)
    forbidden = (
        expressions.AggFunc,
        expressions.Window,
        expressions.Subquery,
        expressions.Order,
        expressions.Limit,
        expressions.CTE,
    )
    if any(tree.find(node_type) is not None for node_type in forbidden):
        raise ValueError(
            f"Semantic Mapping target {index} contains query or calculation operations"
        )
    columns = list(tree.find_all(expressions.Column))
    unqualified = sorted({column.name for column in columns if not column.table})
    if unqualified:
        raise ValueError(
            f"Semantic Mapping target {index} contains unqualified columns: {unqualified}"
        )
    expected_references = _expression_references(
        tree,
        physical_catalog=physical_catalog,
        table_schema=table_schema,
    )

    actual_references = {
        _canonicalize_physical_ref(reference, physical_catalog=physical_catalog)
        for reference in target.references
        if reference.startswith(("column.", "dimension_value."))
    }
    if actual_references != expected_references:
        raise ValueError(
            f"Semantic Mapping target {index} references do not match its physical contract: "
            f"missing={sorted(expected_references-actual_references)}, "
            f"extra={sorted(actual_references-expected_references)}"
        )


def coerce_sqlite_dqs_literals(
    tree: expressions.Expression,
    *,
    physical_catalog: Any,
) -> tuple[expressions.Expression, bool]:
    """Apply SQLite's double-quoted string fallback where schema resolution proves it."""

    changed = False

    def transform(node: expressions.Expression) -> expressions.Expression:
        nonlocal changed
        if not _is_sqlite_dqs_value_position(node, physical_catalog=physical_catalog):
            return node
        changed = True
        return expressions.Literal.string(str(node.name))

    return tree.transform(transform, copy=True), changed


def _canonicalize_expression_columns(
    tree: expressions.Expression,
    *,
    physical_catalog: Any,
    physical_names_by_ref: dict[str, tuple[str, str]],
) -> tuple[expressions.Expression, bool]:
    changed = False
    normalized = tree.copy()
    for column in normalized.find_all(expressions.Column):
        reference = _column_reference(column, physical_catalog=physical_catalog)
        if reference is None:
            continue
        table_name, column_name = physical_names_by_ref.get(
            reference,
            reference.split(".", 2)[1:],
        )
        if str(column.table) == table_name and str(column.name) == column_name:
            continue
        column.set("table", expressions.to_identifier(table_name))
        column.set("this", expressions.to_identifier(column_name))
        changed = True
    return normalized, changed


def _is_sqlite_dqs_value_position(
    node: expressions.Expression,
    *,
    physical_catalog: Any,
) -> bool:
    if not isinstance(node, expressions.Column) or not _is_quoted_unqualified_column(node):
        return False
    parent = node.parent
    if isinstance(
        parent,
        (
            expressions.EQ,
            expressions.NEQ,
            expressions.GT,
            expressions.GTE,
            expressions.LT,
            expressions.LTE,
            expressions.Like,
            expressions.ILike,
        ),
    ):
        opposite = parent.left if parent.right is node else parent.right
        return _is_resolved_column(opposite, physical_catalog=physical_catalog)
    if isinstance(parent, expressions.In) and node in parent.expressions:
        return _is_resolved_column(parent.this, physical_catalog=physical_catalog)
    return False


def _is_quoted_unqualified_column(column: expressions.Column) -> bool:
    identifier = column.this
    return (
        not column.table
        and bool(column.name)
        and isinstance(identifier, expressions.Identifier)
        and bool(identifier.args.get("quoted"))
    )


def _is_resolved_column(node: Any, *, physical_catalog: Any) -> bool:
    return isinstance(node, expressions.Column) and _column_reference(
        node,
        physical_catalog=physical_catalog,
    ) is not None


def _column_reference(
    column: expressions.Column,
    *,
    physical_catalog: Any,
) -> str | None:
    if not column.table or not column.name:
        return None
    reference = make_node_id(NodeType.COLUMN, column.table, column.name)
    return _resolve_physical_ref(reference, physical_catalog) or reference


def _expression_references(
    tree: expressions.Expression,
    *,
    physical_catalog: Any,
    table_schema: dict[str, str],
) -> set[str]:
    expected_references = {
        reference
        for column in tree.find_all(expressions.Column)
        if (reference := _column_reference(column, physical_catalog=physical_catalog))
        is not None
    }
    for predicate in tree.find_all(expressions.EQ):
        column_ref, literal = _column_literal_pair(
            predicate,
            physical_catalog=physical_catalog,
        )
        if (
            column_ref is not None
            and literal is not None
            and _allows_dimension_value(column_ref, predicate.left, predicate.right, table_schema)
        ):
            _, table_name, column_name = column_ref.split(".", 2)
            expected_references.add(
                make_node_id(
                    NodeType.DIMENSION_VALUE,
                    table_name,
                    column_name,
                    literal,
                )
            )
    for predicate in tree.find_all(expressions.In):
        column_ref, literals = _in_column_literals(
            predicate,
            physical_catalog=physical_catalog,
        )
        if column_ref is not None and all(
            _allows_dimension_value(column_ref, predicate.this, literal, table_schema)
            for literal in predicate.expressions
        ):
            _, table_name, column_name = column_ref.split(".", 2)
            expected_references.update(
                make_node_id(
                    NodeType.DIMENSION_VALUE,
                    table_name,
                    column_name,
                    literal,
                )
                for literal in literals
            )
    for predicate in tree.find_all(expressions.Like):
        column_ref, literal = _exact_like_column_literal(
            predicate,
            physical_catalog=physical_catalog,
        )
        if column_ref is not None and literal is not None:
            _, table_name, column_name = column_ref.split(".", 2)
            expected_references.add(
                make_node_id(
                    NodeType.DIMENSION_VALUE,
                    table_name,
                    column_name,
                    literal,
                )
            )
    expected_references = {
        _canonicalize_physical_ref(reference, physical_catalog=physical_catalog)
        for reference in expected_references
    }
    # Numeric auto-discovery is conservative; an explicitly governed, verified
    # stored Value is already evidence and must not be stripped by that heuristic.
    expected_references.update(
        _all_expression_dimension_value_references(tree, physical_catalog=physical_catalog)
        & getattr(physical_catalog, "verified_value_ids", frozenset())
    )
    return expected_references


def _all_expression_dimension_value_references(
    tree: expressions.Expression,
    *,
    physical_catalog: Any,
) -> set[str]:
    """Return literal-derived refs without applying the categorical-value boundary."""

    references: set[str] = set()
    for predicate in tree.find_all(expressions.EQ):
        column_ref, literal = _column_literal_pair(
            predicate, physical_catalog=physical_catalog
        )
        if column_ref is not None and literal is not None:
            _, table_name, column_name = column_ref.split(".", 2)
            references.add(
                make_node_id(NodeType.DIMENSION_VALUE, table_name, column_name, literal)
            )
    for predicate in tree.find_all(expressions.In):
        column_ref, literals = _in_column_literals(
            predicate, physical_catalog=physical_catalog
        )
        if column_ref is not None:
            _, table_name, column_name = column_ref.split(".", 2)
            references.update(
                make_node_id(NodeType.DIMENSION_VALUE, table_name, column_name, literal)
                for literal in literals
            )
    return {_canonicalize_physical_ref(reference, physical_catalog=physical_catalog) for reference in references}


def column_allows_numeric_dimension_values(
    column_ref: str,
    *,
    table_schema: dict[str, str],
) -> bool:
    """Whether a numeric storage column clearly represents a flag or category code."""

    parts = column_ref.split(".", 2)
    if len(parts) != 3:
        return False
    _, table_name, column_name = parts
    detail = ""
    for name, description in _SCHEMA_COLUMN_DETAIL_LINE.findall(
        str(table_schema.get(table_name, ""))
    ):
        if name.casefold() == column_name.casefold():
            detail = description
            break
    if _BOOLEAN_TYPE.search(detail):
        return True
    tokens = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", column_name)
    folded = [token.casefold() for token in tokens]
    compact = column_name.casefold()
    if folded and folded[0] in {"is", "has", "can", "should", "was", "were"}:
        return True
    if any(token in {"flag", "indicator", "boolean", "bool"} for token in folded):
        return True
    return compact.endswith(("_code", "statuscode", "typecode", "categorycode"))


def column_is_numeric(
    column_ref: str,
    *,
    table_schema: dict[str, str],
) -> bool:
    parts = column_ref.split(".", 2)
    if len(parts) != 3:
        return False
    _, table_name, column_name = parts
    for name, description in _SCHEMA_COLUMN_DETAIL_LINE.findall(
        str(table_schema.get(table_name, ""))
    ):
        if name.casefold() == column_name.casefold():
            return _NUMERIC_TYPE.search(description) is not None
    return False


def _allows_dimension_value(
    column_ref: str,
    left: expressions.Expression,
    right: expressions.Expression,
    table_schema: dict[str, str],
) -> bool:
    literal = right if isinstance(right, expressions.Literal) else left
    if not isinstance(literal, expressions.Literal) or literal.is_string:
        return True
    if not column_is_numeric(column_ref, table_schema=table_schema):
        return True
    return column_allows_numeric_dimension_values(
        column_ref,
        table_schema=table_schema,
    )


def _column_literal_pair(
    predicate: expressions.EQ,
    *,
    physical_catalog: Any,
) -> tuple[str | None, str | None]:
    left: Any = predicate.left
    right: Any = predicate.right
    if isinstance(left, expressions.Column) and isinstance(right, expressions.Literal):
        return _column_reference(left, physical_catalog=physical_catalog), str(right.this)
    if isinstance(right, expressions.Column) and isinstance(left, expressions.Literal):
        return _column_reference(right, physical_catalog=physical_catalog), str(left.this)
    return None, None


def _in_column_literals(
    predicate: expressions.In,
    *,
    physical_catalog: Any,
) -> tuple[str | None, tuple[str, ...]]:
    column_ref = (
        _column_reference(predicate.this, physical_catalog=physical_catalog)
        if isinstance(predicate.this, expressions.Column)
        else None
    )
    if column_ref is None or not predicate.expressions:
        return None, ()
    literals = tuple(
        str(item.this)
        for item in predicate.expressions
        if isinstance(item, expressions.Literal)
    )
    if len(literals) != len(predicate.expressions):
        return None, ()
    return column_ref, literals


def _exact_like_column_literal(
    predicate: expressions.Like,
    *,
    physical_catalog: Any,
) -> tuple[str | None, str | None]:
    if isinstance(predicate.parent, expressions.Not):
        return None, None
    left: Any = predicate.left
    right: Any = predicate.right
    if not isinstance(left, expressions.Column) or not isinstance(right, expressions.Literal):
        return None, None
    literal = str(right.this)
    if "%" in literal or "_" in literal:
        return None, None
    return _column_reference(left, physical_catalog=physical_catalog), literal

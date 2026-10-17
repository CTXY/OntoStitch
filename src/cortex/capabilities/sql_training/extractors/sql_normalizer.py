"""SQL normalization utilities for extraction preprocessing.

Normalizes SQL by removing table aliases and expanding CTE/subquery alias
references so that every column reference resolves to real table.column format.

When an optional ``column_table_map`` is provided, unqualified column references
(bare column names without a table prefix) are resolved against the actual
database schema so that every column is prefixed with its real table name.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import expressions

from cortex.capabilities.sql_training.core.enums import NodeType


def _preprocess_sql_for_parsing(sql: str) -> str:
    """Apply light SQL cleanup to improve parser robustness."""
    like_single_quote_pattern = r"\bLIKE\s+'([^']*)'"
    sql = re.sub(
        like_single_quote_pattern,
        lambda match: f"LIKE '{match.group(1)}'",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _collect_table_names(ast: expressions.Expression) -> set[str]:
    """Collect all real table names referenced in the AST (excluding CTE/subquery aliases)."""
    cte_names: set[str] = set()
    for node in ast.walk():
        if isinstance(node, expressions.CTE) and node.alias:
            cte_names.add(str(node.alias).lower())

    subquery_names: set[str] = set()
    for node in ast.walk():
        if isinstance(node, expressions.Subquery) and node.alias:
            subquery_names.add(str(node.alias).lower())

    tables: set[str] = set()
    for node in ast.walk():
        if isinstance(node, expressions.Table) and node.name:
            name = str(node.name).lower()
            if name not in cte_names and name not in subquery_names:
                tables.add(name)

    return tables


def _is_passthrough_column(expr: expressions.Expression) -> str | None:
    """Check if a SELECT expression is a simple passthrough column reference.

    Returns the referenced column name if it IS a passthrough (just a bare
    column or column with no expression wrapping), or None if it's derived.
    """
    if isinstance(expr, expressions.Column):
        # e.g., SELECT col_name  (bare column, no alias)
        return str(expr.name)
    if isinstance(expr, expressions.Alias):
        inner = expr.this
        if isinstance(inner, expressions.Column):
            # e.g., SELECT col_name AS alias
            return str(inner.name)
    return None


def _collect_cte_column_sources(
    ast: expressions.Expression,
) -> dict[str, dict[str, str | None]]:
    """Analyze each CTE to map its output columns to their real source tables.

    Returns:
      {cte_name_lower: {output_col_name: real_table_or_None}}

    - ``real_table``: the column is a simple passthrough and originates from
      this single real database table.
    - ``None``: the column is a derived/computed expression (SUM, CASE, etc.)
      that does not correspond to a real database column.
    """
    real_tables = _collect_table_names(ast)

    # Gather CTE definitions
    cte_defs: dict[str, expressions.CTE] = {}
    cte_deps: dict[str, set[str]] = {}  # cte_name → other ctes it references

    for node in ast.walk():
        if isinstance(node, expressions.CTE) and node.alias:
            name = str(node.alias).lower()
            cte_defs[name] = node
            deps: set[str] = set()
            for inner in node.walk():
                if isinstance(inner, expressions.Table):
                    tname = str(inner.name).lower()
                    if tname not in real_tables:
                        deps.add(tname)
            cte_deps[name] = deps

    # Topological sort: dependencies first
    sorted_names: list[str] = []
    visited: set[str] = set()

    def _visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for dep in cte_deps.get(name, set()):
            if dep in cte_defs and dep not in visited:
                _visit(dep)
        sorted_names.append(name)

    for name in cte_defs:
        if name not in visited:
            _visit(name)

    # Transitively compute column sources
    cte_col_sources: dict[str, dict[str, str | None]] = {}

    for cte_name in sorted_names:
        node = cte_defs[cte_name]

        # Collect FROM sources: what does each alias/name in the FROM clause
        # map to in terms of real tables?
        from_sources: list[tuple[str, str | None]] = []
        # Each entry: (identifier, single_real_table_or_None)
        # identifier is the alias (or table/cte name if no alias)
        # single_real_table is the real table if unambiguous, None if ambiguous

        for from_node in node.walk():
            if isinstance(from_node, expressions.From):
                this = from_node.this
                if isinstance(this, expressions.Table):
                    table_name = str(this.name).lower()
                    alias = str(this.alias) if this.alias else table_name
                    if table_name in real_tables:
                        from_sources.append((alias, table_name))
                    elif table_name in cte_col_sources:
                        # CTE reference: merge the CTE's column sources
                        # For passthrough columns, we need single-table source.
                        # Check if all passthrough columns come from same table
                        sources = {v for v in cte_col_sources[table_name].values() if v is not None}
                        single = sources.pop() if len(sources) == 1 else None
                        from_sources.append((alias, single))
                    else:
                        from_sources.append((alias, None))
                elif isinstance(this, expressions.Subquery):
                    alias = str(this.alias) if this.alias else ""
                    if alias:
                        from_sources.append((alias, None))

        # Also handle JOINs
        for join_node in node.walk():
            if isinstance(join_node, expressions.Join):
                this = join_node.this
                if isinstance(this, expressions.Table):
                    table_name = str(this.name).lower()
                    alias = str(this.alias) if this.alias else table_name
                    if table_name in real_tables:
                        from_sources.append((alias, table_name))
                    elif table_name in cte_col_sources:
                        sources = {v for v in cte_col_sources[table_name].values() if v is not None}
                        single = sources.pop() if len(sources) == 1 else None
                        from_sources.append((alias, single))
                    else:
                        from_sources.append((alias, None))
                elif isinstance(this, expressions.Subquery):
                    alias = str(this.alias) if this.alias else ""
                    if alias:
                        from_sources.append((alias, None))

        # Deduplicate by alias
        seen_aliases: dict[str, str | None] = {}
        for alias, single_table in from_sources:
            if alias in seen_aliases:
                # Multiple sources for same alias → ambiguous
                seen_aliases[alias] = None
            else:
                seen_aliases[alias] = single_table
        from_sources = list(seen_aliases.items())

        select = node.find(expressions.Select)
        if select is None:
            cte_col_sources[cte_name] = {}
            continue

        col_sources: dict[str, str | None] = {}
        for expr in select.expressions:
            output_name: str | None = None

            if isinstance(expr, expressions.Alias):
                output_name = str(expr.alias)
                if isinstance(expr.this, expressions.Column):
                    col_obj = expr.this
                    col_name = str(col_obj.name)
                    # Check if the column has a table prefix that points to a FROM source
                    if col_obj.table:
                        table_ref = str(col_obj.table)
                        for src_alias, src_table in from_sources:
                            if src_alias == table_ref:
                                col_sources[output_name] = src_table
                                break
                        else:
                            col_sources[output_name] = None
                    else:
                        # Bare column in SELECT: find source table
                        col_sources[output_name] = _find_column_source(
                            col_name,
                            from_sources,
                            cte_col_sources,
                            real_tables,
                        )
                else:
                    # Derived expression AS alias
                    col_sources[output_name] = None
            elif isinstance(expr, expressions.Column):
                output_name = str(expr.name)
                col_obj = expr
                col_name = str(col_obj.name)
                if col_obj.table:
                    table_ref = str(col_obj.table)
                    for src_alias, src_table in from_sources:
                        if src_alias == table_ref:
                            col_sources[output_name] = src_table
                            break
                    else:
                        col_sources[output_name] = None
                else:
                    col_sources[output_name] = _find_column_source(
                        col_name,
                        from_sources,
                        cte_col_sources,
                        real_tables,
                    )
            elif isinstance(expr, expressions.Star):
                # SELECT * → we can't analyze columns, skip
                pass

        cte_col_sources[cte_name] = col_sources

    return cte_col_sources


def _find_column_source(
    col_name: str,
    from_sources: list[tuple[str, str | None]],
    cte_col_sources: dict[str, dict[str, str | None]],
    real_tables: set[str],
) -> str | None:
    """Find which real table a bare column name originates from.

    Looks at the FROM clause sources. If the column can be traced to a
    single real table, returns that table. Returns None for derived or
    ambiguous columns.
    """
    candidates: set[str] = set()

    for src_alias, src_single_table in from_sources:
        if src_single_table is not None and src_single_table in real_tables:
            # Direct real table source: all columns are candidates
            candidates.add(src_single_table)
        elif src_single_table is not None:
            # Nested CTE with single table source
            candidates.add(src_single_table)
        elif src_alias in cte_col_sources:
            # CTE source: look up the column
            cte_sources = cte_col_sources[src_alias]
            src = cte_sources.get(col_name)
            if src is not None:
                candidates.add(src)

    # If all candidates agree on a single table, return it
    if len(candidates) == 1:
        return candidates.pop()
    return None


def _collect_alias_mapping(
    ast: expressions.Expression,
    cte_col_sources: dict[str, dict[str, str | None]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build alias mappings.

    Returns:
    - alias_to_table: direct real-table alias → real table name
    - alias_to_cte: CTE/subquery alias → CTE name (lowercase)
    """
    real_tables = _collect_table_names(ast)

    alias_to_table: dict[str, str] = {}
    alias_to_cte: dict[str, str] = {}

    for node in ast.walk():
        if isinstance(node, expressions.Table):
            table_name = str(node.name)
            if node.alias:
                alias = str(node.alias)
                if table_name.lower() in real_tables:
                    alias_to_table[alias.casefold()] = table_name
                elif table_name.lower() in cte_col_sources:
                    alias_to_cte[alias.casefold()] = table_name.lower()
                else:
                    # Subquery or unknown
                    alias_to_cte[alias.casefold()] = table_name.lower()

        if isinstance(node, expressions.Subquery) and node.alias:
            subq_alias = str(node.alias)
            alias_to_cte[subq_alias.casefold()] = subq_alias.lower()

    return alias_to_table, alias_to_cte


def normalize_sql_aliases(
    sql: str,
    *,
    column_table_map: dict[str, str] | None = None,
    dialect: str = "sqlite",
) -> str:
    """Remove table aliases and expand aliased column references.

    For every table alias (e.g., ``FROM logicalview AS t``), the alias is
    stripped and every column reference that used the alias is rewritten to
    use the real table name.

    CTE and subquery aliases are also resolved: passthrough columns
    (columns that are just column references without computation in the CTE
    body) get the real table prefix, while derived/computed columns
    (aggregates, expressions) are left unchanged.

    When ``column_table_map`` is provided, unqualified column references
    (bare column names without a table prefix) are resolved against the
    actual database schema. The map keys are column names and values are
    the corresponding real table names. Only columns that belong to a
    single table are resolved; ambiguous columns (present in multiple
    tables) are left as-is.

    Returns the original ``sql`` string when parsing fails.
    """
    if not sql:
        return sql or ""

    try:
        preprocessed = _preprocess_sql_for_parsing(sql)
        ast = sqlglot.parse_one(
            preprocessed,
            dialect=dialect,
            error_level=sqlglot.ErrorLevel.IGNORE,
        )
        if not ast:
            return sql

        cte_col_sources = _collect_cte_column_sources(ast)
        alias_to_table, alias_to_cte = _collect_alias_mapping(ast, cte_col_sources)

        # Resolve column table references
        for node in ast.walk():
            if not isinstance(node, expressions.Column) or not node.table:
                continue

            table_ref = str(node.table)
            table_ref_key = table_ref.casefold()
            col_name = str(node.name) if node.name else ""

            # 1. Direct real-table alias → real table
            if table_ref_key in alias_to_table:
                node.set("table", alias_to_table[table_ref_key])
                continue

            # 2. CTE alias → look up the column source
            cte_name = alias_to_cte.get(table_ref_key)
            if cte_name is not None and cte_name in cte_col_sources:
                col_map = cte_col_sources[cte_name]
                if col_name in col_map:
                    real_table = col_map[col_name]
                    if real_table is not None:
                        # Passthrough column → prefix with real table
                        node.set("table", real_table)
                    # else: derived column → keep original prefix
                # else: column not in CTE output → keep as-is
                continue

            # 3. CTE/subquery name itself (not as alias)
            #    e.g., FROM sales_data WHERE sales_data.col
            #    Treat like CTE alias
            if table_ref.lower() in cte_col_sources:
                col_map = cte_col_sources[table_ref.lower()]
                if col_name in col_map:
                    real_table = col_map[col_name]
                    if real_table is not None:
                        node.set("table", real_table)
                    # else: derived column → keep original prefix
                continue

        # Strip table aliases on real tables
        for node in ast.walk():
            if isinstance(node, expressions.Table) and node.alias:
                node.set("alias", None)

        # Strip subquery aliases
        for node in ast.walk():
            if isinstance(node, expressions.Subquery) and node.alias:
                node.set("alias", None)

        # Resolve unqualified columns using the actual database schema
        if column_table_map:
            sql_tables = _collect_table_names(ast)
            for node in ast.walk():
                if isinstance(node, expressions.Column) and node.name and not node.table:
                    col_name = str(node.name)
                    table_name = column_table_map.get(col_name)
                    if table_name is None:
                        # Try case-insensitive lookup
                        lower_map = {k.lower(): v for k, v in column_table_map.items()}
                        table_name = lower_map.get(col_name.lower())
                    if table_name is not None:
                        # Only resolve if the table is actually referenced in this SQL
                        if table_name.lower() in sql_tables or not sql_tables:
                            node.set("table", table_name)

        return ast.sql(dialect=dialect)
    except Exception:
        return sql


def build_column_table_map(
    columns: list[dict[str, str]],
) -> dict[str, str]:
    """Build a column-name → table-name mapping from database introspection.

    Only columns that unambiguously belong to a single table are included.
    If a column name appears in multiple tables it is skipped.
    """
    if not columns:
        return {}

    # Count how many tables each column name appears in
    column_tables: dict[str, list[str]] = {}
    for col in columns:
        col_name = col.get("column_name") or col.get("name") or ""
        table_name = col.get("table_name") or col.get("table") or ""
        if not col_name or not table_name:
            continue
        column_tables.setdefault(col_name, []).append(table_name)

    # Only keep unambiguous columns
    return {col_name: tables[0] for col_name, tables in column_tables.items() if len(tables) == 1}


def collect_physical_column_references(
    sql: str,
    *,
    available_column_refs: set[str],
    dialect: str = "sqlite",
) -> set[str]:
    """Resolve Gold SQL columns once at the extraction-input boundary.

    Qualified columns are checked directly against the catalog. An unqualified
    column is accepted only when exactly one physical table in its nearest
    SELECT scope owns that column. Derived and unresolved columns are omitted.
    """

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Failed to parse normalized Gold SQL: {exc}") from exc
    if tree is None:
        raise ValueError("Failed to parse normalized Gold SQL")

    refs_by_qualified_name: dict[tuple[str, str], str] = {}
    refs_by_normalized_qualified_name: dict[tuple[str, str], str] = {}
    for reference in available_column_refs:
        parts = reference.split(".")
        if len(parts) < 3 or parts[0] != NodeType.COLUMN.value:
            continue
        table_name = ".".join(parts[1:-1])
        column_name = parts[-1]
        refs_by_qualified_name[(table_name.casefold(), column_name.casefold())] = reference
        refs_by_normalized_qualified_name[
            (table_name.casefold(), _physical_column_match_key(column_name))
        ] = reference

    references: set[str] = set()
    for column in tree.find_all(expressions.Column):
        if not column.name or isinstance(column.this, expressions.Star):
            continue
        if column.table:
            reference = refs_by_qualified_name.get(
                (str(column.table).casefold(), str(column.name).casefold())
            ) or refs_by_normalized_qualified_name.get(
                (str(column.table).casefold(), _physical_column_match_key(str(column.name)))
            )
            if reference in available_column_refs:
                references.add(reference)
            continue

        select = column.find_ancestor(expressions.Select)
        if select is None:
            continue
        candidates = {
            reference
            for table in _scope_table_sources(select)
            if (
                reference := refs_by_qualified_name.get(
                    (str(table.name).casefold(), str(column.name).casefold())
                )
                or refs_by_normalized_qualified_name.get(
                    (str(table.name).casefold(), _physical_column_match_key(str(column.name)))
                )
            )
            is not None
        }
        if len(candidates) == 1:
            references.update(candidates)
    return references


def _scope_table_sources(select: expressions.Select) -> list[expressions.Table]:
    """Return real table sources directly visible in a SELECT scope.

    ``Expression.find_all`` descends into nested subqueries, which makes an
    outer unqualified column look ambiguous with inner-query tables. SQL name
    resolution is scope-local: an unqualified column can only bind to the
    current SELECT's direct FROM/JOIN table sources.
    """

    sources: list[expressions.Table] = []
    from_ = select.args.get("from_")
    if from_ is not None and isinstance(from_.this, expressions.Table):
        sources.append(from_.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, expressions.Table):
            sources.append(join.this)
    return sources


def _physical_column_match_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()

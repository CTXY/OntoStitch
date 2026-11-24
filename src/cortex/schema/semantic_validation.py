"""Shared nested content guards without authoring ID assumptions."""

import re

import sqlglot
from sqlglot import expressions as exp


def validate_event_predicate(expression: str) -> None:
    """Require an occurrence predicate, rather than a scalar time or identifier.

    This checks expression shape only. Catalog validation checks physical inputs;
    business evidence must still establish what the predicate identifies and its grain.
    SQLGlot's Condition includes scalar Columns, so it is not a predicate test.
    """
    message = (
        "Event targets require a boolean occurrence-identification predicate; "
        "a timestamp, ID or other scalar is an attribute, not an occurrence binding"
    )
    try:
        statements = sqlglot.parse(f"SELECT {expression}")
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(message) from exc
    if len(statements) != 1:
        raise ValueError(message)
    tree = statements[0]
    if (
        not isinstance(tree, exp.Select)
        or len(tree.expressions) != 1
        or any(value for key, value in tree.args.items() if key != "expressions")
    ):
        raise ValueError(message)

    def predicate(value):
        if isinstance(value, exp.Paren):
            return predicate(value.this)
        if isinstance(value, (exp.And, exp.Or)):
            return predicate(value.this) and predicate(value.expression)
        if isinstance(value, exp.Not):
            return predicate(value.this)
        return isinstance(value, exp.Predicate)

    if not predicate(tree.expressions[0]):
        raise ValueError(message)


def validate_semantic_properties(
    node_type, properties, *, allow_legacy_relation_metadata=False
):
    if "relation_metadata" in properties:
        if not allow_legacy_relation_metadata:
            raise ValueError("Unsupported property relation_metadata; preserve business meaning in the node definition and individual relation descriptions")
        if node_type != "concept":
            raise ValueError("Legacy relation_metadata is only supported on Concept")
        from cortex.schema.relation_metadata import validate_metadata_items

        validate_metadata_items(properties["relation_metadata"])
    if node_type == "table":
        keys = properties.get("unique_keys", [])
        if not isinstance(keys, list) or any(
            not isinstance(group, list)
            or not group
            or any(not isinstance(r, str) or not r.strip() for r in group)
            or len(group) != len(set(group))
            for group in keys
        ):
            raise ValueError(
                "Table unique_keys require nonempty lists of distinct Column names or IDs"
            )
    if node_type == "metric":
        expression = properties.get("expression")
        if not isinstance(expression, dict) or not isinstance(
            expression.get("expr"), str
        ):
            raise ValueError("Metric expression requires a formula in expr")
        if not expression["expr"].strip() or set(expression) - {"expr", "refs"}:
            raise ValueError(
                "Metric expression requires nonempty expr and declared refs only"
            )
        bindings = expression.get("refs", {})
        if not isinstance(bindings, dict) or any(
            not isinstance(v, str) or not v.strip() for v in bindings.values()
        ):
            raise ValueError("Metric expression refs must map placeholders to node IDs")
        placeholders = set(re.findall(r"\$\{([^{}]+)\}", expression["expr"]))
        if placeholders != set(bindings):
            raise ValueError(
                "Metric formula placeholders must match expression refs; use ${key} in expression.expr"
            )
        validate_metric_calculation(expression["expr"])
        plans = properties.get("sql_generation_plans", [])
        if not isinstance(plans, list) or any(
            not isinstance(p, dict)
            or set(p) != {"id", "application", "steps"}
            or not isinstance(p.get("id"), str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", p["id"])
            or not isinstance(p.get("application"), str)
            or not p["application"].strip()
            or len(p["application"].strip()) > 500
            or not isinstance(p.get("steps"), list)
            or not 1 <= len(p["steps"]) <= 8
            or any(
                not isinstance(s, str) or not s.strip() or len(s.strip()) > 600
                for s in p["steps"]
            )
            for p in plans
        ):
            raise ValueError("Metric plans require id, application and nonempty steps")
        if len({p["id"] for p in plans}) != len(plans):
            raise ValueError("Metric SQL plan IDs must be unique")
    if node_type == "event" and not (
        isinstance(properties.get("definition"), str)
        and properties["definition"].strip()
    ):
        raise ValueError("Event requires a nonempty business occurrence type definition")
    if node_type in {"concept", "event"}:
        validate_matching_targets(
            properties.get("targets", []),
            properties.get("target_expressions", {}),
            required=node_type == "concept",
            node_type=node_type.title(),
        )

    if node_type == "generation_rule":
        for key in ("condition", "action", "reason"):
            if not isinstance(properties.get(key), str) or not properties[key].strip():
                raise ValueError(f"Generation Rule {key} must be nonempty text")
        if "triggers" not in properties:
            raise ValueError("Generation Rule requires triggers")
        validate_rule_triggers(properties["triggers"])
        triggers = properties["triggers"]
        if not (
            triggers.get("question", {}).get("phrases")
            or any(triggers.get("sql", {}).values())
        ):
            raise ValueError(
                "Generation Rule requires a retrieval signal"
            )


def validate_rule_triggers(triggers):
    """Validate the sole supported nested Rule signal format."""
    if not isinstance(triggers, dict) or set(triggers) - {"question", "sql"}:
        raise ValueError("Generation Rule triggers accept question and sql only")
    for key, fields in (
        ("question", {"phrases"}),
        ("sql", {"columns", "operations"}),
    ):
        branch = triggers.get(key, {})
        if not isinstance(branch, dict) or set(branch) - fields:
            raise ValueError(f"Invalid Generation Rule triggers.{key}")
        for field in fields:
            values = branch.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ValueError(
                    f"Generation Rule triggers.{key}.{field} requires nonempty strings"
                )


def validate_matching_targets(targets, target_expressions, *, required, node_type):
    """Validate match groups; their inputs do not imply direct graph connections."""
    if not isinstance(targets, list) or (required and not targets):
        raise ValueError(f"{node_type} targets require nonempty matching groups")
    for group in targets:
        if not isinstance(group, list) or not group or any(
            not isinstance(ref, str)
            or not ref.split(".", 1)[-1].strip()
            for ref in group
        ):
            raise ValueError(f"{node_type} targets must be nonempty groups of physical node IDs")
        if len(group) != len(set(group)):
            raise ValueError(f"{node_type} matching group contains duplicate endpoints")
    if not isinstance(target_expressions, dict) or any(
        not isinstance(key, str)
        or re.fullmatch(r"0|[1-9][0-9]*", key) is None
        or int(key) >= len(targets)
        or not isinstance(value, str)
        or not value.strip()
        for key, value in target_expressions.items()
    ):
        raise ValueError(f"{node_type} target_expressions must map valid group indices to nonempty expressions")
    fingerprints = [
        (tuple(sorted(group)), target_expressions.get(str(index), "").strip())
        for index, group in enumerate(targets)
    ]
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError(f"{node_type} contains duplicate matching groups")


def validate_metric_calculation(formula):
    """Reject stored inputs and syntactic identity wrappers masquerading as metrics.

    Full business atomicity remains an assessment responsibility. This guard accepts
    actual arithmetic, aggregate and conditional calculations, including unit scaling.
    """
    text = re.sub(r"\$\{[^{}]+\}", "operand", formula)
    try:
        statements = sqlglot.parse(text)
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError("Metric formula must be a valid calculation expression") from exc
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Metric requires one calculation expression")
    tree = statements[0]
    if isinstance(tree, (exp.Query, exp.DDL, exp.DML)):
        raise ValueError("Metric formula is an expression, not a complete query")  # noqa: TRY004

    def unwrapped(node):
        while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast, exp.Alias)):
            node = node.this
        if isinstance(node, (exp.Add, exp.Mul)):
            neutral = "0" if isinstance(node, exp.Add) else "1"
            if isinstance(node.expression, exp.Literal) and node.expression.this == neutral:
                return unwrapped(node.this)
            if isinstance(node.this, exp.Literal) and node.this.this == neutral:
                return unwrapped(node.expression)
        if isinstance(node, (exp.Sub, exp.Div)):
            neutral = "0" if isinstance(node, exp.Sub) else "1"
            if isinstance(node.expression, exp.Literal) and node.expression.this == neutral:
                return unwrapped(node.this)
        return node

    tree = unwrapped(tree)
    if not isinstance(tree, (exp.Binary, exp.Func, exp.Case, exp.Neg)):
        raise ValueError("Metric must calculate a business quantity; a stored field, CAST or identity transform is a Concept")  # noqa: TRY004

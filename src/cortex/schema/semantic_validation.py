"""Shared nested content guards without authoring ID assumptions."""

import re


def validate_semantic_properties(node_type, properties):
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
        if set(bindings.values()) != set(properties.get("references", [])):
            raise ValueError("Metric references must match expression refs")
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
    if node_type == "concept":
        targets = properties.get("targets", [])
        if not isinstance(targets, list) or any(
            not isinstance(t, dict)
            or set(t) - {"id", "sql_expression", "references"}
            or not isinstance(t.get("id"), str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", t["id"])
            or not isinstance(t.get("sql_expression"), str)
            or not t["sql_expression"].strip()
            or not isinstance(t.get("references", []), list)
            or any(
                not isinstance(r, str) or not r.strip() for r in t.get("references", [])
            )
            or len(t.get("references", [])) != len(set(t.get("references", [])))
            for t in targets
        ):
            raise ValueError(
                "Concept targets require id, sql_expression and node references"
            )
        ids = [t["id"] for t in targets]
        if len(ids) != len(set(ids)):
            raise ValueError("Concept target IDs must be unique")

        fingerprints = [
            (
                t["sql_expression"].strip(),
                tuple(sorted(t.get("references", []))),
            )
            for t in targets
        ]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("Concept contains duplicate semantic targets")

    if node_type == "generation_rule":
        for key in ("condition", "action", "reason"):
            if not isinstance(properties.get(key), str) or not properties[key].strip():
                raise ValueError(f"Generation Rule {key} must be nonempty text")
        if "triggers" not in properties:
            raise ValueError("Generation Rule requires triggers")
        validate_rule_triggers(properties["triggers"])
        triggers = properties["triggers"]
        if not (
            properties.get("references")
            or triggers.get("question", {}).get("phrases")
            or any(triggers.get("sql", {}).values())
        ):
            raise ValueError(
                "Generation Rule requires a retrieval signal or dependency"
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

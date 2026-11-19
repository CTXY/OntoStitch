"""Structured assessor responses for controller tests, not a production adapter."""


def assessment_data(groups, accept=None, targets=(), validation=None, quality_by_id=None):
    from cortex.capabilities.sql_training.training.quality_contract import (
        required_quality_checks,
    )
    members = sorted({m for g in groups for m in (g.member_ids if hasattr(g, "member_ids") else g["member_ids"])})
    good = set(members if accept is None else accept)
    rows = []
    for member in members:
        status = (quality_by_id or {}).get(member, "valid" if member in good else "invalid")
        rows.append({
            "unit_id": member,
            "quality": status,
            "quality_checks": dict.fromkeys(required_quality_checks({"node_type": member.split(".")[0]}, relation=True if member.startswith("edge.") else None), status),
            "feedback": [] if status == "valid" else [{"kind": "quality", "location": member, "gap": "clarify the supported behavior", "required_change": "clarify the supported behavior", "preserve": "the source meaning"}],
        })
    return {"unit_results": rows, "target_results": [
        {"target_id": t["target_id"], "status": "resolved", "feedback": []}
        for t in targets
    ], "regressions": [], "coupled_unit_ids": []}


def response_for_payload(payload, **kwargs):
    data = assessment_data(payload["change_groups"], targets=payload["fixed_targets"], validation={"attempts":payload["attempts"]}, **kwargs)
    units = {u["unit_id"]:u for u in payload["units"]}
    for result in data["unit_results"]:
        unit = units[result["unit_id"]]
        result["quality_checks"] = {
            key: result["quality"] for key in unit.get("quality_requirements", [])
        }
    return data

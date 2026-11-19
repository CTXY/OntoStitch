"""Lossless transport deduplication; internal graph/evidence payloads stay intact."""

from copy import deepcopy
import json
from langchain_core.messages import HumanMessage, ToolMessage


def compact_relation_history(messages):
    """Reference identical fields already shown, without deleting unique facts.

    Object versions and input/candidate views are separate. References point to
    first occurrences, so they cannot depend on another removed copy.
    """
    known = {}
    result = list(messages)
    fields = ("nodes", "edges", "items", "endpoint_nodes", "candidate_nodes", "candidate_relations")
    for index, message in enumerate(messages):
        if not isinstance(message, (HumanMessage, ToolMessage)):
            continue
        try:
            packet = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(packet, dict):
            continue
        view = packet.get("view", "candidate")
        if view == "light_relations_v1":
            view = "candidate"
        revision = packet.get("revision", 0)
        packet = deepcopy(packet)
        changed = False
        for field in fields:
            for row_index, row in enumerate(packet.get(field, [])):
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                reused = {}
                for key, value in list(row.items()):
                    if key in {"id", "reused_fields"}:
                        continue
                    parts = list(value.items()) if key == "properties" and isinstance(value, dict) else [(None, value)]
                    for prop, val in parts:
                        path = key + ("." + prop if prop is not None else "")
                        token = (view, revision, row["id"], path, json.dumps(val, sort_keys=True, ensure_ascii=False))
                        if token in known and len(token[-1]) > len(json.dumps({path: known[token]})) + 20:
                            reused[path] = known[token]
                            if prop is None:
                                row.pop(key)
                            else:
                                row[key].pop(prop)
                        else:
                            known[token] = f"messages[{index}].{field}[{row_index}].{path}"
                    if key == "properties" and not row.get(key) and any(p.startswith("properties.") for p in reused):
                        row.pop(key, None)
                if reused:
                    row["reused_fields"] = reused
                    changed = True
        if changed:
            packet["reused_fields_note"] = "reused_fields points to identical values at their first displayed message location; those values remain part of the supplied context."
            result[index] = message.model_copy(update={"content": json.dumps(packet, ensure_ascii=False)})
    return result


def action_message(response):
    """Carry tool arguments/IDs, not incidental narration, into the next turn.

    Provider callbacks retain the complete raw response. Evidence and tool
    results remain in the dialogue; final plain-JSON recovery stays untouched.
    """
    if response.tool_calls and not response.invalid_tool_calls:
        return response.model_copy(update={"content": ""})
    return response


def compact_node_packet(payload):
    packet = deepcopy(payload)
    reused = {}
    for archived, current in (("nodes", "candidate_nodes"), ("relations", "candidate_relations")):
        index = {row["id"]: row for row in packet.get(current, [])}
        reused[archived] = [row["id"] for row in packet.get(archived, []) if row == index.get(row["id"])]
        packet[archived] = [row for row in packet.get(archived, []) if row["id"] not in reused[archived]]
    packet["source_equals_candidate"] = reused
    if "evidence" in packet:
        packet["evidence"] = packet.pop("evidence")
    return packet


def supplied_records(payload):
    records = {}
    for field in ("nodes", "relations", "candidate_nodes", "candidate_relations", "physical_facts"):
        for row in payload.get(field, []):
            records.setdefault(row["id"], []).append((field, row))
    return records


def reference_supplied_records(value, records):
    """Keep candidate scores/signals; replace only identical complete object bodies."""
    if isinstance(value, dict):
        key = value.get("id")
        for field, row in records.get(key, []) if isinstance(key, str) else []:
            if value == row:
                return {"id": value["id"], "already_supplied": True, "location": field}
        return {k: reference_supplied_records(v, records) for k, v in value.items()}
    if isinstance(value, list):
        return [reference_supplied_records(v, records) for v in value]
    return value

"""Lossless transport deduplication; internal graph/evidence payloads stay intact."""

from copy import deepcopy


def compact_node_packet(payload):
    packet = deepcopy(payload)
    reused = {}
    for archived, current in (("nodes", "candidate_nodes"), ("relations", "candidate_relations")):
        index = {row["id"]: row for row in packet.get(current, [])}
        reused[archived] = [row["id"] for row in packet.get(archived, []) if row == index.get(row["id"])]
        packet[archived] = [row for row in packet.get(archived, []) if row["id"] not in reused[archived]]
    packet["source_equals_candidate"] = reused
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

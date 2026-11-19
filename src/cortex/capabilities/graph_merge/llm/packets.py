"""Share identical source/candidate records without cross-message field pointers."""

from copy import deepcopy


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

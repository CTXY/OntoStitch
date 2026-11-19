"""Task-local graph projection and an audited, bounded active conversation."""
from copy import deepcopy
import json

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


def action_message(response):
    """Keep structured tool actions; raw provider narration stays in the trace."""
    if response.tool_calls and not response.invalid_tool_calls:
        return response.model_copy(update={"content": ""})
    return response


def compact_node_packet(payload):
    """Share exact equal input/candidate records in one self-contained packet.

    Applying this at planning and sending is intentionally idempotent. A same-ID
    record with different content is never shared across views.
    """
    packet = deepcopy(payload)
    reused = {}
    previous = packet.get("source_equals_candidate", {})
    for archived, current in (("nodes", "candidate_nodes"), ("relations", "candidate_relations")):
        index = {row["id"]: row for row in packet.get(current, [])}
        equal = {row["id"] for row in packet.get(archived, []) if row == index.get(row["id"])}
        existing = {row["id"] for row in packet.get(archived, [])}
        equal.update(key for key in previous.get(archived, []) if key in index and key not in existing)
        reused[archived] = sorted(equal)
        if archived in packet:
            packet[archived] = [row for row in packet[archived] if row["id"] not in equal]
    packet["source_equals_candidate"] = reused
    if "evidence" in packet:
        packet["evidence"] = packet.pop("evidence")
    return packet


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _representable_call(call):
    return (isinstance(call.get('id'), str) and bool(call['id'])
            and isinstance(call.get('name'), str) and bool(call['name']))


class TaskDialogue:
    """Rebuild active messages from stable task data and latest repair state.

    Completed old submissions are audited outside this object. Read exchanges
    remain exact and snapshot-qualified; only exact repeat reads are replaced.
    At most one rejected final-output exchange is retained. Tool IDs are always
    paired, including malformed calls, and no graph/evidence text is truncated.
    """
    def __init__(self, system, payload):
        self.system = system
        self.payload = compact_node_packet(payload)
        self.reads = {}
        self.repair = None
        self.sequence = 0
        self.fallback_schema = None
        self.latest_draft = deepcopy(payload.get('draft'))
        self.latest_draft_feedback = deepcopy(payload.get('issues', []))
        self.repair_has_draft = False
        self.read_names = {'inspect_graph', 'lookup_catalog', 'read_evidence', 'read_guidance', 'read_source_units', 'inspect_draft'}

    def messages(self):
        packet = deepcopy(self.payload)
        if self.repair is not None:
            # The latest submission is present exactly once in its exchange.
            packet.pop('draft', None)
            packet.pop('issues', None)
            if self.latest_draft is not None and not self.repair_has_draft:
                packet['draft'] = deepcopy(self.latest_draft)
                if self.latest_draft_feedback:
                    packet['draft_validation_issues'] = deepcopy(self.latest_draft_feedback)
        result = [SystemMessage(self.system), HumanMessage(_json(packet))]
        exchanges = list(self.reads.values())
        if self.repair is not None:
            exchanges.append(self.repair)
        for _, messages in sorted(exchanges, key=lambda item: item[0]):
            result.extend(messages)
        if self.fallback_schema is not None:
            result.append(HumanMessage('Return the final result as plain JSON matching this schema: '
                                       + _json(self.fallback_schema)))
        return result

    def use_fallback(self, schema):
        self.fallback_schema = schema

    def plain_failure(self, response, feedback, *, draft=None):
        self.sequence += 1
        # Failed narration is not evidence. Preserve a parsed full draft, if
        # available, plus exact feedback instead of repeating provider prose.
        value = {'latest_output_error': feedback}
        self.repair_has_draft = draft is not None
        if draft is not None:
            self.latest_draft = deepcopy(draft)
            self.latest_draft_feedback = deepcopy(feedback)
            value['draft'] = draft
        self._clear_draft_reads()
        self.repair = (self.sequence, [HumanMessage(_json(value))])

    def _clear_draft_reads(self):
        self.reads = {key: value for key, value in self.reads.items()
                      if not key.startswith('inspect_draft:')}

    def exchange(self, response, results, *, result_name):
        """Store completed calls with matching replies, not cumulative drafts."""
        response = action_message(response)
        all_calls = [*response.tool_calls, *response.invalid_tool_calls]
        calls = [call for call in response.tool_calls if _representable_call(call)]
        if any(not _representable_call(call) for call in all_calls):
            self.plain_failure(response, {'status': 'rejected_unapplied',
                'error': 'Malformed tool call lacks a valid name or call ID; no operation was applied.'})
        # Malformed output is archived by the provider trace, not a source
        # premise. Preserve its call IDs without echoing unparseable megabytes.
        invalid = [{**call, 'args': '', 'error': 'Malformed tool JSON; original output archived'}
                   for call in response.invalid_tool_calls if _representable_call(call)]
        if invalid:
            response = response.model_copy(update={'content': '', 'invalid_tool_calls': invalid})
        by_id = {call['id']: call for call in [*calls, *invalid]}
        repair_calls, repair_results = [], []
        repair_has_draft = False
        read_names = self.read_names
        for call_id, result in results:
            if call_id not in by_id:
                continue
            call = by_id[call_id]
            name = call['name']
            is_invalid = call in invalid
            if name == result_name and not is_invalid:
                self.latest_draft = deepcopy(call['args'])
                self.latest_draft_feedback = deepcopy(result)
                repair_has_draft = True
                self._clear_draft_reads()
            if name == 'inspect_draft' and isinstance(result, dict) and result.get('draft') == self.latest_draft and self.repair is not None:
                continue
            if is_invalid or name == result_name or name not in read_names:
                repair_calls.append(call)
                repair_results.append((call_id, result))
                continue
            # Split parallel reads into independent closed exchanges. This
            # permits dropping a stale draft read without losing a graph read
            # that happened to share its provider response.
            self.sequence += 1
            action = response.model_copy(update={'tool_calls': [call], 'invalid_tool_calls': []})
            signature = name + ':' + _json([call.get('args'), result])
            self.reads[signature] = (self.sequence, [action, ToolMessage(_json(result), tool_call_id=call_id)])
        if repair_calls:
            self.repair_has_draft = repair_has_draft
            self.sequence += 1
            action = response.model_copy(update={
                'tool_calls': [call for call in repair_calls if call in calls],
                'invalid_tool_calls': [call for call in repair_calls if call in invalid]})
            self.repair = (self.sequence, [action, *[
                ToolMessage(_json(result), tool_call_id=call_id) for call_id, result in repair_results]])

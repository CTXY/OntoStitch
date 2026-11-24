"""Bounded reviews of one immutable candidate; coupled identities stay together."""
import json
from copy import deepcopy

from cortex.capabilities.graph_merge.integration.edit_feedback import (
    current_repair_fields,
)
from cortex.capabilities.graph_merge.integration.task_context import (
    LocalTask,
    build_task_context,
)


class ReviewInputTooLarge(RuntimeError):
    pass


def review_packets(context, candidate, task, *, origins, before, max_characters=110000):
    from cortex.capabilities.graph_merge.llm.packets import compact_node_packet
    def size(value):
        return len(json.dumps(compact_node_packet(value), ensure_ascii=False))
    remaining = set(task.targets)
    units = []
    # Overlapping matched groups form a single indivisible review unit.
    for group in task.groups:
        members = set(group["members"]) & remaining
        if not members:
            continue
        overlaps = [u for u in units if u & members]
        for u in overlaps:
            members |= u
            units.remove(u)
        units.append(members)
    covered = set().union(*units) if units else set()
    units += [{key} for key in sorted(remaining - covered)]

    def packet(ids):
        groups = [g for g in task.groups if set(g["members"]) & ids]
        local = LocalTask(task.id, task.phase, ids, groups=groups, checks=task.checks, feedback=task.feedback)
        payload = build_task_context(context, candidate, local, origins=origins, before=before)
        # Actual change/deletion inventories describe this slice, not the whole
        # batch. Complete dependency and source objects remain in the packet.
        affected_edges = {edge.id for graph in (before.graph, candidate.graph)
            for edge in graph.edges if edge.id in ids or edge.from_node_id in ids or edge.to_node_id in ids}
        for key, allowed in (("deleted_nodes", ids), ("deleted_relations", affected_edges)):
            payload[key] = [row for row in payload.get(key, []) if row["id"] in allowed]
        changes = payload.get("actual_changes", {})
        payload["actual_changes"] = {kind: {op: [k for k in keys if k in (affected_edges if kind == "relations" else ids)]
            for op, keys in ops.items()} for kind, ops in changes.items()}
        from cortex.capabilities.graph_merge.integration.node_context import (
            compact_task_context,
        )
        return compact_task_context(payload, context, candidate, ids, groups)


    pending = set()
    for unit in units:
        proposed = pending | unit
        value = packet(proposed)
        if size(value) > max_characters:
            if pending:
                yield packet(pending)
                pending = set()
                value = packet(unit)
            if size(value) > max_characters:
                raise ReviewInputTooLarge(f"Indivisible Node Assessment unit is {size(value)} characters, exceeds {max_characters}: {sorted(unit)}")
        pending |= unit
    if pending:
        yield packet(pending)


def assess_nodes(agent, context, candidate, task, *, origins, before, earlier_findings=(),
                 max_characters=110000, previous_gate=None, policy=''):
    from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
    from cortex.capabilities.graph_merge.llm.incremental import NodeReview
    from cortex.capabilities.graph_merge.llm.packets import (
        compact_node_packet,
        premise_fingerprint,
    )
    context.task_assessment_premises = []
    context.task_assessment_cache_reused = False
    if getattr(agent, 'node_local_contract', False):
        from cortex.capabilities.graph_merge.llm.node_context import build_node_packet
        # The immutable identity group is the indivisible unit. Downstream
        # consumers belong to reconciliation rather than node assessment.
        packet = build_node_packet(context, {'task_id': task.id, 'phase': 'node_assess'},
            before=before, candidate=candidate)
        if len(json.dumps(packet, ensure_ascii=False)) > max_characters:
            raise ReviewInputTooLarge('Complete single-group Node Assessment exceeds the configured local limit')
        packets = [packet]
    else:
        packets = list(review_packets(context, candidate, task, origins=origins, before=before,
                                     max_characters=max_characters))
    reviews = []
    context.task_partial_semantic_findings = ()
    for index, payload in enumerate(packets):
        payload["task_id"] = f"{task.id}-slice-{index}"
        payload["earlier_findings"] = list(earlier_findings)
        if getattr(context, 'task_semantic_issue_basis', ()):
            payload['semantic_issue_basis'] = list(context.task_semantic_issue_basis)
        if payload.get("feedback") == list(earlier_findings):
            payload.pop("feedback")
        payload["current_repair_fields"] = current_repair_fields(candidate, earlier_findings)
        if getattr(context, 'task_required_context', ()):
            payload['required_context'] = list(context.task_required_context)
    # Decide before any model call. The caller has checked source/policy/base
    # compatibility; every old slice's observations must fit its new packet.
    previous = previous_gate or {}
    observed = previous.get('actual_packets', [])
    if (previous.get('phase') == 'node_assess' and previous.get('policy') == policy
            and previous.get('base_hash') == canonical_hash(before)
            and previous.get('candidate_hash') == canonical_hash(candidate)
            and previous.get('last_review', {}).get('verdict') == 'revise'
            and len(observed) == len(packets)):
        proposed = [{'payload': compact_node_packet(payload), 'reads': deepcopy(saved['reads'])}
                    for payload, saved in zip(packets, observed)]
        fingerprint = canonical_hash(sorted({premise_fingerprint(
            item['payload'], reads=item['reads'], policy=policy) for item in proposed}))
        if fingerprint == previous.get('premise_fingerprint'):
            context.task_assessment_premises = proposed
            context.task_assessment_cache_reused = True
            return NodeReview.model_validate(previous['last_review'])
    for payload in packets:
        # The actual presented packet and each slice's own observations define
        # this review. A deterministic Agent may not set the observations, so
        # clear them before invoking instead of inheriting the previous slice.
        context.task_relevant_facts = ()
        presented = compact_node_packet(payload)
        reviews.append(agent.invoke("node_assess", context, payload))
        context.task_assessment_premises.append({
            'payload': presented,
            'reads': deepcopy(getattr(context, 'task_relevant_facts', ())),
        })
        if reviews[-1].verdict == "revisit_identity":
            return reviews[-1]
    context.task_partial_semantic_findings = tuple(issue for review in reviews if review.verdict == 'revise'
                                                 for issue in review.issues)
    return NodeReview(
        verdict=("accept" if all(r.verdict == "accept" for r in reviews)
                 else "needs_context" if any(r.verdict == 'needs_context' for r in reviews) else "revise"),
        issues=tuple(i for r in reviews for i in r.issues),
    )

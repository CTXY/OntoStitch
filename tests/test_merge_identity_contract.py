from __future__ import annotations

import pytest

from cortex.capabilities.graph_merge.identity import IdentityGrouper
from cortex.capabilities.graph_merge.models import (
    EvidenceItem,
    NodeIdentitySelection,
    QueryIdentityVerdict,
)
from cortex.schema import GraphNode, SourceReference, load_root_schema


class RecordingResolver:
    def __init__(
        self, verdict="same", reason="Same requested obligation, not complementary"
    ):
        self.calls = []
        self.verdict = verdict
        self.reason = reason

    def resolve(self, seed, collection_candidates, query_candidates, policy):
        self.calls.append((seed, collection_candidates, query_candidates))
        return NodeIdentitySelection(
            selected_entry_id=(
                collection_candidates[0].entry_id
                if collection_candidates and self.verdict == "same"
                else None
            ),
            query_verdicts=tuple(
                QueryIdentityVerdict(
                    query_id=item.query_id, verdict=self.verdict, reason=self.reason
                )
                for item in query_candidates
            ),
            reason=self.reason,
        )


def rule(node_id, intent):
    return GraphNode(
        id=node_id,
        node_type="generation_rule",
        name="Patient counting",
        properties={
            "condition": "When the request asks how many patients",
            "action": "Count patients using Patient.ID",
            "operation_intents": [intent],
        },
    )


def test_rule_intent_is_not_a_candidate_exclusion_or_prose_validation():
    resolver = RecordingResolver()
    result = IdentityGrouper(resolver).group(
        (rule("rule.existing", "count"),),
        (rule("rule.incoming", "deduplication"),),
        load_root_schema().node_type_index(),
    )
    assert len(resolver.calls) == 1
    assert len(result.groups) == 1
    assert result.groups[0].existing_node_id == "rule.existing"
    assert len(result.groups[0].members) == 2
    assert "INVALID_SELECTION" not in result.diagnostic_codes


@pytest.mark.parametrize("verdict,expected_groups", [("same", 1), ("different", 2)])
def test_concept_targets_recall_candidates_but_do_not_decide_identity(
    verdict, expected_groups
):
    def concept(node_id, name, target_id):
        return GraphNode(
            id=node_id,
            node_type="concept",
            name=name,
            properties={
                "targets": [["column.Patient.Diagnosis"]],
                "target_expressions": {"0": "Patient.Diagnosis"},
            },
        )

    resolver = RecordingResolver(verdict)
    result = IdentityGrouper(resolver).group(
        (concept("concept.existing", "diagnosis", "original"),),
        (concept("concept.incoming", "病种", "case-local"),),
        load_root_schema().node_type_index(),
    )
    assert len(resolver.calls) == 1
    seed, entries, _ = resolver.calls[0]
    assert seed.representation.key_implementation["targets"] == [
        ["column.Patient.Diagnosis"]
    ]
    assert entries[0].representations[0].key_implementation["targets"] == [
        ["column.Patient.Diagnosis"]
    ]
    assert len(result.groups) == expected_groups


def test_matching_source_evidence_is_scoped_and_bounded():
    source = SourceReference(source_id="case:patient-count")
    unrelated = SourceReference(source_id="unrelated")

    class EvidenceResolver:
        def resolve(self, sources):
            return (
                EvidenceItem(
                    id="unrelated",
                    source=unrelated,
                    case_id=None,
                    content={"question": "Must not enter this comparison"},
                ),
                EvidenceItem(
                    id="patient-count",
                    source=source,
                    case_id=None,
                    content={"question": "How many patients?", "sql": "x" * 12_000},
                ),
            )

    resolver = RecordingResolver()
    incoming = rule("rule.incoming", "count").model_copy(update={"sources": (source,)})
    IdentityGrouper(resolver, evidence_resolver=EvidenceResolver()).group(
        (rule("rule.existing", "count"),),
        (incoming,),
        load_root_schema().node_type_index(),
    )
    implementation = resolver.calls[0][0].representation.key_implementation
    evidence = implementation["source_evidence"]
    assert [item["id"] for item in evidence] == ["patient-count"]
    assert len(evidence[0]["content_excerpt"]) == 8_000
    assert evidence[0]["truncated"] is True


def test_missing_matching_evidence_does_not_fail_the_workflow():
    class BrokenEvidence:
        def resolve(self, sources):
            raise LookupError("unavailable source")

    resolver = RecordingResolver("different")
    incoming = rule("rule.incoming", "count").model_copy(
        update={"sources": (SourceReference(source_id="missing"),)}
    )
    result = IdentityGrouper(resolver, evidence_resolver=BrokenEvidence()).group(
        (rule("rule.existing", "count"),),
        (incoming,),
        load_root_schema().node_type_index(),
    )
    assert len(result.groups) == 2
    assert (
        resolver.calls[0][0].representation.key_implementation["source_evidence"] == []
    )

"""Stable, source-relative proposal buffer shared by both merge stages."""

import json
from copy import deepcopy
from threading import RLock

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.compiler import compile_proposal
from cortex.capabilities.graph_merge.integration.models import IntegrationProposal
from cortex.schema import GraphEdge, GraphNode
from cortex.schema.semantic_validation import validate_semantic_properties
from cortex.schema.validation import _validate_properties


def edit_key(edit):
    kind = (
        "node"
        if edit["operation"].endswith("node") or edit["operation"] == "set_conflict"
        else "edge"
    )
    return kind, edit.get(kind + "_id") or edit[kind]["id"]


def content_errors(edits, schema):
    """Check all supplied objects, including complete conflict alternatives, early."""
    errors = []
    for edit in edits:
        try:
            kind, key = edit_key(edit)
            if edit["operation"].startswith("delete_"):
                continue
            if kind == "node":
                raw = edit[kind]
                for field, placeholder in [
                    ("name", raw.get("id")),
                    ("description", None),
                    ("aliases", []),
                    ("tags", []),
                ]:
                    if (
                        raw.get("alternatives")
                        and all(
                            field in a.get("overrides", {}) for a in raw["alternatives"]
                        )
                        and raw.get(field) != placeholder
                    ):
                        raise ValueError(
                            f"node.{field} must be {placeholder!r} because every alternative overrides it; put full differing values inside alternatives, not the common node"
                        )
            obj = (GraphNode if kind == "node" else GraphEdge).model_validate(
                edit[kind]
            )
            if kind == "node":
                variants = [
                    GraphManager.resolve_alternative(obj, a) for a in obj.alternatives
                ] or [obj]
                for variant in variants:
                    _validate_properties(
                        variant.properties,
                        schema.effective_node_fields(obj.node_type),
                        f"nodes.{key}.properties",
                    )
                    validate_semantic_properties(obj.node_type, variant.properties)
            elif (
                not isinstance(obj.properties.get("description"), str)
                or not obj.properties["description"].strip()
            ):
                raise ValueError("edge.properties.description must be nonempty")
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(
                {
                    "element": edit.get("node_id")
                    or edit.get("edge_id")
                    or (edit.get("node") or edit.get("edge") or {}).get("id"),
                    "path": "node.properties" if edit.get("node") else "edge",
                    "message": str(exc),
                }
            )
    return errors


class ProposalBuffer:
    """Invalid pending data never replaces the last successfully compiled candidate.

    Edits replace by (kind, object ID); changes replace by overlapping source groups.
    No correspondence or semantic repair is inferred.
    """

    def __init__(self, context, decision, proposal=None):
        self.context, self.decision = context, decision
        self.pending = (proposal or IntegrationProposal()).model_dump(mode="json")
        self.accepted = proposal or IntegrationProposal()
        self.lock = RLock()
        self.revision = 0
        self.preview = None

    def update(self, **kwargs):
        with self.lock:
            return self._update(**kwargs)

    def _update(
        self,
        *,
        edits=(),
        changes=(),
        relation_contracts=(),
        remove_edits=(),
        remove_contracts=(),
        remove_changes=(),
        expected_revision=None,
    ):
        if expected_revision is not None and expected_revision != self.revision:
            return {"status": "stale_revision", "revision": self.revision}
        data = deepcopy(self.pending)
        try:
            replacements = {edit_key(e): e for e in edits}
            if len(replacements) != len(edits):
                raise ValueError("One operation per object in a submission")
            remove = set(remove_edits)
            data["edits"] = [
                e
                for e in data["edits"]
                if edit_key(e) not in replacements and edit_key(e)[1] not in remove
            ] + list(edits)
            replaced = {
                (e["kind"], e["id"], e.get("alternative_id"))
                for c in changes
                for e in c["before"]
            }
            removed = {
                (e["kind"], e["id"], e.get("alternative_id")) for e in remove_changes
            }
            replaced |= removed
            kept = []
            for c in data["changes"]:
                before = {
                    (e["kind"], e["id"], e.get("alternative_id")) for e in c["before"]
                }
                if before & replaced and not before <= replaced:
                    raise ValueError(
                        "Replace all before elements of this affected correspondence group; submitted edits were NOT saved: "
                        + json.dumps(c, ensure_ascii=False)
                    )
                if not before & replaced:
                    kept.append(c)
            data["changes"] = kept + list(changes)
            # Pending definitions may be corrected or withdrawn. Installed contracts
            # remain immutable and are checked by compile_proposal against context.
            from cortex.schema.models import EdgeType

            contracts = {
                c["id"]: c
                for c in data["relation_contracts"]
                if c["id"] not in set(remove_contracts)
            }
            for contract in relation_contracts:
                parsed_contract = EdgeType.model_validate(contract)
                contracts[parsed_contract.id] = parsed_contract.model_dump(mode="json")
            data["relation_contracts"] = list(contracts.values())
        except (ValueError, TypeError, KeyError) as exc:
            return {
                "status": "rejected",
                "revision": self.revision,
                "errors": [str(exc)],
            }
        self.pending = data
        self.revision += 1
        if hasattr(self, "artifact_dir"):
            (self.artifact_dir / "pending_proposal.json").write_text(
                json.dumps(
                    {"revision": self.revision, "proposal": self.pending},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        errors = content_errors(data["edits"], self.context.schema)
        if errors:
            return {
                "status": "needs_repair",
                "revision": self.revision,
                "errors": errors,
            }
        try:
            parsed = IntegrationProposal.model_validate(data)
            preview = compile_proposal(
                self.context, self.decision, parsed, strict=False
            )
        except (ValueError, TypeError, KeyError) as exc:
            return {
                "status": "needs_repair",
                "revision": self.revision,
                "errors": [str(exc)],
                "pending_contract_ids": [c["id"] for c in data["relation_contracts"]],
                "repair_hint": "Replace pending objects by ID. remove_contracts withdraws proposed contracts only; installed contracts remain immutable. remove_changes supplies all before-elements of a group to withdraw it.",
            }
        self.accepted, self.preview = parsed, preview
        self.pending = parsed.model_dump(mode="json")
        if hasattr(self, "artifact_dir"):
            (self.artifact_dir / "validated_proposal.json").write_text(
                json.dumps(
                    {"revision": self.revision, "proposal": self.pending},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        self.context.candidate = preview.state
        return {
            "status": "validated",
            "revision": self.revision,
            "edit_count": len(parsed.edits),
            "change_count": len(parsed.changes),
            "current_rule_ids": [
                n.id
                for n in preview.state.graph.nodes
                if n.node_type == "generation_rule"
            ],
        }

    @property
    def valid(self):
        return (
            self.pending == self.accepted.model_dump(mode="json")
            and self.preview is not None
        )

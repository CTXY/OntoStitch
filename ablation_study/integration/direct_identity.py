"""Recover historical identity decisions without model or embedding calls."""

from __future__ import annotations

import json
from pathlib import Path

from cortex.capabilities.graph_merge.identity.legacy import (
    IdentityGrouper,
    build_default_identity_implementation_registry,
)
from cortex.capabilities.graph_merge.llm.references import _RefCodec
from cortex.capabilities.graph_merge.models import (
    GroupIdentityView,
    NodeIdentitySelection,
    NodeIdentityView,
    RepresentationRef,
)


class HistoricalIdentityGrouper(IdentityGrouper):
    """Replay archived candidates and decisions, including historical rejection guards."""

    def __init__(self, trace: Path):
        super().__init__(
            resolver=self,
            implementation_registry=build_default_identity_implementation_registry(),
        )
        self.requests = {}
        self.retrievals = {}
        self.cross_type = []
        self.used = set()
        for line in trace.read_text().splitlines():
            event = json.loads(line)
            component = event.get("component")
            if component == "candidate_retrieval":
                ref = event["seed_ref"]["ref_id"]
                self.retrievals[ref] = event
            elif component == "identity":
                payload = json.loads(event["request"][-1]["content"])
                seed = payload["seed"]["query_id"]
                self.requests[seed] = (payload, event.get("parsed_response"))
            elif component == "cross_type_identity":
                self.cross_type.append(
                    (
                        json.loads(event["request"][-1]["content"]),
                        event.get("parsed_response"),
                    )
                )

    def resolve(self, seed, entries, queries, policy):
        result = self.requests[seed.query_id][1]
        if result is None:
            raise ValueError("historical identity model failed")
        return NodeIdentitySelection.model_validate(result)

    def _select_semantic(self, seed, groups, queries, policy, effective_fields):
        seed_id = seed.ref.ref_id
        self.used.add(seed_id)
        if seed_id not in self.requests:
            retrieval = self.retrievals.get(seed_id)
            if (
                retrieval is None
                or retrieval["selected_entries"]
                or retrieval["selected_queries"]
            ):
                raise ValueError(f"missing historical identity decision: {seed_id}")
            return None, (), None
        payload, _ = self.requests[seed_id]
        seed_view = NodeIdentityView.model_validate(payload["seed"])
        current = self._view(seed, policy, effective_fields)
        if _view_content(seed_view.representation) != _view_content(current):
            raise ValueError(f"historical identity seed content mismatch: {seed_id}")
        entries = tuple(
            GroupIdentityView.model_validate(row)
            for row in payload["collection_candidates"]
        )
        candidates = tuple(
            NodeIdentityView.model_validate(row) for row in payload["query_candidates"]
        )
        if any(row.entry_id not in groups for row in entries) or any(
            row.query_id not in queries for row in candidates
        ):
            raise ValueError(
                "historical identity candidates do not match reconstructed inputs"
            )
        for row in entries:
            actual = [
                self._view(m, policy, effective_fields)
                for m in groups[row.entry_id].members
            ]
            if [_view_content(v) for v in row.representations] != [
                _view_content(v) for v in actual
            ]:
                raise ValueError(
                    f"historical collection content mismatch: {row.entry_id}"
                )
        for row in candidates:
            actual = self._view(queries[row.query_id], policy, effective_fields)
            if _view_content(row.representation) != _view_content(actual):
                raise ValueError(
                    f"historical candidate content mismatch: {row.query_id}"
                )
        return self._resolve_semantic_selection(
            seed, seed_view, entries, candidates, policy
        )

    def _cross_type_replacements(self, existing, incoming):
        if not self.cross_type:
            return (), ()
        if len(self.cross_type) != 1:
            raise ValueError(
                "multiple cross-type passes require an explicit frozen grouping"
            )
        _, result = self.cross_type[0]
        if result is None:
            return (), ("MODEL_FAILURE",)
        # The original pair ID is opaque; recover the two original refs from the
        # trace's complete candidate payload, then preserve the recorded keep ref.
        return super()._cross_type_replacements(existing, incoming)

    def group(self, existing, incoming, node_types, effective_fields_by_type=None, *, existing_alternatives=None):
        if self.cross_type:
            from cortex.capabilities.graph_merge.models import CrossTypeResolution

            if len(self.cross_type) != 1:
                raise ValueError(
                    "multiple cross-type passes require an explicit frozen grouping"
                )
            historical_payload, resolution = self.cross_type[0]
            errors = []

            class FrozenCrossType:
                def resolve(self, candidates):
                    if resolution is None:
                        raise ValueError("historical cross-type model failed")
                    payload = {
                        "candidates": [c.model_dump(mode="json") for c in candidates]
                    }
                    codec = _RefCodec.from_payload(payload)
                    if codec.encode(payload) != historical_payload:
                        errors.append(
                            "historical cross-type candidates differ from inputs"
                        )
                        raise ValueError(errors[-1])
                    return codec.decode_model(
                        CrossTypeResolution.model_validate(resolution),
                        CrossTypeResolution,
                    )

            self.cross_type_resolver = FrozenCrossType()
        result = super().group(existing, incoming, node_types, effective_fields_by_type, existing_alternatives=existing_alternatives)
        if self.cross_type and errors:
            raise ValueError(errors[0])
        unused = set(self.requests) - self.used
        if unused:
            raise ValueError(f"unconsumed identity decisions: {sorted(unused)}")
        refs = [ref for pair in result.cross_type_replacements for ref in pair]
        known = {
            RepresentationRef(origin=origin, ref_id=node.id)
            for origin, nodes in (
                ("existing_node", existing),
                ("incoming_node", incoming),
            )
            for node in nodes
        }
        if not set(refs) <= known:
            raise ValueError("cross-type replay returned unknown source refs")
        return result


def _view_content(view):
    # Archived ref IDs are short transport aliases, not membership IDs.
    return view.model_dump(mode="json", exclude={"ref"})

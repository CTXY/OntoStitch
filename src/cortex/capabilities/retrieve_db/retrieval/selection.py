from __future__ import annotations

import asyncio
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict

from cortex.application.retrieval import RelevanceDecision, RetrievedRepresentation
from cortex.capabilities.retrieve_db.llm.prompts import load_prompt
from cortex.infrastructure.llm import run_llm_extraction


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RelevanceOutput(_Model):
    decisions: tuple[RelevanceDecision, ...]


class _DocumentIdHandles:
    """Run-local LLM vocabulary for verbose retrieval document IDs."""

    def __init__(self, document_ids: tuple[str, ...]) -> None:
        ordered = tuple(dict.fromkeys(document_ids))
        self._to_handle = {
            document_id: f"d{index:04d}" for index, document_id in enumerate(ordered, 1)
        }
        self._to_document_id = {
            handle: document_id for document_id, handle in self._to_handle.items()
        }

    def encode(self, document_id: str) -> str:
        return self._to_handle[document_id]

    def decode_decisions(
        self, decisions: tuple[RelevanceDecision, ...]
    ) -> tuple[RelevanceDecision, ...]:
        return tuple(
            item.model_copy(
                update={
                    "document_id": self._to_document_id.get(
                        item.document_id, item.document_id
                    )
                }
            )
            for item in decisions
        )


class RelevanceSelector(Protocol):
    async def select(
        self,
        *,
        question: str,
        initial_sql: str | None,
        representations: tuple[RetrievedRepresentation, ...],
    ) -> tuple[RelevanceDecision, ...]: ...


class LlmRelevanceSelector:
    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def select(
        self,
        *,
        question: str,
        initial_sql: str | None,
        representations: tuple[RetrievedRepresentation, ...],
    ) -> tuple[RelevanceDecision, ...]:
        if not representations:
            return ()
        semantic = tuple(
            r
            for r in representations
            if r.document.node_type in {"concept", "event", "metric", "generation_rule"}
        )
        physical = tuple(r for r in representations if r not in semantic)
        results = await asyncio.gather(
            self._select_group(
                question, initial_sql, semantic, "select_semantic_assets"
            ),
            self._select_group(
                question, initial_sql, physical, "select_schema_grounding"
            ),
        )
        return tuple(item for result in results for item in result)

    async def _select_group(self, question, initial_sql, representations, prompt_name):
        if not representations:
            return ()
        handles = _DocumentIdHandles(
            tuple(item.document.document_id for item in representations)
        )
        entries = []
        for item in representations:
            document = item.document
            entries.append(
                {
                    "document_id": handles.encode(document.document_id),
                    "node_id": document.canonical_node_id,
                    "node_type": document.node_type,
                    "evidence": document.structured_signals,
                }
            )
        if prompt_name == "select_schema_grounding":
            groups = {}
            for entry in entries:
                content = entry["evidence"].get("content", {})
                properties = content.get("properties", {})
                owner = (
                    properties.get("column_ref")
                    if entry["node_type"] == "dimension_value"
                    else entry["node_id"]
                )
                group = groups.setdefault(owner, {"column": None, "values": []})
                compact = {
                    **entry,
                    "evidence": {
                        "name": content.get("name"),
                        "description": content.get("description"),
                        "aliases": content.get("aliases"),
                        "properties": {
                            k: v for k, v in properties.items() if k != "sample_values"
                        },
                        "column_contexts": entry["evidence"].get("column_contexts", []),
                    },
                }
                if entry["node_type"] == "dimension_value":
                    group["values"].append(compact)
                else:
                    group["column"] = compact
            payload = {"column_groups": list(groups.values())}
        else:
            payload = {"assets": entries}
        request = {
            "question": question,
            "initial_sql": initial_sql or "",
            **payload,
        }
        expected = {item.document.document_id for item in representations}
        for attempt in range(2):
            result = await run_llm_extraction(
                llm=self._llm,
                prompt=load_prompt(prompt_name),
                output_model=RelevanceOutput,
                human_payload=request,
            )
            decisions = handles.decode_decisions(result.decisions)
            try:
                _validate_partition(
                    expected=expected,
                    supplied=[item.document_id for item in decisions],
                    label="relevance",
                )
                return decisions
            except ValueError:
                if attempt:
                    raise
                request["format_feedback"] = {
                    "requirement": "Return each supplied document handle exactly once. Do not omit, duplicate or invent handles. Reuse the evidence above; no default relevance is inferred by code.",
                    "expected_handles": [handles.encode(k) for k in sorted(expected)],
                    "previous_decisions": result.model_dump(mode="json"),
                }


def _validate_partition(*, expected: set[str], supplied: list[str], label: str) -> None:
    if len(supplied) != len(set(supplied)) or set(supplied) != expected:
        raise ValueError(f"{label} output must cover every input exactly once")


__all__ = [
    "LlmRelevanceSelector",
    "RelevanceSelector",
]

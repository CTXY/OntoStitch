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


class KeepRecalledSelector:
    """No model judgment: every recalled representation reaches graph assembly."""

    async def select(self, *, question, initial_sql, representations):
        return tuple(RelevanceDecision(document_id=item.document.document_id,
                                       status="relevant",
                                       reason="Relevance filtering disabled; retain recalled representation.")
                     for item in representations)


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
        accepted = {}
        pending = set(expected)
        for attempt in range(2):
            result = await run_llm_extraction(
                llm=self._llm,
                prompt=load_prompt(prompt_name),
                output_model=RelevanceOutput,
                human_payload=request,
            )
            decisions = handles.decode_decisions(result.decisions)
            supplied = [item.document_id for item in decisions]
            invalid = len(supplied) != len(set(supplied)) or not set(supplied) <= pending
            if not invalid:
                accepted.update((item.document_id, item) for item in decisions)
                pending -= set(supplied)
                if not pending:
                    return tuple(accepted[item.document.document_id] for item in representations)
            if attempt:
                raise ValueError("relevance output must cover every input exactly once")
            if invalid:
                # Ambiguous duplicates/foreign handles cannot be partially accepted.
                accepted, pending = {}, set(expected)
                request = {"question": question, "initial_sql": initial_sql or "", **payload}
            else:
                # Preserve valid decisions; ask only about missing documents. The
                # model still judges every status, never a code-generated default.
                wanted = {handles.encode(k) for k in pending}
                if "assets" in payload:
                    reduced = {"assets": [e for e in payload["assets"] if e["document_id"] in wanted]}
                else:
                    from copy import deepcopy
                    groups = []
                    for group in deepcopy(payload["column_groups"]):
                        column = group["column"]
                        values = [v for v in group["values"] if v["document_id"] in wanted]
                        keep_column = column is not None and column["document_id"] in wanted
                        if not keep_column and column is not None:
                            for value in values:
                                value["evidence"]["owner_column"] = column["evidence"]
                        if keep_column or values:
                            groups.append({"column": column if keep_column else None, "values": values})
                    reduced = {"column_groups": groups}
                request = {"question": question, "initial_sql": initial_sql or "", **reduced}
            request["format_feedback"] = {
                "requirement": "Return only these document handles exactly once. Earlier valid decisions are retained by the controller. Do not invent handles or infer a default status.",
                "expected_handles": [handles.encode(k) for k in sorted(pending)],
            }


def _validate_partition(*, expected: set[str], supplied: list[str], label: str) -> None:
    if len(supplied) != len(set(supplied)) or set(supplied) != expected:
        raise ValueError(f"{label} output must cover every input exactly once")


__all__ = [
    "LlmRelevanceSelector",
    "RelevanceSelector",
]

"""Unified question-conditioned retrieval consumed by training and online context."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from cortex.application.retrieval import ColumnSemanticContext
from cortex.application.retrieve_db import RetrieveDbError
from cortex.capabilities.query_signals import (
    GraphPhysicalSchemaRenderer,
    InitialSqlGenerator,
)
from cortex.capabilities.query_signals import prompt_version as sql_prompt_version
from cortex.capabilities.query_signals.signals import InitialSqlOutput
from cortex.capabilities.retrieve_db.llm.prompts import prompt_version
from cortex.capabilities.retrieve_db.retrieval.recall import TypedRecall
from cortex.capabilities.retrieve_db.retrieval.selection import (
    LlmRelevanceSelector,
    RelevanceOutput,
)
from cortex.capabilities.retrieve_db.workflow.nodes import RetrievalNodes
from cortex.capabilities.retrieve_db.workflow.runtime import (
    RetrievalWorkflow,
    context_fingerprint,
)
from cortex.schema import calculate_schema_hash, load_root_schema


class RetrieveDbCapability:
    def __init__(
        self,
        *,
        llm=None,
        embeddings=None,
        relevance_selector=None,
        typed_recall=None,
        initial_sql_generator=None,
        schema_renderer=None,
        ontology_schema=None,
        checkpoint_root=None,
        limits=None,
        document_vector_cache=None,
        embedding_identity=None,
        context_settings=None,
    ):
        if relevance_selector is None and llm is None:
            raise ValueError("retrieval requires an LLM or a relevance selector")
        self._schema = ontology_schema or load_root_schema()
        recall = typed_recall or TypedRecall(
            embeddings,
            limits
            or {
                "concept": 12,
                "metric": 10,
                "generation_rule": 10,
                "column": 20,
                "dimension_value": 12,
            },
            document_cache=document_vector_cache,
            embedding_identity=embedding_identity,
        )
        nodes = RetrievalNodes(
            recall=recall,
            selector=relevance_selector or LlmRelevanceSelector(llm),
            initial_sql_generator=initial_sql_generator
            or (InitialSqlGenerator(llm) if llm is not None else None),
            schema_renderer=schema_renderer or GraphPhysicalSchemaRenderer(),
        )
        # Identity includes schemas and model/config metadata, never API credentials.
        identity = context_fingerprint(
            {
                "prompts": prompt_version(),
                "initial_sql_prompt": sql_prompt_version(),
                "output": RelevanceOutput.model_json_schema(),
                "initial_sql_output": InitialSqlOutput.model_json_schema(),
                "schema": calculate_schema_hash(self._schema),
                "limits": recall.limits,
                "settings": context_settings or {},
                "context_expansion": "one_hop_required_references_v1",
                "model": getattr(llm, "model_name", getattr(llm, "model", None)),
            }
        )
        self._workflow = RetrievalWorkflow(
            nodes,
            checkpoint_root=Path(checkpoint_root)
            if checkpoint_root is not None
            else None,
            context_id=identity,
        )

    @property
    def context_fingerprint(self):
        return self._workflow.context_id

    async def retrieve(
        self,
        *,
        question,
        graph_state,
        graph_fingerprint,
        dialect,
        initial_sql,
        enable_clarification=False,
        column_context: tuple[ColumnSemanticContext, ...] = (),
        run_id=None,
    ):
        # Retrieval deliberately preserves ambiguity; online context owns user interaction.
        return await self._workflow.run(
            {
                "question": question,
                "graph_state": graph_state.model_dump(mode="json"),
                "graph_fingerprint": graph_fingerprint,
                "dialect": dialect,
                "initial_sql": initial_sql,
                "column_context": [
                    item.model_dump(mode="json") for item in column_context
                ],
                "run_id": run_id or uuid4().hex,
            }
        )

    async def resume(self, **kwargs):
        raise RetrieveDbError(
            "retrieval does not ask user questions; use online-context for clarification or rerun with the same run_id for recovery"
        )

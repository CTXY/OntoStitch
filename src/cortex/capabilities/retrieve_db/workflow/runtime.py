"""Async persistent retrieval with strict request compatibility on replay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from cortex.application.retrieve_db import RetrieveDbResult, RetrievalCheckpointCompatibilityError
from cortex.capabilities.retrieve_db.workflow.graph import build_retrieval_graph
from cortex.infrastructure.observability import native_tracing
from cortex.infrastructure.workflow.locking import workflow_writer

WORKFLOW_VERSION = "retrieval-v2-inline"


class RetrievalWorkflow:
    def __init__(self, nodes, *, checkpoint_root: Path | None = None, context_id: str):
        self.nodes = nodes
        self.checkpoint_root = checkpoint_root
        self.context_id = context_id

    async def run(self, request):
        config = {
            "configurable": {"thread_id": request["run_id"]},
            "metadata": {
                "retrieval_run_id": request["run_id"],
                "graph_fingerprint": request["graph_fingerprint"],
                "retrieval_context": self.context_id,
            },
        }
        request = {
            **request,
            "workflow_version": WORKFLOW_VERSION,
            "context_id": self.context_id,
        }
        with native_tracing():
            if self.checkpoint_root is None:
                output = await build_retrieval_graph(self.nodes).ainvoke(
                    {"request": request}, config
                )
            else:
                self.checkpoint_root.mkdir(parents=True, exist_ok=True)
                key = hashlib.sha256(request["run_id"].encode()).hexdigest()
                with workflow_writer(self.checkpoint_root / f"{key}.lock"):
                    async with AsyncSqliteSaver.from_conn_string(
                        str(self.checkpoint_root / f"{key}.sqlite")
                    ) as saver:
                        await saver.setup()
                        graph = build_retrieval_graph(self.nodes, saver)
                        saved = await graph.aget_state(config)
                        if saved.values and saved.values.get("request") != request:
                            raise RetrievalCheckpointCompatibilityError(
                                "cannot resume retrieval: graph, question, prompt, schema or configuration changed"
                            )
                        if saved.values and not saved.next:
                            # Recall and relevance are the persisted model decisions.
                            # Rebuild only deterministic dependency closure on replay,
                            # so old incomplete projections can recover after a fix.
                            assembled = self.nodes.assemble(saved.values)
                            output = {**saved.values, **assembled}
                            if output["result"] != saved.values.get("result"):
                                await graph.aupdate_state(
                                    config, assembled, as_node="assemble"
                                )
                        else:
                            output = await graph.ainvoke(
                                None if saved.values else {"request": request},
                                config,
                                durability="sync",
                            )
        return RetrieveDbResult.model_validate(output["result"])


def context_fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()

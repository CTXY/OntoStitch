"""Explicitly import a reviewed node-stage checkpoint into a new run.

This is not an override of same-run version checks. Sources, actual proposal and
reviewed candidate are revalidated; subsequent graph edits are assessed normally.
"""

import json
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from cortex.capabilities.graph_merge.integration.compiler import compile_proposal
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.schema import GraphState


def load_reviewed_node_stage(directory, existing, incoming, schema):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
        saved = saver.get_tuple({"configurable": {"thread_id": manifest["run_id"]}})
    if saved is None:
        raise ValueError("No node-stage checkpoint")
    data = saved.checkpoint["channel_values"]
    request = data["request"]
    if (
        GraphState.model_validate(request["incoming"]) != incoming
        or GraphState.model_validate(request["existing"]) != existing
        or request["schema"] != schema.model_dump(mode="json")
    ):
        raise ValueError("Reviewed checkpoint has different graph inputs or schema")
    if (
        data.get("return_phase") not in {"consolidate", "reconcile"}
        or data.get("review", {}).get("verdict") != "accept"
    ):
        raise ValueError(
            "Only a checkpoint with an actual accepted node review may be reused as input to live reconciliation"
        )
    traces = [
        json.loads(l)
        for l in (directory / "agent_traces.jsonl").read_text().splitlines()
    ]
    accepted = [
        t
        for t in traces
        if t["component"] == "integration_node_assess"
        and (t.get("parsed_response") or {}).get("verdict") == "accept"
    ]
    if not accepted:
        raise ValueError("Missing actual independent node assessment trace")
    context = IntegrationContext(existing, incoming, schema)
    context.read_ids.update(data["read_ids"])
    preview = compile_proposal(
        context,
        IdentityDecision.model_validate(data["identity"]),
        IntegrationProposal.model_validate(data["proposal"]),
        strict=False,
    )
    if preview.state != GraphState.model_validate(data["preview"]):
        raise ValueError(
            "Saved node assessment no longer matches the actual compiled candidate"
        )
    return {
        k: data[k]
        for k in ["identity", "proposal", "preview", "review", "read_ids", "evidence"]
    } | {
        "origin": str(directory.resolve()),
        "run_id": manifest["run_id"],
        "reviewed_node_ids": accepted[-1]["payload"].get("reviewed_node_ids", []),
    }

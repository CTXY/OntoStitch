"""Domain inputs/outputs alongside native LangGraph, model and tool traces."""

import json
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

from langsmith import trace

from cortex.infrastructure.observability import native_tracing


class ExtractTracer:
    def __init__(self, *, client=None, enabled=None):
        self.client, self.enabled = client, enabled

    @contextmanager
    def span(self, name, *, inputs=None, **metadata):
        started = perf_counter()
        report = dict(metadata)
        with (
            native_tracing(client=self.client, enabled=self.enabled),
            trace(name, inputs=inputs or {}, metadata=metadata) as run,
        ):
            try:
                yield report
            except BaseException as exc:
                report.update(status="error", error_type=type(exc).__name__)
                raise
            finally:
                outputs = report.pop("outputs", None)
                report.setdefault("status", "ok")
                report["duration_seconds"] = perf_counter() - started
                run.metadata.update(report)
                run.end(outputs=outputs if outputs is not None else dict(report))


def case_payload(runtime, state, *, stage=None):
    """Hydrate only relevant immutable evidence; never copy it into checkpoints."""
    payload = {"state": state.model_dump(mode="json")}
    if stage is None:
        payload["source"] = runtime.record
    snapshot = runtime.graph.read(
        state.accepted_revision
        if stage in {"validate_final", "seal"}
        else state.working_revision
    )
    payload["ontology"] = {
        "revision": snapshot.revision,
        "content_fingerprint": snapshot.content_fingerprint,
        "semantic_nodes": [a.model_dump(mode="json") for a in snapshot.semantic_assets],
        "physical_node_count": len(snapshot.physical_assets),
    }
    refs = {
        "validation": state.validation_ref,
        "diagnosis": state.diagnosis_ref,
        "assessment": state.assessment_ref,
        "episode": state.episode_result_ref,
    }
    selected = {
        "direct": ["episode"],
        "repair": ["episode"],
        "diagnose": ["diagnosis"],
        "assess": ["assessment"],
        "accept": ["assessment"],
        "select": ["assessment"],
    }.get(
        stage,
        list(refs)[:3]
        if stage is None
        else (["validation"] if stage.startswith("validate") else []),
    )
    payload["evidence"] = {
        k: runtime.evidence.read(refs[k])["payload"] for k in selected if refs[k]
    }
    if stage == "accept":
        payload["evidence"]["acceptance"] = [
            runtime.evidence.read(ref)["payload"] for ref in state.acceptance_refs[-1:]
        ]
    if stage == "seal" and state.result_ref:
        payload["result"] = json.loads(Path(state.result_ref).read_text())
    return payload

"""The single case workflow; only LangGraph owns routing and execution position."""

from langgraph.graph import END, START, StateGraph

from cortex.infrastructure.progress import emit_progress

from cortex.capabilities.sql_training.training.observability import (
    ExtractTracer,
    case_payload,
)
from cortex.capabilities.sql_training.training.workflow.nodes import CaseRuntime
from cortex.capabilities.sql_training.training.workflow.state import CaseState


def build_case_graph(runtime: CaseRuntime, checkpointer, *, skip_direct=False):
    builder = StateGraph(CaseState)

    def node(name):
        def execute(state: CaseState):
            emit_progress("case_step", source_id=str(runtime.record.get("source_id", state.case_id)), step=name)
            runtime.graph.restore(state.working_revision, state.accepted_revision)
            if name == "seal":
                runtime.close_resources()

            executed = False

            def action():
                nonlocal executed
                executed = True
                updated = state.model_copy(deep=True)
                getattr(runtime, name)(updated)
                updated.working_revision = runtime.graph.working_revision
                updated.accepted_revision = runtime.graph.accepted_revision
                return updated

            with ExtractTracer().span(
                "extract." + name,
                inputs=case_payload(runtime, state),
                case_id=state.case_id,
                round=state.round_number,
                iteration=state.iteration,
                context_id=state.context_id,
                revision=state.working_revision,
            ) as summary:
                result = CaseState.model_validate(
                    runtime.evidence.completed(
                        name,
                        state.model_dump(mode="json"),
                        lambda: action().model_dump(mode="json"),
                    )
                )
                runtime.graph.restore(result.working_revision, result.accepted_revision)
                summary.update(
                    revision=result.working_revision,
                    evidence_ref=result.validation_ref,
                    learning_model_calls=result.model_calls - state.model_calls
                    if executed
                    else 0,
                    tool_calls=result.tool_calls - state.tool_calls if executed else 0,
                    database_calls=result.database_calls - state.database_calls
                    if executed
                    else 0,
                    completion_reason=result.completion_reason,
                    replayed=not executed,
                    validation_reused=bool(
                        name.startswith("validate")
                        and result.validation_ref
                        and result.validation_ref == state.validation_ref
                    ),
                    outputs=case_payload(runtime, result, stage=name),
                )
                if result.validation_ref:
                    validation = runtime.services.replay(result.validation_ref)
                    summary.update(
                        validation_correct=validation.correct,
                        validation_status=validation.status.value,
                        replay_attempts=len(validation.attempts),
                    )
                return result.model_dump(mode="json")

        return execute

    for name in (
        "direct",
        "validate_initial",
        "diagnose",
        "repair",
        "validate_trial",
        "assess",
        "select",
        "validate_selected",
        "accept",
        "retain",
        "validate_final",
        "seal",
    ):
        builder.add_node(name, node(name))
    builder.add_edge(START, "validate_initial" if skip_direct else "direct")
    builder.add_conditional_edges(
        "direct",
        lambda s: "validate_initial" if s.direct_completed else "retain",
        ["validate_initial", "retain"],
    )
    builder.add_conditional_edges(
        "validate_initial",
        lambda s: (
            "retain"
            if s.stop_case
            else (
                "diagnose"
                if not runtime.correct(s)
                else "assess"
                if s.groups
                else "retain"
            )
        ),
        ["retain", "diagnose", "assess"],
    )
    builder.add_conditional_edges(
        "diagnose", lambda s: "repair" if s.targets else "retain", ["repair", "retain"]
    )
    builder.add_conditional_edges(
        "repair",
        lambda s: "validate_trial" if s.candidate_ready else "retain",
        ["validate_trial", "retain"],
    )
    builder.add_conditional_edges(
        "validate_trial",
        lambda s: "retain" if s.stop_case else "assess",
        ["retain", "assess"],
    )
    builder.add_edge("assess", "select")
    builder.add_conditional_edges(
        "select",
        lambda s: (
            "validate_selected" if s.working_revision != s.trial_revision else "accept"
        ),
        ["validate_selected", "accept"],
    )
    builder.add_conditional_edges(
        "validate_selected",
        lambda s: "retain" if s.stop_case else "assess",
        ["retain", "assess"],
    )
    builder.add_conditional_edges(
        "accept",
        lambda s: "retain" if runtime.round_finished(s) else "repair",
        ["retain", "repair"],
    )
    builder.add_edge("retain", "validate_final")
    builder.add_conditional_edges(
        "validate_final",
        lambda s: (
            "seal"
            if runtime.correct(s) or s.stop_case or s.round_number >= runtime.max_rounds
            else "diagnose"
        ),
        ["seal", "diagnose"],
    )
    builder.add_edge("seal", END)
    return builder.compile(checkpointer=checkpointer)

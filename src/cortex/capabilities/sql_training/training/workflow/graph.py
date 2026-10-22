"""The single case workflow; only LangGraph owns routing and execution position."""

from langgraph.graph import END, START, StateGraph

from cortex.capabilities.sql_training.training.observability import SummaryTracer
from cortex.capabilities.sql_training.training.workflow.nodes import CaseRuntime
from cortex.capabilities.sql_training.training.workflow.state import CaseState


def build_case_graph(runtime: CaseRuntime, checkpointer, *, skip_direct=False):
    builder = StateGraph(CaseState)

    def node(name):
        def execute(state: CaseState):
            runtime.graph.restore(state.working_revision, state.accepted_revision)
            if name == "seal":
                runtime.close_resources()

            def action():
                updated = state.model_copy(deep=True)
                getattr(runtime, name)(updated)
                updated.working_revision = runtime.graph.working_revision
                updated.accepted_revision = runtime.graph.accepted_revision
                return updated

            with SummaryTracer().span(
                "extract." + name,
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
                    revision=result.working_revision, evidence_ref=result.validation_ref
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
        lambda s: "retain" if runtime.correct(s) or s.stop_case else "diagnose",
        ["retain", "diagnose"],
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
    builder.add_edge("assess", "accept")
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

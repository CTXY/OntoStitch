"""Only LangGraph owns clarification routing and execution position."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from cortex.application.online_context import (
    ChoicePoint,
    InteractionAnswer,
    InteractionQuestion,
)
from cortex.application.retrieve_db import RetrieveDbResult
from cortex.capabilities.online_context.workflow.state import OnlineState


def build_online_graph(engine, saver, *, run_id, identity):
    # Import after engine initialization to keep workflow/engine dependencies acyclic.
    from cortex.capabilities.online_context.engine import _questions

    def retrieval(state):
        return RetrieveDbResult.model_validate(state["retrieval"])

    def points(state):
        return tuple(ChoicePoint.model_validate(p) for p in state.get("points", ()))

    def answers(state):
        return tuple(
            InteractionAnswer.model_validate(a) for a in state.get("answers", ())
        )

    async def retrieve(state):
        result = await engine._retrieve(state["request"], run_id)
        return {
            "retrieval": result.model_dump(mode="json", exclude={"relevant_graph"}),
            "answers": [],
            "remaining_budget": state["request"]["question_budget"],
        }

    async def discover(state):
        result = retrieval(state)
        if result.retrieved_graph_state is None:
            return {"points": [], "notes": []}
        choices, notes = await engine._discover(state["request"], result)
        return {
            "points": [p.model_dump(mode="json") for p in choices],
            "notes": list(notes),
        }

    def plan(state):
        prior = answers(state)
        answered = {a.question_id for a in prior}
        remaining = [
            p for p in points(state) if f"question:{p.choice_point_id}" not in answered
        ]
        budget = state["remaining_budget"]
        limit = (
            engine._questions_per_round
            if budget is None
            else min(engine._questions_per_round, budget)
        )
        questions = _questions(remaining[:limit])
        if not questions:
            return {"questions": []}
        left = None if budget is None else budget - len(questions)
        result = engine._pending_result(
            state["request"],
            retrieval(state),
            points(state),
            questions,
            prior,
            left,
            run_id,
            identity,
            tuple(state["notes"]),
        )
        return {
            "questions": [q.model_dump(mode="json") for q in questions],
            "remaining_budget": left,
            "result": result.model_dump(mode="json"),
        }

    def await_answers(state):
        # No model calls or writes before interrupt: this node restarts on resume.
        supplied = interrupt({"run_id": run_id, "questions": state["questions"]})
        parsed = tuple(InteractionAnswer.model_validate(a) for a in supplied)
        from cortex.application.online_context import _validate_answers

        _validate_answers(
            tuple(InteractionQuestion.model_validate(q) for q in state["questions"]),
            parsed,
        )
        return {
            "answers": [
                *state["answers"],
                *[a.model_dump(mode="json") for a in parsed],
            ],
            "questions": [],
        }

    async def finish(state):
        result = await engine._finish(
            state["request"],
            retrieval(state),
            points(state),
            answers(state),
            tuple(state["notes"]),
        )
        return {
            "result": result.model_copy(update={"run_id": run_id}).model_dump(
                mode="json"
            )
        }

    graph = StateGraph(OnlineState)
    for name, node in (
        ("retrieve", retrieve),
        ("discover", discover),
        ("plan_questions", plan),
        ("await_answers", await_answers),
        ("finalize", finish),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "discover")
    graph.add_edge("discover", "plan_questions")
    graph.add_conditional_edges(
        "plan_questions", lambda s: "await_answers" if s["questions"] else "finalize"
    )
    graph.add_edge("await_answers", "plan_questions")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=saver, name="online_context")

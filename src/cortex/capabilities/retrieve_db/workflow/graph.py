"""The shared retrieval graph: signals, recall, selection, dependency closure."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class RetrievalState(TypedDict, total=False):
    request: dict
    initial_sql: str | None
    representations: list[dict]
    relevance: list[dict]
    result: dict


def build_retrieval_graph(runtime, checkpointer=None):
    graph = StateGraph(RetrievalState)
    for name in ("signals", "recall", "select", "assemble"):
        graph.add_node(name, getattr(runtime, name))
    graph.add_edge(START, "signals")
    graph.add_edge("signals", "recall")
    graph.add_edge("recall", "select")
    graph.add_edge("select", "assemble")
    graph.add_edge("assemble", END)
    return graph.compile(checkpointer=checkpointer, name="retrieve_db")

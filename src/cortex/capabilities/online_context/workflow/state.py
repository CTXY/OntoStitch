from typing import TypedDict


class OnlineState(TypedDict, total=False):
    request: dict
    identity: str
    retrieval: dict
    points: list[dict]
    notes: list[str]
    answers: list[dict]
    remaining_budget: int | None
    questions: list[dict]
    result: dict

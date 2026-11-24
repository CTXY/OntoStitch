from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import SecretStr

from cortex.bootstrap.online_context import build_online_context_service
from cortex.config import load_config
from cortex.capabilities.retrieve_db.retrieval.selection import KeepRecalledSelector


def test_online_has_no_model_relevance_filter_and_transport_does_not_change_identity(tmp_path):
    cfg = load_config("configs/cortex.yaml")
    model = FakeListChatModel(responses=[])
    first = build_online_context_service(config=cfg, llm=model, embeddings=None)
    changed = cfg.model_copy(update={
        "llm": cfg.llm.model_copy(update={"base_url": "https://other.invalid", "api_key": SecretStr("unused"), "max_retries": 0, "timeout": 12}),
        "services": cfg.services.model_copy(update={"retrieve_db": cfg.services.retrieve_db.model_copy(update={"workspace": tmp_path / "other-cache"})}),
    })
    second = build_online_context_service(config=changed, llm=model, embeddings=None)
    assert isinstance(first._engine._retrieval._engine._workflow.nodes.selector, KeepRecalledSelector)
    assert first._engine._workflow.identity == second._engine._workflow.identity
    semantic = changed.model_copy(update={"llm": changed.llm.model_copy(update={"model": "different-model"})})
    third = build_online_context_service(config=semantic, llm=model, embeddings=None)
    assert third._engine._workflow.identity != second._engine._workflow.identity

"""Official file-backed SQLite checkpoints, owned by each merge invocation."""

from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import SecretStr


@contextmanager
def persistent_checkpointer(url: SecretStr):
    value = url.get_secret_value()
    if not value.startswith("sqlite:///") or value.removeprefix("sqlite:///") in (
        "",
        ":memory:",
    ):
        raise ValueError("Graph Merge requires a file-backed sqlite:/// checkpoint URL")
    path = Path(value.removeprefix("sqlite:///")).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        saver.setup()
        yield saver

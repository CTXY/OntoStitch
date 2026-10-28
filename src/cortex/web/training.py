"""Run the real CLI in an owned subprocess and consume its progress protocol."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import dotenv_values

from cortex.web.graphs import load_graph

ROOT = Path(
    os.environ.get("CORTEX_PROJECT_ROOT", Path(__file__).resolve().parents[3])
).resolve()


def training_environment():
    env = os.environ.copy()
    for key, value in dotenv_values(
        env.get("CORTEX_WEB_ENV_FILE", ROOT / ".env")
    ).items():
        if value is not None:
            env.setdefault(key, value)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def check_training_configuration():
    env = training_environment()
    missing = [
        key
        for key in ("CORTEX_LLM_MODEL", "CORTEX_LLM_BASE_URL", "CORTEX_LLM_API_KEY")
        if not env.get(key)
    ]
    if missing:
        raise ValueError("Missing training configuration: " + ", ".join(missing))
    checkpoint = env.get("CORTEX_WEB_CHECKPOINT_URL") or env.get(
        "GRAPH_MERGE_CHECKPOINT_URL"
    )
    if checkpoint:
        from cortex.config.models import GraphMergeConfig

        try:
            GraphMergeConfig(checkpoint_url=checkpoint)
        except ValueError:
            raise ValueError(
                "Graph Merge checkpoint must use a file-backed sqlite:/// URL"
            ) from None
    return env


class CliTrainingRunner:
    """No archived result fallback. Only this invocation's validated graph is published."""

    def __init__(self, storage: Path):
        self.storage = storage.resolve()

    async def run(self, run: dict, pairs: list[dict], database: Path, update):
        env = check_training_configuration()
        root = self.storage / "runs" / run["id"]
        root.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        completed, active = {}, set()

        def publish(**fields):
            fields.setdefault("elapsed_seconds", round(time.monotonic() - started, 1))
            fields.setdefault("updated_at", datetime.now(UTC).isoformat())
            phase = fields.get("phase", run.get("phase"))
            status = fields.get("status", run.get("status"))
            if (
                status == "running"
                and run.get("eta_case_seconds")
                and phase
                in {
                    "snapshotting",
                    "initializing",
                    "training",
                    "introspecting",
                    "learning",
                }
            ):
                remaining = run["eta_case_seconds"] * len(pairs) - (
                    time.monotonic() - started
                )
                fields["eta_seconds"] = round(remaining) if remaining > 0 else None
                fields["eta_basis"] = (
                    "historical_completed_runs"
                    if remaining > 0
                    else "historical_estimate_exceeded"
                )
            update(**fields)

        publish(
            status="running",
            phase="snapshotting",
            eta_seconds=None,
            eta_basis="insufficient_history",
        )
        snapshot = root / "database.sqlite"

        def backup():
            source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()

        await asyncio.to_thread(backup)
        # mode=ro propagates to every SQLAlchemy engine, including case validation tools.
        config_path = Path(
            os.getenv("CORTEX_WEB_TRAINING_CONFIG", ROOT / "configs/cortex.yaml")
        )
        config = yaml.safe_load(config_path.read_text())
        config["database"] = {
            "type": "sqlite",
            "path": str(snapshot),
            "read_only": True,
        }
        services = config.setdefault("services", {})
        train = services.setdefault("sql_training", {})
        train["case_max_workers"] = 1
        train.setdefault("validation", {})["dialect"] = "sqlite"
        # Reuse the selected model/embedding configuration and all learning/replay gates.
        checkpoint = env.get("CORTEX_WEB_CHECKPOINT_URL") or env.get(
            "GRAPH_MERGE_CHECKPOINT_URL"
        )
        # The web runner explicitly selects a run-local, durable SQLite saver unless
        # a SQLite checkpoint file was configured. This is recorded in task configuration.
        services.setdefault("graph_merge", {})["checkpoint_url"] = (
            checkpoint or f"sqlite:///{root / 'merge-checkpoints.sqlite'}"
        )
        # Pass optional checkpoint configuration through the subprocess environment.
        if checkpoint:
            checkpoint_path = Path(checkpoint.removeprefix("sqlite:///")).expanduser()
            if not checkpoint_path.is_absolute():
                checkpoint_path = ROOT / checkpoint_path
            env["GRAPH_MERGE_CHECKPOINT_URL"] = "sqlite:///" + str(
                checkpoint_path.resolve()
            )
            services["graph_merge"]["checkpoint_url"] = "${GRAPH_MERGE_CHECKPOINT_URL}"
        task_config = root / "config.yaml"
        task_config.write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
        )
        training_path = root / "pairs.jsonl"
        training_path.write_text(
            "".join(
                json.dumps(
                    {
                        key: pair[key]
                        for key in ("source_id", "question", "sql", "evidence")
                        if key in pair
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for pair in pairs
            ),
            encoding="utf-8",
        )
        events_path, result_path = root / "progress.jsonl", root / "cli-result.json"
        command = [
            sys.executable,
            "-m",
            "cortex.cli.main",
            "sql-train",
            "--config",
            str(task_config),
            "--training-set",
            str(training_path),
            "--output",
            str(root / "output"),
            "--progress-file",
            str(events_path),
            "--result-file",
            str(result_path),
        ]
        (root / "command.json").write_text(
            json.dumps(command, indent=2), encoding="utf-8"
        )
        process = None
        offset, pending = 0, b""

        def consume():
            nonlocal offset, pending
            if not events_path.exists():
                return
            with events_path.open("rb") as stream:
                stream.seek(offset)
                pending += stream.read()
                offset = stream.tell()
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                event = json.loads(line)
                fields = {"last_event": event, "progress_sequence": event["sequence"]}
                if event["event"] == "phase":
                    fields.update(
                        phase=event["phase"],
                        current_step=None,
                        eta_seconds=None,
                        eta_basis="insufficient_history",
                    )
                elif event["event"] == "case_started":
                    active.add(event["source_id"])
                    fields.update(
                        phase="learning",
                        current_case=event["source_id"],
                        current_step="direct",
                        pairs_running=len(active),
                    )
                elif event["event"] == "case_step":
                    fields.update(current_step=event["step"])
                elif event["event"] == "case_completed":
                    completed[event["source_id"]] = event
                    active.discard(event["source_id"])
                    fields.update(
                        pairs_completed=len(completed),
                        pairs_running=len(active),
                        passed=sum(v["passed"] for v in completed.values()),
                        failed=sum(not v["passed"] for v in completed.values()),
                        cases=list(completed.values()),
                    )
                    # Case throughput estimates cannot predict unknown merge duration.
                    if len(completed) >= 2 and len(completed) < len(pairs):
                        fields.update(
                            eta_seconds=round(
                                (time.monotonic() - started)
                                / len(completed)
                                * (len(pairs) - len(completed))
                            ),
                            eta_basis="case_throughput_learning_only",
                        )
                elif event["event"] == "merge_progress":
                    fields.update(
                        phase="merging", merge_groups_remaining=event["remaining"]
                    )
                publish(**fields)

        try:
            with (root / "process.log").open("w", encoding="utf-8") as log:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                publish(phase="initializing", process_id=process.pid)
                while process.returncode is None:
                    consume()
                    publish()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.8)
                    except TimeoutError:
                        continue
                consume()
            if process.returncode not in (0, 2) or not result_path.is_file():
                raise RuntimeError(
                    f"cortex sql-train exited with code {process.returncode}; see runs/{run['id']}/process.log"
                )
            summary = json.loads(result_path.read_text())
            if summary["status"] not in {"completed", "needs_review"}:
                raise RuntimeError("Training failed; no graph was published")
            output = Path(summary["output"]).resolve()
            if not output.is_relative_to(root):
                raise RuntimeError("CLI returned an output outside this run")
            graph_path = output / "graph_state.json"
            graph = await asyncio.to_thread(
                load_graph, json.loads(graph_path.read_text())
            )
            report_path = root / "graph.json"
            report_path.write_text(
                json.dumps(
                    {
                        **graph,
                        "origin": "sql_train",
                        "run_id": run["id"],
                        "database_id": run["database_id"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            publish(
                status=summary["status"],
                phase=summary["status"],
                eta_seconds=0,
                pairs_running=0,
                passed=summary["passed"],
                failed=summary["failed"],
                result={
                    "nodes": len(graph["state"]["graph"]["nodes"]),
                    "edges": len(graph["state"]["graph"]["edges"]),
                    "conflicts": sum(n.get("merge_status") == "conflict" for n in graph["state"]["graph"]["nodes"]),
                    "origin": "sql_train",
                    "graph_url": f"/ontology/api/runs/{run['id']}/graph",
                    "note": "Generated by this cortex sql-train invocation from the selected cases.",
                    "exit_code": process.returncode,
                },
            )
        except asyncio.CancelledError:
            if process and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=8)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            publish(
                status="interrupted",
                phase="interrupted",
                eta_seconds=None,
                pairs_running=0,
                error="Training service stopped. This invocation was terminated; start a new run.",
            )
            raise

    def graph(self, run_id: str):
        return json.loads((self.storage / "runs" / run_id / "graph.json").read_text())

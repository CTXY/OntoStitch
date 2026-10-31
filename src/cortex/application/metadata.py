"""Three stages: database profiling, file interpretation, optional DB enrichment."""

from collections.abc import Callable, Iterable
from pathlib import Path

from cortex.capabilities.metadata.agent import MetadataAgent
from cortex.capabilities.metadata.loading import MetadataLoader
from cortex.capabilities.metadata.management import (
    MetadataChange,
    RelationProposal,
    add_relation,
    apply_changes,
)
from cortex.capabilities.metadata.models import (
    BuildOptions,
    MetadataSnapshot,
    SourceRecord,
)
from cortex.capabilities.metadata.ports import (
    MetadataSource,
    SnapshotRepository,
)
from cortex.capabilities.metadata.run import AgentExecutionError, AgentResult
from cortex.capabilities.metadata.views import MetadataReader


class MetadataService:
    def __init__(
        self,
        repository: SnapshotRepository,
        source_factory: Callable[[str | Path, BuildOptions], MetadataSource],
        read_files: Callable[[Iterable[str | Path]], list[SourceRecord]],
        loader: MetadataLoader | None,
        agent: MetadataAgent | None = None,
        default_options: BuildOptions | None = None,
        file_agent: MetadataAgent | None = None,
    ):
        if loader is not None and file_agent is not None:
            raise ValueError("Choose a dataset loader or file Agent, not both")
        self.runs = []
        self.repository = repository
        self.source_factory, self.read_files = source_factory, read_files
        self.loader, self.agent = loader, agent
        self.file_agent = file_agent
        self.default_options = default_options or BuildOptions()

    def build(
        self,
        database,
        *,
        initial_files=(),
        options: BuildOptions | None = None,
        expected_revision: int = 0,
    ) -> MetadataSnapshot:
        self.runs = []
        self.repository.check_source(database)
        options = options or self.default_options
        source = self.source_factory(database, options)
        try:
            # Stage 1: database DDL/profiling only.
            snapshot = source.scan()
            # Stage 2: file import, with optional file-only interpretation.
            loader_run = None
            files = self.read_files(initial_files)
            snapshot.sources.extend(files)
            if self.loader is not None:
                for file in files:
                    try:
                        loaded = self.loader.extract(snapshot, file)
                    except AgentExecutionError as exc:
                        if loader_run is not None:
                            exc.result.trace = loader_run.trace + exc.result.trace
                            self.runs.remove(("files", loader_run))
                        self.runs.append(("files", exc.result))
                        raise
                    if loaded.trace or loaded.status != "completed":
                        if loader_run is None:
                            loader_run = AgentResult(snapshot)
                            self.runs.append(("files", loader_run))
                        loader_run.trace.extend(
                            dict(entry, file=file.locator) for entry in loaded.trace
                        )
                        if loaded.status != "completed":
                            loader_run.status = "incomplete"
                    snapshot.issues.extend(
                        f"{file.id}: {issue}" for issue in loaded.issues
                    )
                    if not loaded.supported:
                        if not loaded.issues:
                            snapshot.issues.append(
                                f"{file.id}: unsupported file format"
                            )
                        continue
                    for batch in loaded.batches:
                        for change in batch:
                            change.source_refs = list(
                                dict.fromkeys([file.id, *change.source_refs])
                            )
                    changes = [change for batch in loaded.batches for change in batch]
                    if not changes:
                        continue
                    try:
                        snapshot = apply_changes(snapshot, changes)
                    except ValueError:
                        # Fast path validates once per file. On conflict, isolate records,
                        # preserving atomicity within each record and all valid records.
                        for number, batch in enumerate(loaded.batches, 1):
                            try:
                                snapshot = apply_changes(snapshot, batch)
                            except ValueError as exc:
                                snapshot.issues.append(
                                    f"{file.id}, record {number}: {exc}"
                                )
            if loader_run is not None:
                loader_run.snapshot = snapshot
            # Autonomous file discovery still belongs to stage 2.
            # Database exploration/enrichment is stage 3 and independently enabled.
            for stage, runner in [
                ("files", self.file_agent if files else None),
                ("enrich", self.agent),
            ]:
                if runner is not None:
                    try:
                        result = runner.run(snapshot, source, options=options)
                    except AgentExecutionError as exc:
                        self.runs.append((stage, exc.result))
                        raise
                    self.runs.append((stage, result))
                    snapshot = result.snapshot
            return self.repository.save(snapshot, expected_revision=expected_revision)
        finally:
            source.close()

    def load_snapshot(self, revision=None):
        return self.repository.load(revision)

    def update(self, changes: list[MetadataChange], *, expected_revision: int):
        snapshot = self.repository.load(expected_revision)
        return self.repository.save(
            apply_changes(snapshot, changes), expected_revision=expected_revision
        )

    def schema(self, kind="column"):
        return MetadataReader.schema(kind)

    def inspect(self, object_id, *, revision=None, offset=0, limit=20):
        return MetadataReader(self.repository.load(revision)).inspect(
            object_id, offset=offset, limit=limit
        )

    def inspect_many(self, object_ids, *, revision=None, include_profiles=False):
        return MetadataReader(self.repository.load(revision)).inspect_many(
            object_ids, include_profiles=include_profiles
        )

    def list_metadata(
        self, *, revision=None, kind="table", table_id=None, offset=0, limit=20
    ):
        return MetadataReader(self.repository.load(revision)).list(
            kind=kind, table_id=table_id, offset=offset, limit=limit
        )

    def read_source(self, source_ref, *, revision=None, offset=0, limit=12000):
        return MetadataReader(self.repository.load(revision)).read_source(
            source_ref, offset=offset, limit=limit
        )

    def list_sources(self, *, revision=None, offset=0, limit=20):
        return MetadataReader(self.repository.load(revision)).list_sources(
            offset=offset, limit=limit
        )

    def export(self, revision=None):
        return self.repository.load(revision).public_dict()

    def propose_relation(self, proposal: RelationProposal, *, expected_revision: int):
        snapshot = self.repository.load(expected_revision)
        return self.repository.save(
            add_relation(snapshot, proposal), expected_revision=expected_revision
        )

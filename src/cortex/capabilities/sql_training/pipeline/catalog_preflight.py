"""Build and validate a shared physical catalog before dispatching case workers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy.engine import make_url

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.artifacts.jsonl import write_json_atomic
from cortex.capabilities.sql_training.builders import build_catalog_assets
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.nodes import build_graph
from cortex.capabilities.sql_training.pipeline.init import ScanCatalogPipeline


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _identity(config, schema, dimension_values_path):
    database = config.database
    url = make_url(database.url)
    identity = {
        'database': hashlib.sha256(database.url.encode()).hexdigest(),
        'include_tables': database.include_tables,
        'schema': hashlib.sha256(schema.model_dump_json().encode()).hexdigest(),
    }
    if url.drivername.startswith('sqlite') and url.database:
        from urllib.parse import unquote
        path = Path(unquote(url.database.removeprefix('file:'))).resolve()
        stat = path.stat()
        identity['file'] = [str(path), stat.st_size, stat.st_mtime_ns]
    descriptions = Path(database.description_path) if database.description_path else None
    identity['descriptions'] = {str(p.relative_to(descriptions)): _hash(p)
                                for p in sorted(descriptions.rglob('*')) if p.is_file()} if descriptions else {}
    identity['dimension_values'] = _hash(dimension_values_path) if dimension_values_path else None
    return identity


def validate_prepared_files(root):
    """A manifest is published only after a complete successful preflight."""
    root = Path(root)
    manifest = json.loads((root / 'catalog_manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if _hash(root / name) != expected:
            raise ValueError(f'Prepared physical catalog changed: {name}')
    return manifest


def prepare_physical_catalog(config, database, schema, root, *, dimension_values_path=None):
    """No QA/SQL or model calls; failed validation dispatches no case workers."""
    import fcntl
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.catalog.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = _identity(config, schema, dimension_values_path)
        marker = root / 'catalog_manifest.json'
        if marker.exists():
            manifest = validate_prepared_files(root)
            if manifest['identity'] != identity:
                raise ValueError('Prepared catalog scope/database changed; use a new catalog directory')
            return {**manifest, 'reused': True}
        # Catch structural errors before performing expensive row sampling.
        light = database.introspect_catalog(include_tables=config.database.include_tables, sample_values=False)
        assets = build_catalog_assets(light.tables, light.columns, light.joins,
                                      description_directory=config.database.description_path)
        build_graph([*assets.tables, *assets.columns], assets.join_edges, schema)
        prepared_config = config.model_copy(update={'workspace': str(root)})
        workspace = Workspace(root)
        workspace.init()
        summary = ScanCatalogPipeline(prepared_config, database=database).run()
        workspace.write_draft_assets(NodeType.DIMENSION_VALUE, [])
        if dimension_values_path:
            from cortex.capabilities.sql_training.pipeline.values import DimensionValuePipeline
            DimensionValuePipeline(prepared_config, database=database).run(dimension_values_path)
        graph = build_graph(workspace.read_all_draft_assets(), workspace.read_catalog_edges(), schema)
        if _identity(config, schema, dimension_values_path) != identity:
            raise ValueError('Database changed during catalog preparation; no catalog published')
        write_json_atomic(root / 'physical_graph.json', graph.model_dump(mode='json'))
        files = {str(p.relative_to(root)): _hash(p) for p in sorted(workspace.draft_dir.glob('*.json'))}
        files['physical_graph.json'] = _hash(root / 'physical_graph.json')
        manifest = {'identity': identity, 'files': files, 'summary': summary,
                    'nodes': len(graph.nodes), 'edges': len(graph.edges)}
        write_json_atomic(marker, manifest)
        return {**manifest, 'reused': False}

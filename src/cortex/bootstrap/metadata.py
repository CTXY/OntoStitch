"""Composition root for profiling, file interpretation and database enrichment."""

from pathlib import Path

from cortex.application.metadata import MetadataService
from cortex.capabilities.metadata.agent import MetadataAgent
from cortex.capabilities.metadata.loading import MetadataLoader
from cortex.capabilities.metadata.models import BuildOptions
from cortex.infrastructure.metadata.files import read_initial_files
from cortex.infrastructure.metadata.loaders import (
    BirdMetadataLoader,
    NormalizedMetadataLoader,
)
from cortex.infrastructure.metadata.repository import MetadataRepository
from cortex.infrastructure.metadata.sqlite_source import SQLiteMetadataSource


def build_metadata_service(
    workspace,
    *,
    model=None,
    options=None,
    loader=None,
    file_mode="loader",
    enrich_database=False,
):
    """loader: 'bird', 'normalized', or a custom MetadataLoader instance.

    file_mode='agent' interprets unknown file layouts without a deterministic loader.
    BIRD loader internally uses the supplied model to interpret its descriptions.
    enrich_database controls the separate database semantic enrichment pass.
    """
    options = options or BuildOptions()
    if file_mode not in {"loader", "agent"}:
        raise ValueError("file_mode must be loader or agent")
    if enrich_database and model is None:
        raise ValueError("enrich_database requires a model")
    if file_mode == "agent":
        if loader is not None:
            raise ValueError("file_mode=agent does not accept a dataset loader")
        if model is None:
            raise ValueError("file_mode=agent requires a model")
        selected = None
    elif loader is None or isinstance(loader, str):
        loaders = {"bird": BirdMetadataLoader, "normalized": NormalizedMetadataLoader}
        if (loader or "normalized") not in loaders:
            raise ValueError(f"Unknown metadata loader: {loader}")
        selected = (
            BirdMetadataLoader(model, options=options)
            if loader == "bird"
            else NormalizedMetadataLoader()
        )
    else:
        if not isinstance(loader, MetadataLoader):
            raise TypeError("loader must implement MetadataLoader")
        selected = loader
    return MetadataService(
        MetadataRepository(Path(workspace) / "metadata.sqlite"),
        SQLiteMetadataSource,
        read_initial_files,
        selected,
        MetadataAgent(model) if model is not None and enrich_database else None,
        default_options=options,
        file_agent=MetadataAgent(model, task="files") if file_mode == "agent" else None,
    )

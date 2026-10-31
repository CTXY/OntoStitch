"""Explicit dataset-specific metadata loaders."""

from .bird import BirdMetadataLoader
from .normalized import NormalizedMetadataLoader

__all__ = ["BirdMetadataLoader", "NormalizedMetadataLoader"]

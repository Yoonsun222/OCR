"""Interfaces for recorded token and synthetic geometry providers."""

from typing import Protocol

from .models import OcrToken, TargetRegion


class OcrProvider(Protocol):
    def read(self, artifact_path: str) -> tuple[OcrToken, ...]: ...


class GeometryProvider(Protocol):
    def read(self, artifact_path: str) -> tuple[TargetRegion, ...]: ...

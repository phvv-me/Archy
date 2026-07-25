import ast
from hashlib import sha256
from pathlib import Path

from pydantic import field_validator

from archy.engine.base import FrozenModel


class SourceUnit(FrozenModel):
    """One already-read Python source document supplied to the graph engine."""

    path: Path
    source: str
    tree: ast.Module | None = None

    @field_validator("path")
    @classmethod
    def relative_path(cls, path: Path) -> Path:
        """Require portable paths relative to the repository root."""
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Source paths must stay relative to the repository root")
        return path

    @property
    def content_hash(self) -> str:
        """Return the source digest used to detect stale evidence."""
        return sha256(self.source.encode()).hexdigest()

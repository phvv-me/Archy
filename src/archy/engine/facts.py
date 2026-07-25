from hashlib import blake2b
from pathlib import Path

from pydantic import Field, computed_field

from archy.engine.base import FrozenModel
from archy.engine.enums import DerivationKind, EdgeKind, NodeKind, ResolutionKind


class SourceSpan(FrozenModel):
    """Location of one graph fact in a source document."""

    path: Path
    line: int = Field(ge=1)
    column: int | None = Field(default=None, ge=0)
    end_line: int | None = Field(default=None, ge=1)
    end_column: int | None = Field(default=None, ge=0)


class Evidence(FrozenModel):
    """Provenance and confidence metadata for one graph fact."""

    span: SourceSpan
    source_hash: str
    provider: str
    provider_version: str
    derivation: DerivationKind
    resolution: ResolutionKind
    confidence: float = Field(ge=0.0, le=1.0)


class GraphNode(FrozenModel):
    """Stable entity in a repository graph."""

    kind: NodeKind
    qualname: str
    path: Path | None = None
    is_package: bool = False
    span: SourceSpan | None = None
    annotation: str | None = None
    return_annotation: str | None = None
    decorators: tuple[str, ...] = ()
    asynchronous: bool = False
    ordinal: int | None = Field(default=None, ge=0)

    @computed_field
    @property
    def id(self) -> str:
        """Return the semantic identity shared by every graph producer."""
        return f"python:{self.kind.value}:{self.qualname}"

    @property
    def name(self) -> str:
        """Return the concise display name for this entity."""
        if self.kind in {NodeKind.FILE, NodeKind.DIRECTORY}:
            return Path(self.qualname).name
        return self.qualname.rsplit(".", 1)[-1]

    @property
    def external(self) -> bool:
        """Return whether this node represents code outside the repository."""
        return self.kind in {NodeKind.EXTERNAL_MODULE, NodeKind.EXTERNAL_SYMBOL}


class GraphEdge(FrozenModel):
    """One source-site relationship between two stable graph nodes."""

    source: str
    target: str
    kind: EdgeKind
    evidence: Evidence

    @computed_field
    @property
    def id(self) -> str:
        """Return a stable identity for this exact source-site relation."""
        span = self.evidence.span
        value = (
            f"{self.source}\0{self.target}\0{self.kind.value}\0"
            f"{span.path}\0{span.line}\0{span.column}"
        ).encode()
        return blake2b(value, digest_size=12, person=b"archy-edge").hexdigest()

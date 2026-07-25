from archy.engine.base import FrozenModel
from archy.engine.enums import NodeKind
from archy.engine.facts import GraphNode
from archy.engine.source import SourceUnit


class PythonImports(FrozenModel):
    """Resolve Python module paths without importing analyzed project code."""

    modules: dict[str, tuple[GraphNode, SourceUnit]]

    def target(
        self,
        candidate: str,
    ) -> GraphNode:
        """Resolve the deepest internal module or one external package boundary."""
        if target := self.module_name(candidate):
            return self.modules[target][0]
        qualname = candidate.split(".")[0]
        return GraphNode(
            kind=NodeKind.EXTERNAL_MODULE,
            qualname=qualname,
        )

    def module_name(
        self,
        candidate: str,
    ) -> str | None:
        """Return the deepest module prefix of one qualified name."""
        parts = candidate.split(".")
        return next(
            (
                prefix
                for size in range(len(parts), 0, -1)
                if (prefix := ".".join(parts[:size])) in self.modules
            ),
            None,
        )

    def relative_module(self, source: GraphNode, module: str, level: int) -> str:
        """Resolve an imported module against its containing package."""
        if not level:
            return module
        parts = source.qualname.split(".")
        if not source.is_package:
            parts.pop()
        keep = max(0, len(parts) - level + 1)
        return ".".join((*parts[:keep], *module.split("."))) if module else ".".join(parts[:keep])

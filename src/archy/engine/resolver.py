import builtins
from collections.abc import Mapping, Sequence
from functools import cached_property

from archy.engine.base import FrozenModel
from archy.engine.enums import NodeKind, ResolutionKind
from archy.engine.facts import GraphEdge, GraphNode
from archy.engine.structure import SymbolReference


class ResolvedReference(FrozenModel):
    """One symbolic reference paired with its visible static target."""

    target: GraphNode

    @property
    def resolution(self) -> ResolutionKind:
        """Derive resolution certainty from the canonical target kind."""
        if self.target.kind is NodeKind.UNRESOLVED_SYMBOL:
            return ResolutionKind.UNRESOLVED
        return ResolutionKind.EXTERNAL if self.target.external else ResolutionKind.EXACT


class PythonSymbolResolver(FrozenModel):
    """Resolve callable and inheritance expressions without importing target code."""

    aliases: Mapping[str, Mapping[str, str]]
    symbols: Mapping[str, GraphNode]
    nodes: dict[str, GraphNode]

    @cached_property
    def qualname_index(self) -> dict[str, GraphNode]:
        """Index structural nodes once for all receiver type lookups."""
        return {node.qualname: node for node in self.nodes.values()}

    @cached_property
    def internal_modules(self) -> frozenset[str]:
        """Return internal module names used to distinguish external references."""
        return frozenset(
            node.qualname for node in self.symbols.values() if node.kind is NodeKind.MODULE
        )

    def resolve(
        self,
        reference: SymbolReference,
        inheritance: Sequence[GraphEdge],
    ) -> ResolvedReference:
        """Resolve an expression to an exact, external, or explicit unknown node."""
        parts = reference.expression.split(".")
        if not parts or not all(part.isidentifier() for part in parts):
            return ResolvedReference(target=self.unresolved(reference))
        aliases = self.aliases[reference.module]
        if parts[0] in {"self", "cls"} and reference.owner_class:
            candidates = [".".join((reference.owner_class, *parts[1:]))]
        elif parts[0] == "super" and reference.owner_class:
            candidates = self.super_candidates(reference.owner_class, parts[1:], inheritance)
        elif parts[0] in aliases:
            candidates = [".".join((aliases[parts[0]], *parts[1:]))]
        else:
            candidates = self.typed_candidates(reference, parts, aliases)
            candidates.extend(self.lexical_candidates(reference, parts))
        if target := next(
            (self.symbols[name] for name in candidates if name in self.symbols),
            None,
        ):
            return ResolvedReference(target=target)
        if len(parts) == 1 and hasattr(builtins, parts[0]):
            return ResolvedReference(target=self.external(f"builtins.{parts[0]}"))
        imported_roots = {value.split(".")[0] for value in aliases.values()}
        if (
            candidates
            and candidates[0].split(".")[0] in imported_roots
            and not any(
                candidates[0] == module or candidates[0].startswith(f"{module}.")
                for module in self.internal_modules
            )
        ):
            return ResolvedReference(target=self.external(candidates[0]))
        return ResolvedReference(target=self.unresolved(reference))

    def typed_candidates(
        self,
        reference: SymbolReference,
        parts: Sequence[str],
        aliases: Mapping[str, str],
    ) -> list[str]:
        """Resolve a member through a declared parameter, variable, or field type."""
        if len(parts) < 2:
            return []
        if parts[0] in {"self", "cls"} and reference.owner_class and len(parts) > 2:
            value_names = [f"{reference.owner_class}.{parts[1]}"]
            members = parts[2:]
        else:
            value_names = self.lexical_candidates(reference, parts[:1])
            members = parts[1:]
        values = [
            node
            for name in value_names
            if (node := self.qualname_index.get(name)) is not None and node.annotation is not None
        ]
        candidates: list[str] = []
        for value in values:
            annotation = value.annotation or ""
            if not all(part.isidentifier() for part in annotation.split(".")):
                continue
            target = aliases.get(annotation, f"{reference.module}.{annotation}")
            if target not in self.symbols and annotation in self.symbols:
                target = annotation
            candidates.append(".".join((target, *members)))
        return candidates

    def lexical_candidates(self, reference: SymbolReference, parts: Sequence[str]) -> list[str]:
        """Search the current scope and its lexical parents before the module."""
        scope = reference.scope.split(".")
        floor = len(reference.module.split("."))
        return [".".join((*scope[:size], *parts)) for size in range(len(scope), floor - 1, -1)]

    def super_candidates(
        self,
        owner_class: str,
        members: list[str],
        inheritance: Sequence[GraphEdge],
    ) -> list[str]:
        """Resolve a `super` member against exact direct base classes."""
        owner = next((node for node in self.nodes.values() if node.qualname == owner_class), None)
        if owner is None:
            return []
        return [
            ".".join((self.nodes[edge.target].qualname, *members))
            for edge in inheritance
            if edge.source == owner.id and edge.target in self.nodes
        ]

    def unresolved(self, reference: SymbolReference) -> GraphNode:
        """Return a stable visible node for one unresolved expression."""
        qualname = f"{reference.module}::{reference.expression}"
        node = GraphNode(
            kind=NodeKind.UNRESOLVED_SYMBOL,
            qualname=qualname,
            path=self.nodes[reference.source].path,
        )
        self.nodes.setdefault(node.id, node)
        return self.nodes[node.id]

    def external(self, qualname: str) -> GraphNode:
        """Return a stable visible external symbol node."""
        node = GraphNode(
            kind=NodeKind.EXTERNAL_SYMBOL,
            qualname=qualname,
        )
        self.nodes.setdefault(node.id, node)
        return self.nodes[node.id]

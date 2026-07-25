from functools import cached_property
from pathlib import Path

import networkx as nx

from archy.cycles import Cycle, find_cycles
from archy.engine.base import FrozenModel
from archy.engine.enums import EdgeKind, NodeKind
from archy.engine.facts import GraphEdge, GraphNode


class RepositoryGraph(FrozenModel):
    """Immutable repository facts with typed queries and hidden storage details."""

    root: Path
    schema_version: int = 1
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    parse_errors: tuple[str, ...] = ()

    @cached_property
    def node_index(self) -> dict[str, GraphNode]:
        """Index nodes by stable identity for repeated graph queries."""
        return {node.id: node for node in self.nodes}

    @cached_property
    def ownership(self) -> dict[str, tuple[str, ...]]:
        """Index lexical and filesystem children by their direct owner."""
        children: dict[str, list[str]] = {}
        for edge in self.edges:
            if edge.kind not in {EdgeKind.CONTAIN, EdgeKind.DEFINE}:
                continue
            children.setdefault(edge.source, []).append(edge.target)
        return {owner: tuple(items) for owner, items in children.items()}

    @cached_property
    def parents(self) -> dict[str, str]:
        """Index each structurally owned node by its direct owner."""
        return {
            edge.target: edge.source
            for edge in self.edges
            if edge.kind in {EdgeKind.CONTAIN, EdgeKind.DEFINE}
        }

    def cycles(self, kind: EdgeKind = EdgeKind.IMPORT) -> tuple[Cycle, ...]:
        """Return strongly connected components for one relationship kind."""
        graph = nx.DiGraph()
        graph.add_nodes_from(node.id for node in self.nodes if node.kind is NodeKind.MODULE)
        for edge in self.edges:
            if edge.kind is not kind or edge.target not in graph:
                continue
            line = edge.evidence.span.line
            lines = (*graph.get_edge_data(edge.source, edge.target, {}).get("lines", ()), line)
            graph.add_edge(edge.source, edge.target, lines=lines)
        return tuple(find_cycles(graph))

    def edges_between(
        self,
        source: str,
        target: str,
        kind: EdgeKind | None = None,
    ) -> tuple[GraphEdge, ...]:
        """Return source-site facts joining two nodes, optionally filtered by kind."""
        return tuple(
            edge
            for edge in self.edges
            if edge.source == source
            and edge.target == target
            and (kind is None or edge.kind is kind)
        )

    def relations(self, *kinds: EdgeKind) -> tuple[GraphEdge, ...]:
        """Return source-site edges from the requested relation families."""
        selected = set(kinds)
        return tuple(edge for edge in self.edges if not selected or edge.kind in selected)

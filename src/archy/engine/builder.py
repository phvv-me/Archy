import ast
from collections.abc import Iterable, Mapping
from operator import attrgetter
from pathlib import Path

from pydantic import Field

from archy import __version__
from archy.engine.base import FrozenModel
from archy.engine.enums import DerivationKind, EdgeKind, NodeKind, ResolutionKind
from archy.engine.facts import Evidence, GraphEdge, GraphNode, SourceSpan
from archy.engine.imports import PythonImports
from archy.engine.repository_graph import RepositoryGraph
from archy.engine.resolver import PythonSymbolResolver
from archy.engine.source import SourceUnit
from archy.engine.structure import (
    PendingRelation,
    PythonStructure,
    PythonStructureCollector,
)


class GraphTooLargeError(Exception):
    """Repository source count exceeds the configured graph safety ceiling."""

    def __init__(self, count: int, root: Path, limit: int) -> None:
        self.count = count
        self.root = root
        self.limit = limit
        super().__init__(f"Found {count:,} modules under {root} with a limit of {limit:,}")


class RepositoryGraphBuilder(FrozenModel):
    """Build a typed structural graph from already-read Python documents."""

    max_modules: int = Field(default=10_000, ge=0)
    extra_roots: tuple[Path, ...] = ()

    def build(self, root: Path, sources: Iterable[SourceUnit]) -> RepositoryGraph:
        """Extract repository, file, symbol, field, type, and call facts in one pass."""
        modules = self.modules(root, sources)
        if self.max_modules and len(modules) > self.max_modules:
            raise GraphTooLargeError(len(modules), root, self.max_modules)
        parsed, parse_errors = self.trees(modules)
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        self.workspace(root, modules, nodes, edges)
        structures: dict[str, PythonStructure] = {}
        for name, tree in parsed.items():
            module, document = modules[name]
            structure = PythonStructureCollector(module, document).collect(tree)
            structures[name] = structure
            nodes.update({node.id: node for node in structure.nodes})
            edges.extend(
                self.edge(relation, document, ResolutionKind.EXACT)
                for relation in structure.relations
            )
        symbols = {
            node.qualname: node
            for node in nodes.values()
            if node.kind
            not in {
                NodeKind.REPOSITORY,
                NodeKind.DIRECTORY,
                NodeKind.FILE,
                NodeKind.PARAMETER,
            }
        }
        importer = PythonImports(modules=modules)
        reexports = self.reexports(structures, modules, symbols, importer)
        aliases: dict[str, dict[str, str]] = {}
        for name, structure in structures.items():
            module, document = modules[name]
            imported, aliases[name] = self.imports(
                module,
                document,
                structure.imports,
                reexports,
                nodes,
                importer,
            )
            edges.extend(imported)
        inheritance = self.resolve_references(
            structures,
            aliases,
            symbols,
            modules,
            nodes,
            EdgeKind.INHERIT,
        )
        edges.extend(inheritance)
        edges.extend(
            self.resolve_references(
                structures,
                aliases,
                symbols,
                modules,
                nodes,
                EdgeKind.CALL,
                inheritance,
            )
        )
        return RepositoryGraph(
            root=root,
            schema_version=2,
            nodes=tuple(sorted(nodes.values(), key=attrgetter("id"))),
            edges=tuple(sorted(edges, key=attrgetter("id"))),
            parse_errors=tuple(sorted(parse_errors)),
        )

    def modules(
        self,
        root: Path,
        sources: Iterable[SourceUnit],
    ) -> dict[str, tuple[GraphNode, SourceUnit]]:
        """Assign stable importable identities to supplied source paths."""
        absolute = {root / source.path: source for source in sources}
        package_dirs = {path.parent for path in absolute if path.name == "__init__.py"}
        package_dirs.update(root / extra_root for extra_root in self.extra_roots)
        package_roots = sorted(
            package for package in package_dirs if package.parent not in package_dirs
        )
        modules: dict[str, tuple[GraphNode, SourceUnit]] = {}
        for path, document in sorted(absolute.items()):
            qualname = self.qualname(path, root, package_roots)
            modules[qualname] = (
                GraphNode(
                    kind=NodeKind.MODULE,
                    qualname=qualname,
                    path=document.path,
                    is_package=path.name == "__init__.py",
                    span=SourceSpan(path=document.path, line=1, column=0),
                ),
                document,
            )
        return modules

    def trees(
        self,
        modules: Mapping[str, tuple[GraphNode, SourceUnit]],
    ) -> tuple[dict[str, ast.Module], list[str]]:
        """Reuse caller syntax trees and retain bounded standalone parse failures."""
        parsed: dict[str, ast.Module] = {}
        errors: list[str] = []
        for name, (_, document) in modules.items():
            try:
                parsed[name] = document.tree or ast.parse(document.source)
            except SyntaxError:
                errors.append(name)
        return parsed, errors

    def workspace(
        self,
        root: Path,
        modules: Mapping[str, tuple[GraphNode, SourceUnit]],
        nodes: dict[str, GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Build repository, directory, file, module, and containment facts."""
        repository = GraphNode(
            kind=NodeKind.REPOSITORY,
            qualname=root.name,
        )
        nodes[repository.id] = repository
        connected: set[tuple[str, str]] = set()
        for module, document in modules.values():
            parent = repository
            directory = Path()
            for part in document.path.parent.parts:
                directory /= part
                folder = GraphNode(
                    kind=NodeKind.DIRECTORY,
                    qualname=directory.as_posix(),
                    path=directory,
                )
                nodes.setdefault(folder.id, folder)
                if (parent.id, folder.id) not in connected:
                    edges.append(self.exact_edge(parent, folder, EdgeKind.CONTAIN, document))
                    connected.add((parent.id, folder.id))
                parent = folder
            file = GraphNode(
                kind=NodeKind.FILE,
                qualname=document.path.as_posix(),
                path=document.path,
                span=SourceSpan(path=document.path, line=1, column=0),
            )
            nodes[file.id] = file
            nodes[module.id] = module
            edges.append(self.exact_edge(parent, file, EdgeKind.CONTAIN, document))
            edges.append(self.exact_edge(file, module, EdgeKind.DEFINE, document))

    def imports(
        self,
        source: GraphNode,
        document: SourceUnit,
        statements: Iterable[ast.Import | ast.ImportFrom],
        reexports: Mapping[str, Mapping[str, str]],
        nodes: dict[str, GraphNode],
        importer: PythonImports,
    ) -> tuple[list[GraphEdge], dict[str, str]]:
        """Extract module dependencies and bindings used by symbol resolution."""
        edges: list[GraphEdge] = []
        aliases: dict[str, str] = {}
        for statement in statements:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    target = importer.target(alias.name)
                    nodes.setdefault(target.id, target)
                    local = alias.asname or alias.name.split(".")[0]
                    aliases[local] = alias.name if alias.asname else local
                    edges.append(self.import_edge(source, target, statement, document))
            elif isinstance(statement, ast.ImportFrom):
                base = importer.relative_module(source, statement.module or "", statement.level)
                for alias in statement.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    binding = reexports.get(base, {}).get(alias.name, candidate)
                    aliases[alias.asname or alias.name] = binding
                    module_name = importer.module_name(binding) or base
                    target = importer.target(module_name)
                    nodes.setdefault(target.id, target)
                    edges.append(self.import_edge(source, target, statement, document))
        return edges, aliases

    def reexports(
        self,
        structures: Mapping[str, PythonStructure],
        modules: Mapping[str, tuple[GraphNode, SourceUnit]],
        symbols: Mapping[str, GraphNode],
        importer: PythonImports,
    ) -> dict[str, dict[str, str]]:
        """Map package public names to their defining modules or symbols."""
        maps: dict[str, dict[str, str]] = {}
        for package, structure in structures.items():
            source = modules[package][0]
            if not source.is_package:
                continue
            bindings: dict[str, str] = {}
            for statement in (
                item for item in structure.imports if isinstance(item, ast.ImportFrom)
            ):
                base = importer.relative_module(source, statement.module or "", statement.level)
                for alias in statement.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if candidate in symbols or importer.module_name(candidate):
                        bindings[alias.asname or alias.name] = candidate
            if bindings:
                maps[package] = bindings
        return maps

    def resolve_references(
        self,
        structures: Mapping[str, PythonStructure],
        aliases: Mapping[str, Mapping[str, str]],
        symbols: Mapping[str, GraphNode],
        modules: Mapping[str, tuple[GraphNode, SourceUnit]],
        nodes: dict[str, GraphNode],
        kind: EdgeKind,
        inheritance: list[GraphEdge] | None = None,
    ) -> list[GraphEdge]:
        """Resolve one relation family while preserving every unresolved source site."""
        edges: list[GraphEdge] = []
        resolver = PythonSymbolResolver(aliases=aliases, symbols=symbols, nodes=nodes)
        for module, structure in structures.items():
            document = modules[module][1]
            for item in (reference for reference in structure.references if reference.kind is kind):
                resolved = resolver.resolve(item, inheritance or [])
                target = resolved.target
                nodes.setdefault(target.id, target)
                actual = (
                    EdgeKind.INSTANTIATE
                    if kind is EdgeKind.CALL and target.kind is NodeKind.CLASS
                    else kind
                )
                relation = PendingRelation(
                    source=item.source,
                    target=target.id,
                    kind=actual,
                    span=item.span,
                )
                edges.append(self.edge(relation, document, resolved.resolution))
        return edges

    def exact_edge(
        self,
        source: GraphNode,
        target: GraphNode,
        kind: EdgeKind,
        document: SourceUnit,
    ) -> GraphEdge:
        """Build an exact structural edge at the start of its source file."""
        return self.edge(
            PendingRelation(
                source=source.id,
                target=target.id,
                kind=kind,
                span=SourceSpan(path=document.path, line=1),
            ),
            document,
            ResolutionKind.EXACT,
        )

    def import_edge(
        self,
        source: GraphNode,
        target: GraphNode,
        statement: ast.stmt,
        document: SourceUnit,
    ) -> GraphEdge:
        """Build one exact lexical import edge."""
        return self.edge(
            PendingRelation(
                source=source.id,
                target=target.id,
                kind=EdgeKind.IMPORT,
                span=SourceSpan(
                    path=document.path,
                    line=statement.lineno,
                    column=statement.col_offset,
                ),
            ),
            document,
            ResolutionKind.EXTERNAL if target.external else ResolutionKind.EXACT,
        )

    def edge(
        self,
        relation: PendingRelation,
        document: SourceUnit,
        resolution: ResolutionKind,
    ) -> GraphEdge:
        """Build one stable source-site relationship with complete provenance."""
        explicit = relation.kind in {
            EdgeKind.CONTAIN,
            EdgeKind.DEFINE,
            EdgeKind.IMPORT,
            EdgeKind.INHERIT,
        }
        return GraphEdge(
            source=relation.source,
            target=relation.target,
            kind=relation.kind,
            evidence=Evidence(
                span=relation.span,
                source_hash=document.content_hash,
                provider="archy-stdlib-ast",
                provider_version=__version__,
                derivation=DerivationKind.EXPLICIT if explicit else DerivationKind.INFERRED,
                resolution=resolution,
                confidence=1.0 if resolution is not ResolutionKind.UNRESOLVED else 0.0,
            ),
        )

    def qualname(self, path: Path, root: Path, package_roots: Iterable[Path]) -> str:
        """Return the longest applicable package or repository-relative module name."""
        candidates = [package for package in package_roots if path.is_relative_to(package)]
        base = (
            min(candidates, key=lambda package: len(package.parts)).parent if candidates else root
        )
        parts = list(path.relative_to(base).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

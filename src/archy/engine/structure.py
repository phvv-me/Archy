import ast
from collections.abc import Iterable

from archy.engine.base import FrozenModel
from archy.engine.enums import EdgeKind, NodeKind
from archy.engine.facts import GraphNode, SourceSpan
from archy.engine.source import SourceUnit


class PendingRelation(FrozenModel):
    """Exact relationship awaiting canonical evidence construction."""

    source: str
    target: str
    kind: EdgeKind
    span: SourceSpan


class SymbolReference(FrozenModel):
    """One symbolic relationship awaiting repository-wide resolution."""

    source: str
    expression: str
    module: str
    scope: str
    owner_class: str | None = None
    kind: EdgeKind
    span: SourceSpan


class PythonStructure(FrozenModel):
    """Definitions and unresolved relations extracted from one Python module."""

    nodes: tuple[GraphNode, ...]
    relations: tuple[PendingRelation, ...]
    references: tuple[SymbolReference, ...]
    imports: tuple[ast.Import | ast.ImportFrom, ...]


class PythonStructureCollector(ast.NodeVisitor):
    """Collect definitions, fields, signatures, and calls in one syntax traversal."""

    def __init__(self, module: GraphNode, document: SourceUnit) -> None:
        self.module = module
        self.document = document
        self.nodes: dict[str, GraphNode] = {}
        self.relations: list[PendingRelation] = []
        self.references: list[SymbolReference] = []
        self.imports: list[ast.Import | ast.ImportFrom] = []
        self.owners = [module]
        self.callers = [module]
        self.classes: list[GraphNode] = []

    def collect(self, tree: ast.Module) -> PythonStructure:
        """Return every structural fact found in the supplied module tree."""
        self.visit(tree)
        return PythonStructure(
            nodes=tuple(self.nodes.values()),
            relations=tuple(self.relations),
            references=tuple(self.references),
            imports=tuple(self.imports),
        )

    def visit_If(self, node: ast.If) -> None:
        """Exclude type-only definitions and calls from the runtime structure."""
        guarded = (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
            isinstance(node.test, ast.Attribute)
            and isinstance(node.test.value, ast.Name)
            and node.test.value.id == "typing"
            and node.test.attr == "TYPE_CHECKING"
        )
        if not guarded:
            self.visit(node.test)
        for statement in node.orelse if guarded else (*node.body, *node.orelse):
            self.visit(statement)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Record one class, its bases, decorators, fields, and nested definitions."""
        definition = self.definition(
            NodeKind.CLASS,
            node.name,
            node,
            decorators=tuple(self.expression(item) for item in node.decorator_list),
        )
        for base in node.bases:
            self.reference(definition, self.expression(base), EdgeKind.INHERIT, base)
        self.visit_expressions((*node.decorator_list, *node.keywords))
        self.owners.append(definition)
        self.classes.append(definition)
        for statement in node.body:
            self.visit(statement)
        self.classes.pop()
        self.owners.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Record one synchronous callable and traverse its executable body."""
        self.callable(node, asynchronous=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Record one asynchronous callable and traverse its executable body."""
        self.callable(node, asynchronous=True)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Record module, class, and instance fields introduced by assignment."""
        for target in node.targets:
            self.assignment(target, node, None)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Record an annotated field and visit its optional value."""
        self.assignment(node.target, node, self.expression(node.annotation))
        if node.value is not None:
            self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        """Preserve every call site, including unresolved and dynamic targets."""
        self.reference(self.callers[-1], self.expression(node.func), EdgeKind.CALL, node.func)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        """Retain one runtime import for dependency and alias resolution."""
        self.imports.append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Retain one runtime from-import for dependency and alias resolution."""
        self.imports.append(node)

    def callable(self, node: ast.FunctionDef | ast.AsyncFunctionDef, asynchronous: bool) -> None:
        """Record one function or method with parameters and return type."""
        owner = self.owners[-1]
        decorators = tuple(self.expression(item) for item in node.decorator_list)
        kind = (
            NodeKind.PROPERTY
            if owner.kind is NodeKind.CLASS
            and any(
                item.rsplit(".", 1)[-1] in {"property", "cached_property"} for item in decorators
            )
            else NodeKind.METHOD
            if owner.kind is NodeKind.CLASS
            else NodeKind.FUNCTION
        )
        definition = self.definition(
            kind,
            node.name,
            node,
            return_annotation=(self.expression(node.returns) if node.returns is not None else None),
            decorators=decorators,
            asynchronous=asynchronous,
        )
        self.parameters(definition, node.args)
        defaults = (
            *node.args.defaults,
            *(item for item in node.args.kw_defaults if item is not None),
        )
        self.visit_expressions((*node.decorator_list, *defaults))
        self.owners.append(definition)
        self.callers.append(definition)
        for statement in node.body:
            self.visit(statement)
        self.callers.pop()
        self.owners.pop()

    def parameters(self, function: GraphNode, arguments: ast.arguments) -> None:
        """Record ordered callable parameters and their declared types."""
        positional = (*arguments.posonlyargs, *arguments.args)
        parameters = (
            *positional,
            *((arguments.vararg,) if arguments.vararg is not None else ()),
            *arguments.kwonlyargs,
            *((arguments.kwarg,) if arguments.kwarg is not None else ()),
        )
        for ordinal, parameter in enumerate(parameters):
            qualname = f"{function.qualname}.{parameter.arg}"
            item = GraphNode(
                kind=NodeKind.PARAMETER,
                qualname=qualname,
                path=self.document.path,
                span=self.span(parameter),
                annotation=(
                    self.expression(parameter.annotation)
                    if parameter.annotation is not None
                    else None
                ),
                ordinal=ordinal,
            )
            self.nodes.setdefault(item.id, item)
            self.relate(function, item, EdgeKind.DEFINE, parameter)

    def assignment(self, target: ast.expr, statement: ast.stmt, annotation: str | None) -> None:
        """Turn relevant assignment targets into stable variable or attribute nodes."""
        owner = self.owners[-1]
        name: str | None = None
        kind: NodeKind | None = None
        field_owner = owner
        if isinstance(target, ast.Name) and owner.kind in {NodeKind.MODULE, NodeKind.CLASS}:
            name = target.id
            kind = NodeKind.ATTRIBUTE if owner.kind is NodeKind.CLASS else NodeKind.VARIABLE
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
            and self.classes
        ):
            name = target.attr
            kind = NodeKind.ATTRIBUTE
            field_owner = self.classes[-1]
        if name is None or kind is None:
            return
        qualname = f"{field_owner.qualname}.{name}"
        item = GraphNode(
            kind=kind,
            qualname=qualname,
            path=self.document.path,
            span=self.span(statement),
            annotation=annotation,
        )
        if existing := self.nodes.get(item.id):
            if existing.annotation is None and annotation is not None:
                self.nodes[item.id] = existing.model_copy(update={"annotation": annotation})
            return
        self.nodes[item.id] = item
        self.relate(field_owner, item, EdgeKind.DEFINE, statement)

    def definition(
        self,
        kind: NodeKind,
        name: str,
        node: ast.AST,
        *,
        return_annotation: str | None = None,
        decorators: tuple[str, ...] = (),
        asynchronous: bool = False,
    ) -> GraphNode:
        """Create one stable symbol and connect it to its lexical owner."""
        owner = self.owners[-1]
        qualname = f"{owner.qualname}.{name}"
        definition = GraphNode(
            kind=kind,
            qualname=qualname,
            path=self.document.path,
            span=self.span(node),
            return_annotation=return_annotation,
            decorators=decorators,
            asynchronous=asynchronous,
        )
        self.nodes[definition.id] = definition
        self.relate(owner, definition, EdgeKind.DEFINE, node)
        return definition

    def reference(
        self,
        source: GraphNode,
        expression: str,
        kind: EdgeKind,
        node: ast.AST,
    ) -> None:
        """Retain one reference for repository-wide symbolic resolution."""
        self.references.append(
            SymbolReference(
                source=source.id,
                expression=expression,
                module=self.module.qualname,
                scope=source.qualname,
                owner_class=self.classes[-1].qualname if self.classes else None,
                kind=kind,
                span=self.span(node),
            )
        )

    def relate(self, source: GraphNode, target: GraphNode, kind: EdgeKind, node: ast.AST) -> None:
        """Record an exact relation between two extracted nodes."""
        self.relations.append(
            PendingRelation(
                source=source.id,
                target=target.id,
                kind=kind,
                span=self.span(node),
            )
        )

    def visit_expressions(self, expressions: Iterable[ast.AST]) -> None:
        """Visit expressions evaluated in the current lexical owner."""
        for expression in expressions:
            self.visit(expression)

    def expression(self, node: ast.AST | None) -> str:
        """Return a stable source-like representation for one expression."""
        if node is None:
            return ""
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "super"
        ):
            return f"super.{node.attr}"
        return ast.unparse(node)

    def span(self, node: ast.AST) -> SourceSpan:
        """Return the complete available source span for one syntax node."""
        return SourceSpan(
            path=self.document.path,
            line=getattr(node, "lineno", 1),
            column=getattr(node, "col_offset", 0),
            end_line=getattr(node, "end_lineno", None),
            end_column=getattr(node, "end_col_offset", None),
        )

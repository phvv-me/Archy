import ast
from collections.abc import Mapping
from pathlib import Path

import pytest

from archy.engine import (
    EdgeKind,
    GraphTooLargeError,
    NodeKind,
    RepositoryGraphBuilder,
    SourceUnit,
)


def source_units(documents: Mapping[str, str]) -> tuple[SourceUnit, ...]:
    """Build immutable source units from concise path and source fixtures."""
    return tuple(SourceUnit(path=Path(path), source=source) for path, source in documents.items())


_PROJECT_SOURCES = source_units(
    {
        "pkg/__init__.py": "",
        "pkg/a.py": "from . import b\nimport requests\nb.work()\n",
        "pkg/b.py": "from . import a\ndef work():\n    pass\n",
        "app.py": "import pkg.a\n",
    }
)


def test_builder_preserves_typed_source_site_facts(tmp_path: Path) -> None:
    graph = RepositoryGraphBuilder().build(tmp_path, _PROJECT_SOURCES)
    nodes = {node.qualname: node for node in graph.nodes}

    assert nodes["app"].kind is NodeKind.MODULE
    assert nodes["requests"].kind is NodeKind.EXTERNAL_MODULE
    assert nodes["pkg.a"].id == "python:module:pkg.a"
    assert len(graph.edges_between(nodes["pkg.a"].id, nodes["pkg.b"].id)) == 1
    assert graph.edges_between(nodes["pkg.a"].id, nodes["pkg.b"].id)[0].kind is EdgeKind.IMPORT
    assert graph.edges_between(nodes["pkg.a"].id, nodes["pkg.b.work"].id)[0].kind is EdgeKind.CALL
    assert all(edge.evidence.source_hash for edge in graph.edges)
    assert all(not edge.id.startswith("python:") for edge in graph.edges)


def test_builder_reuses_supplied_trees_and_resolves_plain_import_calls(tmp_path: Path) -> None:
    sources = (
        *source_units({"pkg/__init__.py": "", "pkg/a.py": "def work():\n    pass\n"}),
        SourceUnit(
            path=Path("app.py"),
            source="not valid Python",
            tree=ast.parse("import pkg.a\npkg.a.work()\n"),
        ),
    )

    graph = RepositoryGraphBuilder().build(tmp_path, sources)
    nodes = {node.qualname: node for node in graph.nodes}

    assert graph.parse_errors == ()
    assert len(graph.edges_between(nodes["app"].id, nodes["pkg.a"].id)) == 1
    assert graph.edges_between(nodes["app"].id, nodes["pkg.a.work"].id)[0].kind is EdgeKind.CALL


def test_builder_exposes_import_cycles_without_networkx(tmp_path: Path) -> None:
    graph = RepositoryGraphBuilder().build(tmp_path, _PROJECT_SOURCES)

    cycle = graph.cycles()[0]
    assert cycle.modules == ("python:module:pkg.a", "python:module:pkg.b")
    assert {(edge.source, edge.target) for edge in cycle.edges} == {
        ("python:module:pkg.a", "python:module:pkg.b"),
        ("python:module:pkg.b", "python:module:pkg.a"),
    }
    assert graph.cycles(EdgeKind.CALL) == ()


def test_builder_resolves_package_reexports_without_phantom_cycles(tmp_path: Path) -> None:
    sources = source_units(
        {
            "pkg/__init__.py": "from .a import Public\n",
            "pkg/a.py": "class Public:\n    pass\n",
            "pkg/b.py": "from . import Public\n",
        }
    )

    graph = RepositoryGraphBuilder().build(tmp_path, sources)
    nodes = {node.qualname: node for node in graph.nodes}

    assert graph.cycles() == ()
    assert len(graph.edges_between(nodes["pkg.b"].id, nodes["pkg.a"].id)) == 1
    assert graph.edges_between(nodes["pkg.b"].id, nodes["pkg"].id) == ()


def test_builder_excludes_type_checking_dependencies(tmp_path: Path) -> None:
    sources = source_units(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import b\n"
            ),
            "pkg/b.py": "from . import a\n",
        }
    )

    graph = RepositoryGraphBuilder().build(tmp_path, sources)

    assert graph.cycles() == ()


def test_builder_records_folders_files_fields_types_inheritance_and_every_call(
    tmp_path: Path,
) -> None:
    sources = source_units(
        {
            "pkg/__init__.py": "",
            "pkg/models.py": (
                "class Base:\n"
                "    enabled: bool = True\n\n"
                "class Service(Base):\n"
                "    timeout: float\n\n"
                "    def run(self, value: int) -> str:\n"
                "        self.retries: int = 1\n"
                "        self.helper()\n"
                "        dynamic().execute()\n"
                "        return str(value)\n\n"
                "    def helper(self) -> None:\n"
                "        pass\n"
            ),
        }
    )

    graph = RepositoryGraphBuilder().build(tmp_path, sources)
    nodes = {node.qualname: node for node in graph.nodes}

    assert any(node.kind is NodeKind.DIRECTORY and node.qualname == "pkg" for node in graph.nodes)
    assert nodes["pkg/models.py"].kind is NodeKind.FILE
    assert nodes["pkg.models.Service"].kind is NodeKind.CLASS
    assert nodes["pkg.models.Service.timeout"].annotation == "float"
    assert nodes["pkg.models.Service.retries"].annotation == "int"
    assert nodes["pkg.models.Service.run.value"].annotation == "int"
    assert nodes["pkg.models.Service.run"].return_annotation == "str"
    assert graph.edges_between(
        nodes["pkg.models.Service"].id,
        nodes["pkg.models.Base"].id,
        EdgeKind.INHERIT,
    )
    calls = graph.relations(EdgeKind.CALL, EdgeKind.INSTANTIATE)
    assert len(calls) == 4
    assert all(
        edge.source in graph.node_index and edge.target in graph.node_index for edge in graph.edges
    )
    assert graph.edges_between(
        nodes["pkg.models.Service.run"].id,
        nodes["pkg.models.Service.helper"].id,
        EdgeKind.CALL,
    )
    assert sum(node.kind is NodeKind.UNRESOLVED_SYMBOL for node in graph.nodes) == 2


def test_builder_reports_partial_parse_and_module_ceiling(tmp_path: Path) -> None:
    invalid = source_units({"pkg/__init__.py": "", "pkg/broken.py": "def broken(:\n"})

    graph = RepositoryGraphBuilder().build(tmp_path, invalid)
    assert graph.parse_errors == ("pkg.broken",)

    with pytest.raises(GraphTooLargeError, match="Found 2 modules"):
        RepositoryGraphBuilder(max_modules=1).build(tmp_path, invalid)


@pytest.mark.parametrize("path", [Path("/tmp/source.py"), Path("../source.py")])
def test_source_units_require_repository_relative_paths(path: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        SourceUnit(path=path, source="")

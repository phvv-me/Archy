from enum import Enum


class NodeKind(str, Enum):
    """Kinds of entities represented by the repository graph."""

    REPOSITORY = "repository"
    DIRECTORY = "directory"
    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    PROPERTY = "property"
    ATTRIBUTE = "attribute"
    VARIABLE = "variable"
    PARAMETER = "parameter"
    EXTERNAL_MODULE = "external-module"
    EXTERNAL_SYMBOL = "external-symbol"
    UNRESOLVED_SYMBOL = "unresolved-symbol"


class EdgeKind(str, Enum):
    """Kinds of relationships represented by the repository graph."""

    CONTAIN = "contain"
    DEFINE = "define"
    IMPORT = "import"
    CALL = "call"
    INSTANTIATE = "instantiate"
    INHERIT = "inherit"


class DerivationKind(str, Enum):
    """How one graph fact was obtained."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class ResolutionKind(str, Enum):
    """Static resolution quality for one graph fact."""

    EXACT = "exact"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"

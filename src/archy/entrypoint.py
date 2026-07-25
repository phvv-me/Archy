def main() -> None:
    """Run the Archy CLI when its parser extension is installed."""
    try:
        from archy.cli import main as cli
    except ModuleNotFoundError as error:
        if error.name not in {"tree_sitter", "tree_sitter_python"}:
            raise
        raise SystemExit("The Archy CLI needs `archy[parser]`.") from None
    cli()

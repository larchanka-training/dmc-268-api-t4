import ast
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parent.parent / "domain"
FORBIDDEN_ROOTS = frozenset({"adapters", "pydantic", "httpx"})


def imported_roots(source: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_domain_does_not_import_adapters_or_io_libraries() -> None:
    sources = sorted(DOMAIN_DIR.glob("*.py"))
    assert sources

    offenders = {
        source.name: sorted(imported_roots(source) & FORBIDDEN_ROOTS) for source in sources
    }

    assert {name: roots for name, roots in offenders.items() if roots} == {}

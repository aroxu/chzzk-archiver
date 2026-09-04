"""Guard the layering that the modular refactor introduced."""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"

LAYER = {"config": 0, "db": 1, "models": 2, "schemas": 0, "security": 3, "services": 4, "routers": 5, "lifecycle": 6, "main": 7, "schema_migrations": 3}


def _imports(path: pathlib.Path) -> set[str]:
    package = path.relative_to(ROOT).parts[:-1]
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level:
            prefix = list(package) if node.level == 1 else []
            parts = [p for p in (node.module or "").split(".") if p]
            target = [*prefix, *parts]
            if target:
                found.add(target[0])
    return found


def test_layers_only_depend_downward():
    """Routers may use services, but nothing low-level may import upward."""
    violations = []
    for path in sorted(ROOT.rglob("*.py")):
        top = path.relative_to(ROOT).parts[0].removesuffix(".py")
        if top not in LAYER:
            continue
        for imported in _imports(path):
            if imported in LAYER and LAYER[imported] > LAYER[top]:
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert violations == []


def test_no_sqlalchemy_remains():
    offenders = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if "sqlalchemy" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


def test_main_is_only_assembly():
    """main.py should wire routers together, not hold business logic."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) < 60
    assert "include_router" in source

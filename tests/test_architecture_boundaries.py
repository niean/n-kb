import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"

DOMAIN_FORBIDDEN_IMPORTS = (
    "fastapi",
    "qdrant_client",
    "httpx",
    "langgraph",
    "llama_index",
    "sqlite3",
    "app.infrastructure",
)
APPLICATION_FORBIDDEN_IMPORTS = ("app.infrastructure",)
INTERFACES_FORBIDDEN_IMPORTS = (
    "app.infrastructure",
    "httpx",
    "qdrant_client",
    "sqlite3",
    "llama_index",
)


def iter_python_files(package_path: Path):
    return sorted(path for path in package_path.rglob("*.py") if path.is_file())


def module_name_for_path(path: Path) -> str:
    relative_path = path.relative_to(PROJECT_ROOT).with_suffix("")
    return ".".join(relative_path.parts)


def resolve_import_from_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    current_package_parts = module_name_for_path(path).split(".")[:-1]
    base_parts = current_package_parts[: len(current_package_parts) - node.level + 1]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_import_from_module(path, node)
            if module:
                imports.append(module)
                imports.extend(f"{module}.{alias.name}" for alias in node.names)

    return imports


def test_imported_modules_includes_import_from_alias_names(tmp_path):
    module_path = tmp_path / "sample.py"
    module_path.write_text(
        "from app import infrastructure\n"
        "from app.infrastructure import persistence\n",
        encoding="utf-8",
    )

    imports = imported_modules(module_path)

    assert "app.infrastructure" in imports
    assert "app.infrastructure.persistence" in imports


def test_imported_modules_resolves_relative_imports(tmp_path):
    module_path = PROJECT_ROOT / "app" / "application" / "sample.py"
    module_path.write_text("from ..infrastructure import persistence\n", encoding="utf-8")

    try:
        imports = imported_modules(module_path)
    finally:
        module_path.unlink()

    assert "app.infrastructure" in imports
    assert "app.infrastructure.persistence" in imports


def assert_package_has_no_forbidden_imports(package: str, forbidden_prefixes: tuple[str, ...]):
    violations = []
    package_path = APP_ROOT / package

    for path in iter_python_files(package_path):
        for module in imported_modules(path):
            if module.startswith(forbidden_prefixes):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {module}")

    assert not violations, "Forbidden imports found:\n" + "\n".join(violations)


def test_domain_does_not_import_frameworks_sdks_or_infrastructure():
    assert_package_has_no_forbidden_imports("domain", DOMAIN_FORBIDDEN_IMPORTS)


def test_application_does_not_import_infrastructure():
    assert_package_has_no_forbidden_imports("application", APPLICATION_FORBIDDEN_IMPORTS)


def test_interfaces_do_not_import_infrastructure_or_external_sdks():
    assert_package_has_no_forbidden_imports("interfaces", INTERFACES_FORBIDDEN_IMPORTS)

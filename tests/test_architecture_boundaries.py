import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"

DOMAIN_FORBIDDEN_IMPORTS = (
    "fastapi",
    "qdrant_client",
    "httpx",
    "llama_index",
    "sqlite3",
    "app.infrastructure",
)
APPLICATION_FORBIDDEN_IMPORTS = ("app.infrastructure",)
INTERFACES_FORBIDDEN_IMPORTS = (
    "app.infrastructure",
    "qdrant_client",
    "sqlite3",
    "llama_index",
)


def iter_python_files(package_path: Path):
    return sorted(path for path in package_path.rglob("*.py") if path.is_file())


def imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    return imports


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

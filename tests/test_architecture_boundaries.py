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
    "app.application",
    "app.interfaces",
    # T7：Domain 零 OTel 依赖（含 opentelemetry-api 与任何 sdk/exporter 子模块）。
    "opentelemetry",
)
# T7：Application 仅允许 opentelemetry-api；SDK/exporter/instrumentation 禁止。
# 注意：不要把禁项写成 "opentelemetry.api" —— 该模块路径不存在，API 本体是
# "opentelemetry" 根命名空间（opentelemetry.trace / opentelemetry.metrics 等）。
APPLICATION_FORBIDDEN_IMPORTS = (
    "app.infrastructure",
    "app.interfaces",
    "opentelemetry.sdk",
    "opentelemetry.exporter",
    "opentelemetry.instrumentation",
)
INTERFACES_FORBIDDEN_IMPORTS = (
    "app.infrastructure",
    "httpx",
    "qdrant_client",
    "sqlite3",
    "llama_index",
)
INFRASTRUCTURE_FORBIDDEN_IMPORTS = (
    "app.application",
    "app.interfaces",
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


def test_imported_modules_resolves_relative_imports_in_domain():
    module_path = PROJECT_ROOT / "app" / "domain" / "sample.py"
    module_path.write_text("from .chunk import Chunk\n", encoding="utf-8")

    try:
        imports = imported_modules(module_path)
    finally:
        module_path.unlink()

    assert "app.domain.chunk" in imports


def test_imported_modules_expands_from_opentelemetry_import(tmp_path):
    """`from opentelemetry import sdk` 必须展开为 opentelemetry.sdk 子路径。"""
    module_path = tmp_path / "sample.py"
    module_path.write_text(
        "from opentelemetry import sdk\n"
        "from opentelemetry.sdk import trace\n",
        encoding="utf-8",
    )

    imports = imported_modules(module_path)

    assert "opentelemetry" in imports
    assert "opentelemetry.sdk" in imports
    assert "opentelemetry.sdk.trace" in imports


def test_observability_ast_rules_match_plan_boundaries():
    # T7 规则：Domain 禁止 opentelemetry（整个根命名空间）；
    # Application 禁止 SDK/exporter/instrumentation（API 可用）；
    # 不使用不存在的 opentelemetry.api 模块路径作为禁项。
    assert "opentelemetry" in DOMAIN_FORBIDDEN_IMPORTS
    assert "opentelemetry.sdk" in APPLICATION_FORBIDDEN_IMPORTS
    assert "opentelemetry.exporter" in APPLICATION_FORBIDDEN_IMPORTS
    assert "opentelemetry.instrumentation" in APPLICATION_FORBIDDEN_IMPORTS
    assert "opentelemetry.api" not in APPLICATION_FORBIDDEN_IMPORTS
    assert "opentelemetry.api" not in DOMAIN_FORBIDDEN_IMPORTS


def test_application_opentelemetry_sdk_import_would_be_flagged(tmp_path):
    module_path = tmp_path / "sample.py"
    module_path.write_text(
        "import opentelemetry.sdk.trace\n", encoding="utf-8"
    )

    modules = imported_modules(module_path)

    assert any(m.startswith(APPLICATION_FORBIDDEN_IMPORTS) for m in modules)


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


def test_infrastructure_does_not_reverse_import_application_or_interfaces():
    assert_package_has_no_forbidden_imports("infrastructure", INFRASTRUCTURE_FORBIDDEN_IMPORTS)


def test_entry_scripts_import_infrastructure_only_via_composition():
    # 组合根（main.py/composition.py）外，入口脚本不得直接 import
    # app.infrastructure 符号；Infrastructure 实现经组合根函数注入。
    violations = []
    for name in ("mcp_stdio.py",):
        path = APP_ROOT / name
        for module in imported_modules(path):
            if module.startswith("app.infrastructure"):
                violations.append(f"{name} imports {module}")

    assert not violations, "Forbidden entry imports found:\n" + "\n".join(violations)


def test_detail_policy_is_stdlib_and_infrastructure_does_not_reverse_import():
    import sys
    path = APP_ROOT / "application" / "telemetry_details.py"
    for module in imported_modules(path):
        assert module.split(".")[0] in sys.stdlib_module_names or module == "__future__"
    for path in (APP_ROOT / "infrastructure" / "observability").glob("*.py"):
        assert not any(m.startswith("app.application") for m in imported_modules(path))

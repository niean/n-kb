from pathlib import Path

from app.main import create_app


STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "interfaces" / "http" / "static"


def test_static_management_files_exist_and_app_mounts_static_files(tmp_path):
    app = create_app()

    routes = {getattr(route, "path", None) for route in app.routes}

    assert "/static" in routes
    assert (STATIC_ROOT / "index.html").exists()
    assert (STATIC_ROOT / "app.js").exists()
    assert (STATIC_ROOT / "styles.css").exists()
    assert (STATIC_ROOT / "management-api.js").exists()
    assert (STATIC_ROOT / "management-ui.js").exists()
    assert (STATIC_ROOT / "management-navigation.js").exists()
    assert (STATIC_ROOT / "favicon.svg").exists()


def test_management_favicon_uses_kb_mark():
    favicon = (STATIC_ROOT / "favicon.svg").read_text(encoding="utf-8")

    assert "KB" in favicon


def test_management_index_references_split_static_resources_in_order():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'href="/static/styles.css"' in html
    assert "<style" not in html
    assert "style=" not in html
    script_names = [
        "management-api.js",
        "management-ui.js",
        "management-navigation.js",
        "app.js",
    ]
    positions = [html.index(f'src="/static/{name}"') for name in script_names]

    assert positions == sorted(positions)


def test_management_stylesheet_contains_migrated_key_css():
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    assert css.strip()
    assert ":root" in css
    assert ".sidebar" in css
    assert ".topbar" in css
    assert ".main-content" in css
    assert ".tab-content.active" in css
    assert ".stack-spaced" in css


def test_management_stylesheet_defines_design_tokens_and_component_modifiers():
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    required_tokens_and_classes = [
        ":root",
        "--color-primary",
        "--space-",
        "--radius-",
        ".sidebar__item--active",
        ".status-panel",
        ".btn--primary",
        ".badge--success",
        ".empty-state",
        ".loading-state",
        ".error-state",
    ]

    for expected in required_tokens_and_classes:
        assert expected in css


def test_management_fe_contains_required_sections_and_script():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert "N-KB" in html
    assert 'rel="icon"' in html
    assert 'href="/static/favicon.svg"' in html
    assert "sidebar" in html
    assert "topbar" in html
    assert "overview-stats" in html
    assert "upload-form" in html
    assert "document-list" in html
    assert "document-chunks" in html
    assert "dependency-health" in html
    assert "retrieval-form" in html
    assert "retrieval-min-score" in html
    assert 'value="0.5"' in html
    assert "概览" in html
    assert "文档" in html
    assert "检索" in html
    assert "健康" in html
    assert 'href="#overview"' in html
    assert 'href="#documents"' in html
    assert 'href="#retrieval"' in html
    assert 'href="#health"' in html
    assert "检索实验室" not in html
    assert "app.js" in html


def test_management_api_module_exposes_document_retrieval_and_health_helpers():
    js = (STATIC_ROOT / "management-api.js").read_text(encoding="utf-8")

    assert "window.NKB.api" in js
    for helper_name in [
        "listDocuments",
        "getDocument",
        "getDocumentContent",
        "getDocumentChunks",
        "indexDocument",
        "deleteDocument",
        "uploadDocument",
        "searchRetrieval",
        "getDependencyHealth",
    ]:
        assert helper_name in js



def test_management_ui_module_exposes_safe_render_helpers():
    js = (STATIC_ROOT / "management-ui.js").read_text(encoding="utf-8")

    assert "window.NKB.ui" in js
    assert "textContent" in js
    for helper_name in [
        "renderEmpty",
        "renderLoading",
        "renderError",
        "appendBadge",
        "parseTags",
    ]:
        assert helper_name in js



def test_management_ui_module_defines_standard_async_state_classes():
    js = (STATIC_ROOT / "management-ui.js").read_text(encoding="utf-8")

    for state_class in ["loading-state", "error-state", "empty-state"]:
        assert state_class in js



def test_management_app_uses_loading_and_error_helpers_for_async_paths():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "renderLoading" in js
    for function_name in [
        "loadHealth",
        "loadDocuments",
        "showDocument",
        "indexDocument",
        "deleteDocument",
        "uploadDocument",
        "searchRetrieval",
    ]:
        start = js.index(f"async function {function_name}")
        end = js.find("\nasync function ", start + 1)
        if end == -1:
            end = js.find("\nfunction init", start + 1)
        function_body = js[start:end]
        assert "renderLoading" in function_body
        assert "renderError" in function_body



def test_management_app_preserves_dangerous_delete_and_reload_flow():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "window.confirm" in js
    assert "Delete document ${documentId}?" in js
    assert "deleteDocument" in js
    assert "api.deleteDocument" in js
    assert "loadDocuments" in js
    delete_start = js.index("async function deleteDocument")
    confirm_pos = js.index("window.confirm", delete_start)
    api_delete_pos = js.index("api.deleteDocument", delete_start)
    reload_pos = js.index("loadDocuments", api_delete_pos)
    assert confirm_pos < api_delete_pos < reload_pos



def test_management_app_renders_document_load_failures_in_all_detail_regions():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    start = js.index("async function showDocument")
    end = js.index("\nasync function indexDocument", start)
    function_body = js[start:end]
    catch_body = function_body[function_body.index("} catch (error) {") :]

    assert "renderError(detail" in catch_body
    assert "renderError(chunksTarget" in catch_body
    assert "content.textContent" in catch_body
    assert "Unable to load document content" in catch_body
    assert "renderError(content" not in catch_body



def test_management_app_preserves_document_chunks_and_retrieval_filter_paths():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    for expected in [
        "getDocumentChunks",
        "source_kind",
        "document_status",
        "min_score",
        "token_count",
        "chunk_id",
    ]:
        assert expected in js



def test_management_navigation_module_exposes_tab_hash_and_sidebar_behaviors():
    js = (STATIC_ROOT / "management-navigation.js").read_text(encoding="utf-8")

    assert "window.NKB.navigation" in js
    for expected in [
        "tabNames",
        "selectedTabFromHash",
        "hashchange",
        "aria-expanded",
        "sidebar__item--active",
    ]:
        assert expected in js



def test_management_navigation_corrects_invalid_hash_without_history_entry():
    js = (STATIC_ROOT / "management-navigation.js").read_text(encoding="utf-8")

    assert "history.replaceState" in js
    assert "#overview" in js
    assert "history.pushState" not in js



def test_management_app_module_exposes_init_and_triggers_namespaced_initialization():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "window.NKB.app" in js
    assert "init()" in js
    assert "window.NKB.app.init()" in js



def test_management_js_uses_safe_dynamic_rendering_without_inner_html():
    management_js_files = sorted(STATIC_ROOT.glob("*.js"))
    combined_js = "\n".join(path.read_text(encoding="utf-8") for path in management_js_files)

    assert "document.createElement" in combined_js
    assert "textContent" in combined_js
    for path in management_js_files:
        assert "innerHTML" not in path.read_text(encoding="utf-8")
    assert "/documents" in combined_js
    assert "method: 'DELETE'" in combined_js
    assert "Delete" in combined_js
    assert "/health/dependencies" in combined_js
    assert "/retrieval/search" in combined_js
    assert "/chunks" in combined_js
    assert "source_kind" in combined_js
    assert "document_status" in combined_js
    assert "chunk_id" in combined_js
    assert "token_count" in combined_js
    assert "window.location.hash" in combined_js
    assert "hashchange" in combined_js
    assert "selectedTabFromHash" in combined_js


def test_management_js_keeps_document_list_compact_and_detail_metadata_complete():
    js = "\n".join(
        (STATIC_ROOT / name).read_text(encoding="utf-8")
        for name in ["app.js", "management-ui.js"]
    )
    list_start = js.index("async function loadDocuments")
    list_end = js.index("\nfunction renderChunks", list_start)
    list_body = js[list_start:list_end]
    detail_start = js.index("async function showDocument")
    detail_end = js.index("\nasync function indexDocument", detail_start)
    detail_body = js[detail_start:detail_end]

    assert "document-table" in list_body
    assert "文件名" in list_body
    assert "操作" in list_body
    assert "文件名称:" not in list_body
    assert "录入时间:" not in list_body
    assert "VDB入库状态:" not in list_body
    assert "ID:" not in list_body
    assert "Source:" not in list_body
    assert "Tags:" not in list_body
    assert "Detail" in list_body
    assert "Index" in list_body
    assert "Delete" in list_body
    assert "VDB入库状态:" in detail_body
    assert "ID:" in detail_body
    assert "来源:" in detail_body
    assert "Tags:" in detail_body
    assert "入库成功" in js
    assert "未入库" in js
    assert "入库失败" in js

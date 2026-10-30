import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.main import create_app


STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "interfaces" / "http" / "static"
FE_HARNESS = Path(__file__).resolve().parents[0] / "fe_harness" / "harness.js"

NODE_AVAILABLE = shutil.which("node") is not None


def _run_fe_scenario(scenario: str) -> dict:
    """在 Node 桩环境中运行前端 JS 的指定场景，返回 JSON 观测结果。"""
    proc = subprocess.run(
        ["node", str(FE_HARNESS), str(STATIC_ROOT), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"FE harness failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _css_rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", css)
    assert match is not None, f"missing CSS rule: {selector}"
    return match.group("body")


def _css_declarations(css: str, selector: str) -> list[str]:
    body = _css_rule(css, selector)
    return [part.strip() for part in body.split(";") if part.strip()]


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
    assert (STATIC_ROOT / "observations-otel.js").exists()
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
        "observations-otel.js",
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
        "getObservationsOtel",
    ]:
        assert helper_name in js


def test_management_api_observations_helper_uses_fixed_page_size():
    js = (STATIC_ROOT / "management-api.js").read_text(encoding="utf-8")

    assert "/observations/otel" in js
    start = js.index("function getObservationsOtel")
    end = js.find("\n  function ", start + 1)
    body = js[start:end]
    assert "'size', '10'" in body



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

    assert "global.NKB.navigation" in js
    for expected in [
        "tabNames",
        "selectedTabFromHash",
        "hashchange",
        "aria-expanded",
        "sidebar__item--active",
        "initNavigation(callback)",
        "onTabChange({ previous, current: nextTab })",
    ]:
        assert expected in js


def test_management_navigation_declares_observations_in_tab_names():
    js = (STATIC_ROOT / "management-navigation.js").read_text(encoding="utf-8")

    match = re.search(r"const tabNames = \[([^\]]*)\];", js)
    assert match is not None, "missing tabNames declaration"
    names = [item.strip().strip("'\"") for item in match.group(1).split(",")]
    assert "observations-otel" in names



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


def test_management_sidebar_defaults_expanded_and_guards_first_paint():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    body_open = '<body class="sidebar-expanded">'
    guard_read = "localStorage.getItem('nkb.sidebar.expanded')"
    sidebar = 'id="app-sidebar"'
    head = html[html.index("<head>") : html.index("</head>")]

    toggle_start = html.index('id="sidebar-toggle"')
    toggle = html[toggle_start : html.index("</button>", toggle_start)]

    assert body_open in html
    assert 'aria-expanded="true"' in toggle
    assert html.index(body_open) < html.index(guard_read) < html.index(sidebar)
    assert guard_read not in head


def test_management_first_paint_guard_uses_only_zero_as_collapsed():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert "v!=='0'" in html
    assert "classList.toggle('sidebar-expanded',v!=='0')" in html
    assert "classList.add('sidebar-expanded')" in html


def test_management_sidebar_css_supports_column_layout_and_nav_scrolling():
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
    sidebar = _css_rule(css, ".sidebar")
    toggle = _css_rule(css, ".sidebar__toggle")
    nav = _css_rule(css, ".sidebar__nav")

    assert "display: flex" in sidebar
    assert "flex-direction: column" in sidebar
    assert "flex: 0 0 auto" in toggle
    for declaration in [
        "flex: 1 1 auto",
        "min-height: 0",
        "overflow-x: hidden",
        "overflow-y: auto",
        "scrollbar-width: thin",
        "scrollbar-color: var(--color-border-default) transparent",
    ]:
        assert declaration in nav
    assert "width: 6px" in _css_rule(css, ".sidebar__nav::-webkit-scrollbar")
    thumb = _css_rule(css, ".sidebar__nav::-webkit-scrollbar-thumb")
    assert "background: var(--color-border-default)" in thumb
    assert "border-radius: 3px" in thumb
    assert "background: transparent" in _css_rule(css, ".sidebar__nav::-webkit-scrollbar-track")


def test_management_content_width_rules_match_reference_declarations():
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    collapsed = _css_declarations(css, ".topbar, .main-content")
    expanded = _css_declarations(
        css,
        "body.sidebar-expanded .topbar, body.sidebar-expanded .main-content",
    )

    assert collapsed == [
        "width: calc(100vw - var(--sidebar-width-collapsed))",
        "margin-left: var(--sidebar-width-collapsed)",
        "transition: margin-left 0.2s ease, width 0.2s ease",
    ]
    assert expanded == [
        "width: calc(100vw - var(--sidebar-width-expanded))",
        "margin-left: var(--sidebar-width-expanded)",
    ]
    assert all("max-width" not in declaration for declaration in collapsed + expanded)


def test_management_sidebar_rule_matches_reference_declarations():
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    assert _css_declarations(css, ".sidebar") == [
        "position: fixed",
        "inset: 0 auto 0 0",
        "z-index: 10",
        "display: flex",
        "flex-direction: column",
        "width: var(--sidebar-width-collapsed)",
        "overflow: hidden",
        "background: var(--color-bg-white)",
        "border-right: 1px solid var(--color-sidebar-border)",
        "box-shadow: var(--shadow-sidebar)",
        "transition: width var(--motion-fast)",
    ]
    assert _css_declarations(css, "body.sidebar-expanded .sidebar") == [
        "width: var(--sidebar-width-expanded)"
    ]


def test_management_sidebar_nav_rule_matches_local_reference_contract():
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    assert _css_declarations(css, ".sidebar__nav") == [
        "display: flex",
        "flex-direction: column",
        "gap: 4px",
        "padding: 8px 6px",
        "flex: 1 1 auto",
        "min-height: 0",
        "overflow-x: hidden",
        "overflow-y: auto",
        "scrollbar-width: thin",
        "scrollbar-color: var(--color-border-default) transparent",
    ]


def test_management_sidebar_alignment_preserves_local_contracts():
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
    root = _css_declarations(css, ":root")
    main = _css_declarations(css, ".main-content")
    sidebar_item = _css_declarations(css, ".sidebar__item")
    sidebar_parent = _css_declarations(css, ".sidebar__item--parent")
    topbar = _css_declarations(css, ".topbar")

    assert "--sidebar-width-collapsed: 64px" in root
    assert "--sidebar-width-expanded: 200px" in root
    assert "min-width: 0" in main
    assert "padding: 20px" in main
    for declaration in [
        "text-align: left",
        "cursor: pointer",
    ]:
        assert declaration in sidebar_item
    for item in [sidebar_item, sidebar_parent]:
        assert not any(declaration.startswith("font-size:") for declaration in item)
    assert "justify-content: space-between" in topbar
    assert "gap: var(--space-6)" not in topbar
    assert _css_declarations(css, ".sidebar__nav::-webkit-scrollbar") == [
        "width: 6px"
    ]
    assert _css_declarations(css, ".sidebar__nav::-webkit-scrollbar-thumb") == [
        "background: var(--color-border-default)",
        "border-radius: 3px",
    ]
    assert _css_declarations(css, ".sidebar__nav::-webkit-scrollbar-track") == [
        "background: transparent"
    ]
    assert _css_declarations(css, ".sidebar__item-icon") == [
        "display: inline-flex",
        "align-items: center",
        "justify-content: center",
        "width: 28px",
        "height: 28px",
    ]
    assert _css_declarations(css, ".sidebar__item-icon svg") == [
        "width: 24px",
        "height: 24px",
        "stroke: currentColor",
    ]
    assert _css_declarations(css, ".sidebar__toggle-icon svg") == [
        "width: 24px",
        "height: 24px",
        "stroke: currentColor",
    ]
    assert "gap: 12px" in sidebar_item
    assert "padding: 12px 10px" in sidebar_item


def test_management_navigation_persists_and_applies_sidebar_preference():
    js = (STATIC_ROOT / "management-navigation.js").read_text(encoding="utf-8")

    assert "const SIDEBAR_PREF_KEY = 'nkb.sidebar.expanded';" in js
    assert "const SUBMENU_PREF_KEY = 'nkb.sidebar.submenu.observations';" in js
    assert "localStorage.getItem(key)" in js
    assert "if (value === '0') return false;" in js
    assert "if (value === '1') return true;" in js
    assert "localStorage.setItem(key, expanded ? '1' : '0')" in js
    assert "document.body.classList.toggle('sidebar-expanded', expanded)" in js
    assert "toggle.setAttribute('aria-expanded', String(expanded))" in js
    assert "applySidebarExpanded(readPref(SIDEBAR_PREF_KEY))" in js
    assert js.index("applySidebarExpanded(nextExpanded)") < js.index("writePref(SIDEBAR_PREF_KEY, nextExpanded)")
    assert js.index("applySubmenuState(nextOpen)") < js.index("writePref(SUBMENU_PREF_KEY, nextOpen)")
    assert "global.NKB.navigation = { initNavigation }" in js


def test_management_navigation_uses_n_agent_parent_menu_components():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    js = (STATIC_ROOT / "management-navigation.js").read_text(encoding="utf-8")
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'data-menu-parent="observations"' in html
    assert 'data-submenu-of="observations"' in html
    assert 'id="submenu-observations"' in html
    assert 'aria-controls="submenu-observations"' in html
    assert 'data-tab="observations-otel"' in html
    assert 'href="#observations-otel"' in html
    assert "M3 3v18h18" in html
    assert "M7 14l3-3 3 3 5-7" in html
    assert "nkb.sidebar.submenu.observations" in js
    assert "aria-current" in js
    assert "history.replaceState(null, '', '#observations-otel')" in js
    for selector in [".sidebar__group", ".sidebar__item--parent", ".sidebar__submenu", ".sidebar__item--child"]:
        assert selector in css


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_navigation_parent_menu_alias_history_and_storage_failure():
    result = _run_fe_scenario("navigation")

    assert result["alias"]["current"] == "observations-otel"
    assert result["alias"]["requests"] == 1
    assert result["repeatRequests"] == 1
    assert result["parentCurrent"] == "observations-otel"
    assert result["expandedAfterEscape"] == "false"
    assert result["collapsedHoverExpanded"] == "true"
    assert result["collapsedAfterEscape"] == "false"
    assert result["collapsedFocusExpanded"] == "true"
    assert result["collapsedAfterFocusOut"] == "false"
    assert result["back"]["current"] == "observations-otel"
    assert result["forward"]["current"] == "documents"
    assert result["storageFailure"] is True


    paths = [
        STATIC_ROOT / "index.html",
        STATIC_ROOT / "styles.css",
        STATIC_ROOT / "management-navigation.js",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for excluded in [
        "topnav-mount",
        "topbar__reserved",
        "topnav__",
        "window.NAGENT",
        "nagent.",
    ]:
        assert excluded not in combined
    assert "history.pushState" not in combined


def test_management_app_keeps_navigation_initialization_contract():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "const { api, ui, navigation, observationsOtel } = window.NKB;" in js
    assert "navigation.initNavigation(" in js
    assert "observationsOtel.deactivate()" in js
    assert "observationsOtel.init()" in js


def test_management_navigation_preserves_leaf_hash_routing_and_title_sync():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    js = (STATIC_ROOT / "management-navigation.js").read_text(encoding="utf-8")

    for tab_name in ["overview", "documents", "retrieval", "health", "observations-otel"]:
        assert f'href="#{tab_name}"' in html
        assert f'data-tab="{tab_name}"' in html
    assert "window.addEventListener('hashchange'" in js
    assert "history.replaceState(null, '', '#overview')" in js
    assert "sidebar__item--active" in js
    assert "navigationById('topbar-title').textContent" in js
    assert "switchTab(selectedTabFromHash())" in js
    assert "history.pushState" not in js


def test_management_sidebar_divider_matches_n_agent_layout_contract():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    divider = '<hr class="sidebar__divider" aria-hidden="true">'
    assert html.count(divider) == 1
    assert html.index('data-tab="retrieval"') < html.index(divider) < html.index(
        'data-tab="health"'
    )
    assert _css_declarations(css, ".sidebar__divider") == [
        "flex: 0 0 1px",
        "height: 1px",
        "margin: 6px 8px",
        "background: var(--color-border-light)",
        "border: 0",
        "padding: 0",
    ]


def test_management_index_declares_observations_tab_and_container():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'href="#observations-otel"' in html
    assert 'data-tab="observations-otel"' in html
    assert "观测" in html
    assert 'id="tab-observations-otel"' in html
    assert 'id="observations-stats"' not in html
    assert 'id="observations-body"' not in html
    assert 'id="refresh-observations"' not in html
    assert 'src="/static/observations-otel.js"' in html
    assert html.index('data-tab="health"') < html.index('data-tab="observations-otel"')


def test_management_observations_uses_reference_components_and_safe_links():
    js = (STATIC_ROOT / "observations-otel.js").read_text(encoding="utf-8")
    ui_js = (STATIC_ROOT / "management-ui.js").read_text(encoding="utf-8")
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "namespace.observationsOtel = { init, refresh, deactivate }" in js
    assert "api.getObservationsOtel" in js
    assert "requestToken" in js
    for title in ["OTel 自检状态", "外部链接", "信号状态", "Instrument 清单(metric)", "最近执行片段(span)", "说明"]:
        assert title in js
    for component in ["policy-meta", "policy-item", "document-table__numeric", "otel-links", "observations-pager"]:
        assert component in js or component in ui_js
    assert "renderMetrics" not in js
    assert "renderLogs" not in js
    assert "parsed.username" in js and "parsed.password" in js
    assert "noopener noreferrer" in js
    assert "'http:'" in js and "'https:'" in js
    assert "observationsOtel.init()" in app_js
    assert "observationsOtel.deactivate()" in app_js
    assert "buildPager" in ui_js
    assert "formatDateTime" in ui_js


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_renders_n_agent_panels_fields_and_zero_values():
    result = _run_fe_scenario("instruments")

    text = result["bodyText"]
    for title in ["OTel 自检状态", "外部链接", "信号状态", "Instrument 清单(metric)", "最近执行片段(span)", "说明"]:
        assert title in text
    assert "gen_ai.client.operation.duration" in text
    assert "histogram" in text
    assert "采样率：0" in text
    assert "导出 endpoint：http://collector:4317" in text
    assert "关停截止后丢弃：2" in text
    assert "sampling：仅展示本进程自检快照" in text
    for label in ["信号", "状态", "最近交付", "累计失败", "最近成功", "最近失败", "当前故障"]:
        assert label in text
    for extra in ["指标实值", "最近安全日志", "diagnostic_error", "differing_fields"]:
        assert extra not in text


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_invalid_snapshot_uses_safe_fixed_error():
    result = _run_fe_scenario("invalid-snapshot")

    assert "加载 OTel 自检数据失败" in result["bodyText"]
    assert "secret-response" not in result["bodyText"]
    assert any(item["text"] == "重试" for item in result["elements"])


    result = _run_fe_scenario("malicious")

    body_text = result["bodyText"]
    assert '<script>alert("xss")</script>' in body_text
    assert '<img src=x onerror=alert(2)>' in body_text
    assert '<b>export deadline exceeded</b>' in body_text
    all_tags = set(result["createdTags"]) | {item["tag"] for item in result["elements"]}
    assert "SCRIPT" not in all_tags
    assert "IMG" not in all_tags
    assert "B" not in all_tags


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_network_failure_shows_retry_and_recovers():
    result = _run_fe_scenario("network-failure")

    before_elements = result["before"]["elements"]
    error_states = [item for item in before_elements if "error-state" in item["classes"]]
    assert error_states
    assert "加载 OTel 自检数据失败" in result["before"]["bodyText"]
    retry_buttons = [item for item in before_elements if item["tag"] == "BUTTON" and item["text"] == "重试"]
    assert retry_buttons

    after_text = result["after"]["bodyText"]
    assert "观测自检加载失败" not in after_text
    assert "healthy" in after_text or "正常" in after_text
    assert result["after"]["fetchUrls"][-1].startswith("/observations/otel?page=1&size=10")


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_http_error_shows_retry_and_recovers():
    result = _run_fe_scenario("http-error")

    before_elements = result["before"]["elements"]
    error_states = [item for item in before_elements if "error-state" in item["classes"]]
    assert error_states
    assert "加载 OTel 自检数据失败" in result["before"]["bodyText"]

    after_text = result["after"]["bodyText"]
    assert "观测自检加载失败" not in after_text
    assert result["after"]["fetchUrls"][-1].startswith("/observations/otel?page=1&size=10")


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_discards_stale_responses():
    result = _run_fe_scenario("stale-response")

    assert "new-span" in result["afterNew"]["bodyText"]
    assert "old-span" not in result["afterNew"]["bodyText"]
    assert "new-span" in result["afterOld"]["bodyText"]
    assert "old-span" not in result["afterOld"]["bodyText"]


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_pagination_uses_fixed_size_and_reload_on_tab_return():
    result = _run_fe_scenario("pagination")

    first_urls = result["firstPage"]["fetchUrls"]
    second_urls = result["secondPage"]["fetchUrls"]
    back_urls = result["backToFirst"]["fetchUrls"]
    assert first_urls[-1] == "/observations/otel?page=1&size=10"
    assert second_urls[-1] == "/observations/otel?page=2&size=10"
    assert back_urls[-1] == "/observations/otel?page=1&size=10"

    first_buttons = {item["text"]: item.get("disabled", False) for item in result["firstPage"]["elements"] if item["tag"] == "BUTTON"}
    assert first_buttons["<"] is True
    assert first_buttons[">"] is False
    second_buttons = {item["text"]: item.get("disabled", False) for item in result["secondPage"]["elements"] if item["tag"] == "BUTTON"}
    assert second_buttons["<"] is False
    assert second_buttons[">"] is True

    assert "第 1-10 条 / 共 12 条" in result["firstPage"]["bodyText"]
    assert "第 11-12 条 / 共 12 条" in result["secondPage"]["bodyText"]
    assert "span-10" in result["secondPage"]["bodyText"]
    assert "span-0" in result["firstPage"]["bodyText"]


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_external_links_use_n_agent_names_and_safe_urls():
    result = _run_fe_scenario("grafana")

    anchors = result["anchors"]
    assert len(anchors) == 2
    metric_link = next(a for a in anchors if a["text"] == "Metric")
    trace_link = next(a for a in anchors if a["text"] == "Trace")
    assert metric_link["href"] == "https://grafana.example.com/?org=2#main"
    assert trace_link["href"] == "https://grafana.example.com/explore?orgId=1"
    for anchor in anchors:
        assert anchor["target"] == "_blank"
        assert anchor["rel"] == "noopener noreferrer"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_grafana_links_hidden_or_rejected_when_unsafe():
    result = _run_fe_scenario("grafana-unsafe")

    assert result["anchors"] == []


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_disabled_and_empty_state_remains_readable():
    result = _run_fe_scenario("empty-disabled")

    body_text = result["bodyText"]
    assert "disabled" in body_text
    assert "暂无已采样 span" in body_text
    assert result["anchors"] == []
    retry_buttons = [item for item in result["elements"] if item["tag"] == "BUTTON" and item["text"] == "重试"]
    assert not retry_buttons
    error_states = [item for item in result["elements"] if "error-state" in item["classes"]]
    assert not error_states
    # total=0：无“1 / 0”页，分页按钮不可点
    assert "暂无记录" in body_text
    assert "1 / 0" not in body_text
    empty_buttons = {
        item["text"]: item.get("disabled", False)
        for item in result["elements"]
        if item["tag"] == "BUTTON" and item["text"] in ("<", ">")
    }
    assert empty_buttons == {"<": True, ">": True}


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_ignores_response_after_leaving_and_reloads_on_return():
    result = _run_fe_scenario("leave-return")

    assert "left-span" not in result["afterLeave"]["bodyText"]
    assert "returned-span" in result["afterReturn"]["bodyText"]
    assert result["afterReturn"]["fetchUrls"][-1].endswith("page=1&size=10")


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_ignores_stale_failure_after_latest_success():
    result = _run_fe_scenario("stale-failure")

    assert "latest-span" in result["bodyText"]
    assert "stale-secret-error" not in result["bodyText"]
    assert not [item for item in result["elements"] if "error-state" in item["classes"]]


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_refresh_preserves_current_page():
    result = _run_fe_scenario("refresh-page")

    assert "refreshed-page-two" in result["bodyText"]
    assert result["fetchUrls"][-1].endswith("page=2&size=10")


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_corrects_shrunken_page_once():
    result = _run_fe_scenario("page-shrink")

    assert "corrected-page-two" in result["bodyText"]
    assert [url.split("page=")[1].split("&")[0] for url in result["fetchUrls"]] == ["1", "10", "2"]


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_page_two_retry_keeps_failed_page():
    result = _run_fe_scenario("page-two-retry")

    assert "page-two-secret" not in result["failed"]["bodyText"]
    assert result["recovered"]["fetchUrls"][-1].endswith("page=2&size=10")


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_repeated_shrink_offers_back_to_first_page():
    result = _run_fe_scenario("repeated-shrink")

    assert "数据已变化，当前页暂无记录" in result["recovery"]["bodyText"]
    assert "back-on-first-page" in result["first"]["bodyText"]
    assert result["first"]["fetchUrls"][-1].endswith("page=1&size=10")


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node 运行时不可用时跳过前端行为验证")
def test_fe_observations_null_array_and_json_failure_use_fixed_error():
    result = _run_fe_scenario("invalid-variants")

    assert len(result["states"]) == 3
    for state in result["states"]:
        assert "加载 OTel 自检数据失败" in state["bodyText"]
        assert "bad json" not in state["bodyText"]


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node required for FE behavior")
def test_complete_signals_render_safely_and_refresh_failure_keeps_snapshot():
    state = _run_fe_scenario('complete-signals')
    text = state['before']['bodyText']
    for value in ['OTel 自检状态', '外部链接', '信号状态', 'Instrument 清单(metric)', '最近执行片段(span)', '说明', 'span-1']:
        assert value in text
    for extra in ['指标实值', '最近安全日志', 'nkb.retrieval.completed', 'provider_lifetime', 'nkb.result_count']:
        assert extra not in text
    assert {a['text'] for a in state['before']['anchors']} == {'Metric', 'Log', 'Profile'}
    assert all(not a['href'].startswith('javascript:') for a in state['before']['anchors'])
    assert not any(e['tag'] in ('IMG', 'SCRIPT') for e in state['before']['elements'])
    assert 'span-1' in state['after']['bodyText']
    assert '保留上次快照' in state['after']['bodyText']
    assert 'SECRET' not in state['after']['bodyText']


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node required for FE behavior")
def test_extra_snapshot_values_and_diagnostics_do_not_add_n_kb_only_content():
    state = _run_fe_scenario('summary-states')['before']
    text = state['bodyText']
    for value in ['span-1', 'UNSET', '1ms', 'sampling：仅展示本进程自检快照']:
        assert value in text
    for extra in ['摘要已陈旧', 'metric_collection_failed', 'counter-value', 'gauge-unknown', 'metrics.dropped_queue_full', 'HIDDEN-DIAGNOSTIC', 'nkb.result_count', 'trace-1', 'sid-1']:
        assert extra not in text

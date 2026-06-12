from pathlib import Path

from app.main import create_app


STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "interfaces" / "http" / "static"


def test_static_management_files_exist_and_app_mounts_static_files(tmp_path):
    app = create_app()

    routes = {getattr(route, "path", None) for route in app.routes}

    assert "/static" in routes
    assert (STATIC_ROOT / "index.html").exists()
    assert (STATIC_ROOT / "app.js").exists()


def test_management_fe_contains_required_sections_and_script():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert "N-KB" in html
    assert "sidebar" in html
    assert "topbar" in html
    assert "overview-stats" in html
    assert "upload-form" in html
    assert "document-list" in html
    assert "document-chunks" in html
    assert "dependency-health" in html
    assert "retrieval-form" in html
    assert "retrieval-min-score" in html
    assert "总览" in html
    assert "文档" in html
    assert "检索" in html
    assert "健康" in html
    assert "检索实验室" not in html
    assert "app.js" in html


def test_management_js_uses_safe_dynamic_rendering_without_inner_html():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "document.createElement" in js
    assert "textContent" in js
    assert "innerHTML" not in js
    assert "fetch('/documents'" in js
    assert "method: 'DELETE'" in js
    assert "Delete" in js
    assert "fetch('/health/dependencies'" in js
    assert "fetch('/retrieval/search'" in js
    assert "/chunks" in js
    assert "source_kind" in js
    assert "document_status" in js
    assert "chunk_id" in js
    assert "token_count" in js


def test_management_js_labels_vdb_index_status_in_document_list():
    js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "VDB:" in js
    assert "入库成功" in js
    assert "未入库" in js
    assert "入库失败" in js

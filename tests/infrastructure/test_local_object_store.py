import hashlib

import pytest

from app.infrastructure.storage.local_object_store import LocalObjectStore


def test_save_writes_original_file_under_document_directory(tmp_path):
    store = LocalObjectStore(tmp_path)
    content = b"# hello\n"

    stored = store.save("doc-1", "notes.md", content)

    assert stored.path == tmp_path / "documents" / "doc-1" / "original.md"
    assert stored.path.read_bytes() == content
    assert stored.size_bytes == len(content)
    assert stored.content_hash == hashlib.sha256(content).hexdigest()
    assert stored.content_type == "text/markdown"
    assert store.read_text("doc-1") == "# hello\n"


def test_save_plain_text_and_delete_controlled_document_directory(tmp_path):
    store = LocalObjectStore(tmp_path)
    stored = store.save("doc-1", "notes.txt", b"plain")

    assert stored.content_type == "text/plain"
    assert store.read_text("doc-1") == "plain"

    store.delete("doc-1")

    assert not (tmp_path / "documents" / "doc-1").exists()


@pytest.mark.parametrize("document_id,filename", [("../escape", "notes.md"), ("doc-1", "../notes.md")])
def test_save_rejects_path_traversal(tmp_path, document_id, filename):
    store = LocalObjectStore(tmp_path)

    with pytest.raises(ValueError, match="invalid_storage_path"):
        store.save(document_id, filename, b"body")


def test_save_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "doc-1").symlink_to(outside, target_is_directory=True)
    store = LocalObjectStore(tmp_path)

    with pytest.raises(ValueError, match="invalid_storage_path"):
        store.save("doc-1", "notes.md", b"body")

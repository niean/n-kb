import hashlib
import shutil
from pathlib import Path

from app.domain.ports import StoredObject


class LocalObjectStore:
    def __init__(self, storage_root: str | Path):
        self.storage_root = Path(storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.documents_root = self.storage_root / "documents"
        self.documents_root.mkdir(parents=True, exist_ok=True)

    def _ensure_safe_path(self, path: Path) -> Path:
        try:
            resolved_root = self.storage_root.resolve()
            resolved_path = path.resolve(strict=False)
            if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
                raise ValueError("invalid_storage_path")
            return resolved_path
        except OSError as exc:
            raise ValueError("invalid_storage_path") from exc

    def _document_directory(self, document_id: str) -> Path:
        if Path(document_id).name != document_id:
            raise ValueError("invalid_storage_path")
        document_dir = self.documents_root / document_id
        self._ensure_safe_path(document_dir)
        return document_dir

    @staticmethod
    def _extension(filename: str) -> str:
        extension = Path(filename).suffix.lower()
        return extension if extension in {".md", ".txt"} else ".txt"

    @staticmethod
    def _content_type(extension: str) -> str:
        return "text/markdown" if extension == ".md" else "text/plain"

    def save(self, document_id: str, filename: str, content: bytes) -> StoredObject:
        if Path(filename).name != filename:
            raise ValueError("invalid_storage_path")
        extension = self._extension(filename)
        document_dir = self._document_directory(document_id)
        target = document_dir / f"original{extension}"
        self._ensure_safe_path(target)
        document_dir.mkdir(parents=True, exist_ok=True)
        if document_dir.resolve() != self._ensure_safe_path(document_dir):
            raise ValueError("invalid_storage_path")
        if document_dir.is_symlink() or not self._ensure_safe_path(target).is_relative_to(self.storage_root.resolve()):
            raise ValueError("invalid_storage_path")

        target.write_bytes(content)
        return StoredObject(
            path=target,
            size_bytes=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            content_type=self._content_type(extension),
        )

    def read_text(self, document_id: str) -> str:
        document_dir = self._document_directory(document_id)
        for extension in (".md", ".txt"):
            path = document_dir / f"original{extension}"
            self._ensure_safe_path(path)
            if path.exists():
                return path.read_text(encoding="utf-8")
        raise FileNotFoundError(document_id)

    def delete(self, document_id: str) -> None:
        document_dir = self._document_directory(document_id)
        if document_dir.exists():
            shutil.rmtree(document_dir)

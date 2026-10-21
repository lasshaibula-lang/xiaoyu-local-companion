"""Incremental Markdown indexing with working local keyword and optional vector search."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from companion.memory import _lexical_score, _normalize
from companion.storage import get_connection, now_iso


MAX_FILE_BYTES = 1024 * 1024
MAX_SYNC_FILES = 1000
MAX_SYNC_BYTES = 32 * 1024 * 1024
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_EXCLUDED_DIRS = {".git", ".obsidian", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
_EXCLUDED_FILES = {"credentials.md", "secrets.md", "secret.md", "api-keys.md", "api_keys.md", "apikeys.md", ".env.md"}


def _vector(value) -> list[float]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 8192:
        raise ValueError("向量维度必须在 1 到 8192 之间")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError("向量必须是有限数字组成的列表")
    vector = [float(item) for item in value]
    if not all(math.isfinite(item) for item in vector) or not any(vector):
        raise ValueError("向量必须是有限数字且不能全为零")
    return vector


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm, right_norm = math.hypot(*left), math.hypot(*right)
    return sum((a / left_norm) * (b / right_norm) for a, b in zip(left, right)) if left_norm and right_norm else 0.0


def _chunks(content: str) -> list[str]:
    result, start = [], 0
    while start < len(content):
        end = min(start + 800, len(content))
        if end < len(content):
            boundary = content.rfind("\n\n", start + 400, end)
            if boundary < 0:
                boundary = max(content.rfind(char, start + 550, end) for char in "\n。！？")
            if boundary >= 0:
                end = boundary + 1
        piece = content[start:end].strip()
        if piece:
            result.append(piece)
        if end >= len(content):
            break
        start = max(start + 1, end - 80)
    return result


def _is_link(path: Path) -> bool:
    try:
        flags = getattr(path.lstat(), "st_file_attributes", 0)
        return path.is_symlink() or bool(flags & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def _within(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _safe_index_path(path: Path, vault: Path) -> bool:
    if not _within(path, vault) or _within(path, _PROJECT_ROOT / "data"):
        return False
    if path.name.casefold() in _EXCLUDED_FILES or path.name.startswith("."):
        return False
    current = path
    while current != vault:
        if current.name.casefold() in _EXCLUDED_DIRS or _is_link(current):
            return False
        current = current.parent
    return True


def _validate_vault(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请提供 Obsidian 笔记库的绝对文件夹路径")
    value = value.strip()
    if value.startswith(("\\\\", "//")) or "://" in value:
        raise ValueError("仅支持本机文件夹路径")
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        raise ValueError("请使用笔记库的绝对路径")
    for part in (supplied, *supplied.parents):
        if _is_link(part):
            raise ValueError("笔记库不能经过符号链接或目录联接")
    try:
        vault = supplied.resolve(strict=True)
    except OSError as exc:
        raise ValueError("笔记库文件夹不存在或无法访问") from exc
    if not vault.is_dir():
        raise ValueError("笔记库路径必须是文件夹")
    if vault.parent == vault:
        raise ValueError("请指定笔记文件夹，不能索引整个磁盘")
    # Refuse network-mapped drives too, rather than treating a drive letter as local.
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(vault.anchor)) == 4:
            raise ValueError("仅支持本机文件夹，不支持映射的网络磁盘")
    if _within(vault, _PROJECT_ROOT / "data"):
        raise ValueError("项目 data 文件夹保存私人数据，不能作为知识库索引")
    return vault


def _document(row) -> dict:
    item = dict(row)
    item["excerpt"] = item.pop("content", "")[:180]
    item.pop("content_hash", None)
    item["managed"] = bool(item.get("managed", False))
    return item


class KnowledgeStore:
    def list(self) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM documents ORDER BY updated_at DESC, id").fetchall()
        return [_document(row) for row in rows]

    def import_text(self, title: str, content: str, path: str | None = None, managed: bool = True) -> dict:
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            raise ValueError("标题不能为空，且最多 200 字")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("笔记内容不能为空")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("每篇笔记最多 1 MB")
        title, content = title.strip(), content.strip()
        if path is None:
            path = "manual://" + str(uuid4())
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise ValueError("笔记来源路径无效")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with get_connection() as conn:
            existing = conn.execute("SELECT * FROM documents WHERE path = ?", (path,)).fetchone()
            if existing and existing["content_hash"] == digest and existing["title"] == title:
                return _document(existing)
            document_id = existing["id"] if existing else str(uuid4())
            if existing:
                conn.execute("UPDATE documents SET title = ?, content = ?, content_hash = ?, updated_at = ?, managed = ? WHERE id = ?", (title, content, digest, now_iso(), int(bool(managed)), document_id))
                conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            else:
                conn.execute("INSERT INTO documents (id, title, path, content, content_hash, updated_at, managed) VALUES (?, ?, ?, ?, ?, ?, ?)", (document_id, title, path, content, digest, now_iso(), int(bool(managed))))
            conn.executemany("INSERT INTO chunks (id, document_id, content, embedding, ordinal) VALUES (?, ?, ?, NULL, ?)", [(str(uuid4()), document_id, piece, ordinal) for ordinal, piece in enumerate(_chunks(content))])
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return _document(row)

    def delete(self, id: str) -> bool:
        """Delete the search index only. Source Markdown files are never changed."""
        with get_connection() as conn:
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (id,))
            cursor = conn.execute("DELETE FROM documents WHERE id = ?", (id,))
        return cursor.rowcount > 0

    def sync(self, vault_path: str) -> dict:
        vault = _validate_vault(vault_path)
        indexed, skipped, deleted, files_seen, bytes_seen = 0, 0, 0, 0, 0
        errors, truncated = [], False

        def walk_error(error):
            errors.append(str(error))

        for folder, dirs, files in os.walk(vault, followlinks=False, onerror=walk_error):
            folder_path = Path(folder)
            dirs[:] = sorted(name for name in dirs if name.casefold() not in _EXCLUDED_DIRS and not name.startswith(".") and not _is_link(folder_path / name) and not _within(folder_path / name, _PROJECT_ROOT / "data"))
            for name in sorted(files):
                if not name.casefold().endswith(".md"):
                    continue
                path = folder_path / name
                if not _safe_index_path(path, vault):
                    skipped += 1
                    continue
                files_seen += 1
                if files_seen > MAX_SYNC_FILES:
                    truncated = True
                    break
                try:
                    size = path.stat().st_size
                    if size > MAX_FILE_BYTES or bytes_seen + size > MAX_SYNC_BYTES:
                        skipped += 1
                        if bytes_seen + size > MAX_SYNC_BYTES:
                            truncated = True
                        continue
                    # A bounded binary read also protects against files growing after stat.
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_FILE_BYTES + 1)
                    if len(raw) > MAX_FILE_BYTES:
                        skipped += 1
                        continue
                    bytes_seen += len(raw)
                    content = raw.decode("utf-8-sig").strip()
                    if not content:
                        skipped += 1
                        continue
                    title_match = re.search(r"^#\s+(.+?)\s*#*\s*$", content, flags=re.MULTILINE)
                    title = (title_match.group(1) if title_match else path.stem).strip()[:200]
                    key = str(path)
                    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    with get_connection() as conn:
                        old = conn.execute("SELECT content_hash, title FROM documents WHERE path = ?", (key,)).fetchone()
                    if old and old["content_hash"] == digest and old["title"] == title:
                        skipped += 1
                        continue
                    self.import_text(title, content, path=key, managed=False)
                    indexed += 1
                except (OSError, UnicodeError, ValueError) as exc:
                    skipped += 1
                    if len(errors) < 20:
                        errors.append(f"{path.name}: {exc}")
            if files_seen > MAX_SYNC_FILES:
                break
        with get_connection() as conn:
            previous = conn.execute("SELECT id, path FROM documents WHERE managed = 0 ORDER BY id").fetchall()
        for item in previous:
            path = Path(item["path"])
            # Existing files beyond this scan's limit retain their previous index.
            if path.is_absolute() and _within(path, vault) and (not path.is_file() or not _safe_index_path(path, vault)):
                deleted += int(self.delete(item["id"]))
        return {"indexed": indexed, "deleted": deleted, "skipped": skipped, "truncated": truncated, "errors": errors}

    def search(self, query: str, limit: int = 5, query_embedding=None) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or limit <= 0:
            return []
        vector = _vector(query_embedding) if query_embedding is not None else None
        with get_connection() as conn:
            rows = conn.execute("SELECT c.id, c.content, c.embedding, c.ordinal, d.id AS document_id, d.title, d.path FROM chunks c JOIN documents d ON d.id = c.document_id ORDER BY d.id, c.ordinal, c.id").fetchall()
        best = {}
        for row in rows:
            lexical = max(_lexical_score(query, row["content"]), 0.85 * _lexical_score(query, row["title"]))
            semantic = None
            if vector is not None and row["embedding"]:
                try:
                    stored = _vector(json.loads(row["embedding"]))
                    if len(vector) == len(stored):
                        semantic = max(0.0, min(1.0, _cosine(vector, stored)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
            if semantic is not None:
                score = 0.35 * lexical + 0.65 * semantic
                if lexical < 0.15 and semantic < 0.55:
                    continue
            else:
                score = lexical
                if lexical < 0.15:
                    continue
            candidate = {"title": row["title"], "path": row["path"], "snippet": self._snippet(query, row["content"]), "score": round(score, 6), "document_id": row["document_id"]}
            if row["document_id"] not in best or score > best[row["document_id"]][0]:
                best[row["document_id"]] = (score, candidate)
        ranked = sorted(best.values(), key=lambda item: (-item[0], item[1]["document_id"]))
        return [item[1] for item in ranked[:min(int(limit), 50)]]

    @staticmethod
    def _snippet(query: str, content: str) -> str:
        if len(content) <= 420:
            return content
        needles = re.findall(r"[\u3400-\u9fff]{2,}|[a-zA-Z0-9]{2,}", query)
        normalized = _normalize(content)
        positions = [normalized.find(_normalize(needle)) for needle in needles]
        positions = [position for position in positions if position >= 0]
        start = max(0, min(positions) - 90) if positions else 0
        return ("…" if start else "") + content[start:start + 420] + ("…" if start + 420 < len(content) else "")

    def get_unembedded_chunks(self, limit: int = 64) -> list[dict]:
        if limit <= 0:
            return []
        with get_connection() as conn:
            rows = conn.execute("SELECT id, content FROM chunks WHERE embedding IS NULL OR embedding = '' ORDER BY document_id, ordinal, id LIMIT ?", (min(int(limit), 1024),)).fetchall()
        return [dict(row) for row in rows]

    def set_chunk_embedding(self, id: str, vector) -> None:
        encoded = json.dumps(_vector(vector), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with get_connection() as conn:
            cursor = conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (encoded, id))
            if cursor.rowcount == 0:
                raise ValueError("知识库片段不存在")

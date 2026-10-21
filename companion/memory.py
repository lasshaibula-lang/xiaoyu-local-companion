"""Local, inspectable memories. No network requests are made in this module."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from uuid import uuid4

from companion.storage import get_connection, now_iso


_VISIBLE = "id, content, kind, source, created_at, updated_at, pinned"
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORDS = re.compile(r"[a-z0-9]+(?:['’-][a-z0-9]+)*")
_STOP = {"the", "a", "an", "is", "are", "to", "of", "and", "or", "in", "on", "it", "i", "me", "my", "you", "your", "请问", "知道", "记得", "帮我", "一下", "什么", "关于", "有关", "这个", "那个", "怎么", "如何"}


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _fingerprint(value: str) -> str:
    """Ignore whitespace and punctuation when detecting exact repeated facts."""
    return "".join(c for c in _normalize(value) if c.isalnum())


def _tokens(value: str) -> Counter:
    normalized = _normalize(value)
    result = Counter(word for word in _WORDS.findall(normalized) if word not in _STOP)
    for part in _HAN.findall(normalized):
        grams = [part] if len(part) == 1 else [part[i:i + 2] for i in range(len(part) - 1)]
        result.update(gram for gram in grams if gram not in _STOP)
    return result


def _lexical_score(query: str, content: str) -> float:
    """Query coverage plus cosine similarity, including Chinese character bigrams."""
    q, c = _tokens(query), _tokens(content)
    if not q or not c:
        return 0.0
    overlap = set(q) & set(c)
    if not overlap:
        return 0.0
    dot = sum(q[word] * c[word] for word in overlap)
    norm = math.sqrt(sum(v * v for v in q.values()) * sum(v * v for v in c.values()))
    coverage = len(overlap) / len(q)
    score = 0.6 * coverage + 0.4 * dot / norm
    nq = _normalize(query).strip(" ?？!！。.")
    if len(nq) > 1 and nq in _normalize(content):
        score = min(1.0, score + 0.15)
    return score


def _content(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("记忆内容必须是文字")
    value = value.strip()
    if not value or not _fingerprint(value):
        raise ValueError("记忆内容不能为空")
    if len(value) > 2000:
        raise ValueError("每条记忆最多 2000 字")
    return value


def _row_dict(row) -> dict:
    result = dict(row)
    result["pinned"] = bool(result.get("pinned", False))
    return result


class MemoryStore:
    def list(self) -> list[dict]:
        with get_connection() as conn:
            rows = conn.execute(f"SELECT {_VISIBLE} FROM memories WHERE deleted_at IS NULL ORDER BY pinned DESC, updated_at DESC, id").fetchall()
        return [_row_dict(row) for row in rows]

    def add(self, content: str, kind: str = "fact", source: str = "manual") -> dict:
        content = _content(content)
        if kind not in {"fact", "preference", "identity", "event", "promise", "summary"}:
            raise ValueError("未知的记忆类型")
        if not isinstance(source, str) or not source.strip() or len(source) > 120:
            raise ValueError("记忆来源无效")
        source = source.strip()
        fingerprint = _fingerprint(content)
        with get_connection() as conn:
            if source != "manual":
                forgotten = conn.execute("SELECT content FROM tombstones").fetchall()
                if any(_fingerprint(row["content"]) == fingerprint for row in forgotten):
                    raise ValueError("这条记忆已被删除，不会自动重新保存；可手动添加")
            rows = conn.execute(f"SELECT {_VISIBLE} FROM memories WHERE deleted_at IS NULL").fetchall()
            for row in rows:
                if _fingerprint(row["content"]) == fingerprint:
                    return _row_dict(row)
            memory_id, stamp = str(uuid4()), now_iso()
            conn.execute("INSERT INTO memories (id, content, kind, source, created_at, updated_at, pinned) VALUES (?, ?, ?, ?, ?, ?, 0)", (memory_id, content, kind, source, stamp, stamp))
            row = conn.execute(f"SELECT {_VISIBLE} FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _row_dict(row)

    def update(self, id: str, content: str | None = None, pinned: bool | None = None) -> dict:
        if content is not None:
            content = _content(content)
        if pinned is not None and not isinstance(pinned, bool):
            raise ValueError("置顶值必须是布尔值")
        with get_connection() as conn:
            row = conn.execute(f"SELECT {_VISIBLE} FROM memories WHERE id = ? AND deleted_at IS NULL", (id,)).fetchone()
            if row is None:
                raise ValueError("记忆不存在或已删除")
            if content is not None:
                duplicates = conn.execute("SELECT id, content FROM memories WHERE deleted_at IS NULL AND id != ?", (id,)).fetchall()
                if any(_fingerprint(other["content"]) == _fingerprint(content) for other in duplicates):
                    raise ValueError("已有相同内容的记忆")
                # Replaced text must not be silently re-learned from an old transcript.
                if _fingerprint(content) != _fingerprint(row["content"]):
                    conn.execute("INSERT INTO tombstones (id, content, created_at) VALUES (?, ?, ?)", (str(uuid4()), row["content"], now_iso()))
                conn.execute("UPDATE memories SET content = ?, embedding = NULL, updated_at = ? WHERE id = ?", (content, now_iso(), id))
            if pinned is not None:
                conn.execute("UPDATE memories SET pinned = ?, updated_at = ? WHERE id = ?", (int(pinned), now_iso(), id))
            result = conn.execute(f"SELECT {_VISIBLE} FROM memories WHERE id = ?", (id,)).fetchone()
        return _row_dict(result)

    def delete(self, id: str) -> bool:
        with get_connection() as conn:
            row = conn.execute("SELECT content FROM memories WHERE id = ? AND deleted_at IS NULL", (id,)).fetchone()
            if row is None:
                return False
            stamp = now_iso()
            conn.execute("UPDATE memories SET deleted_at = ?, updated_at = ?, embedding = NULL, pinned = 0 WHERE id = ?", (stamp, stamp, id))
            conn.execute("INSERT INTO tombstones (id, content, created_at) VALUES (?, ?, ?)", (str(uuid4()), row["content"], stamp))
        return True

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or limit <= 0:
            return []
        ranked = []
        for item in self.list():
            score = _lexical_score(query, item["content"])
            # A pinned fact still has to match the user's current subject.
            if score < 0.15:
                continue
            item["score"] = round(min(1.0, score + (0.05 if item["pinned"] else 0)), 6)
            ranked.append(item)
        ranked.sort(key=lambda item: (-item["score"], not item["pinned"], item["id"]))
        return ranked[:min(int(limit), 50)]

    def extract_explicit(self, message: str, source: str = "chat") -> list[dict]:
        """Learn a small set of clear first-person statements; never infer a biography."""
        if not isinstance(message, str) or not message.strip():
            return []
        learned = []
        for sentence in re.split(r"[\n。！？!?]+", message):
            sentence = sentence.strip(" \t，,；;：:")
            if not sentence or len(sentence) > 2000:
                continue
            explicit = re.match(r"^(?:请)?(?:帮我)?(?:记住|记一下)(?:这件事|这一点)?[：:,，\s]*(.+)$", sentence)
            candidate = explicit.group(1).strip() if explicit else sentence
            # A request or hypothetical clause is not a reliable personal fact.
            if re.search(r"(?:不要记|别记|不许记|忘记|如果|假如|假设|比如|例如|是否|还是|能不能|可不可以|你觉得|要不要|吗$|么$)", candidate):
                continue
            identity = re.fullmatch(r"我叫[：:\s]*([^，,；;]{1,40})", candidate)
            preference = re.fullmatch(r"我(?P<changed>现在|目前)?(?P<negative>不)?喜欢(?P<object>[^，,；;]{1,120})", candidate)
            if identity:
                candidate, kind = "我叫" + identity.group(1).strip(), "identity"
            elif preference:
                obj = preference.group("object").strip()
                if not obj or obj.endswith(("吗", "么")):
                    continue
                candidate = "我" + ("不" if preference.group("negative") else "") + "喜欢" + obj
                kind = "preference"
            elif explicit:
                # Keep only the literal user-provided fact, without inventing fields.
                if re.search(r"(?:记住|记一下)", candidate) or candidate.endswith(("吗", "么")):
                    continue
                kind = "fact"
            else:
                continue
            try:
                if preference and preference.group("changed"):
                    # Only replace an exact object's opposite preference. A new hobby
                    # does not imply that every old hobby was abandoned.
                    opposite = "我" + ("" if preference.group("negative") else "不") + "喜欢" + preference.group("object").strip()
                    old = next((item for item in self.list() if item["kind"] == "preference" and _fingerprint(item["content"]) == _fingerprint(opposite)), None)
                    if old:
                        # Check tombstones before replacing a current preference.
                        with get_connection() as conn:
                            blocked = any(_fingerprint(row["content"]) == _fingerprint(candidate) for row in conn.execute("SELECT content FROM tombstones").fetchall())
                        if blocked and source != "manual":
                            continue
                        learned.append(self.update(old["id"], content=candidate))
                        continue
                learned.append(self.add(candidate, kind=kind, source=source))
            except ValueError:
                continue
        unique = {item["id"]: item for item in learned}
        return list(unique.values())

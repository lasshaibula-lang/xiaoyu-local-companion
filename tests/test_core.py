"""Offline regression checks for memory, local retrieval, and API boundaries.

Run from the project root with ``python -m unittest discover -s tests -v``.
Every test uses a fresh data directory and never calls a paid model API.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class IsolatedDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="companion-test-")
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.env = patch.dict(
            os.environ,
            {"COMPANION_DATA_DIR": str(self.data_dir)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        from companion.storage import init_db

        init_db()

class MemoryTests(IsolatedDataTest):
    def test_deleted_fact_is_not_recreated_by_automatic_extraction(self) -> None:
        from companion.memory import MemoryStore

        store = MemoryStore()
        message = "记住：我喜欢茉莉花茶。"
        extracted = store.extract_explicit(message)
        self.assertTrue(extracted, "明确要求记住的事实应能被抽取")
        forgotten = extracted[0]
        self.assertTrue(store.delete(forgotten["id"]))

        store.extract_explicit(message)
        self.assertFalse(
            any(item["content"] == forgotten["content"] for item in store.list()),
            "用户删除的记忆不能从旧消息自动复活",
        )
        self.assertFalse(store.recall("茉莉花茶"))

        restored = store.add(forgotten["content"], source="manual")
        self.assertIsNotNone(restored, "用户手动重新添加应尊重当前意愿")
        self.assertIn(restored["id"], {item["id"] for item in store.list()})

    def test_unrelated_query_does_not_force_a_memory_match(self) -> None:
        from companion.memory import MemoryStore

        store = MemoryStore()
        store.add("用户喜欢在雨天喝茉莉花茶。")
        self.assertFalse(store.recall("火星飞船轨道方程"))


class KnowledgeTests(IsolatedDataTest):
    def test_sync_replaces_changed_notes_and_removes_deleted_notes(self) -> None:
        from companion.knowledge import KnowledgeStore

        vault = Path(self.temp_dir.name) / "vault"
        vault.mkdir()
        note = vault / "饮食.md"
        note.write_text("# 饮食偏好\n我喜欢茉莉花茶，周末常喝。", encoding="utf-8")
        store = KnowledgeStore()

        store.sync(str(vault))
        self.assertTrue(store.search("茉莉花茶"))

        note.write_text("# 饮食偏好\n我现在喜欢喝普洱茶。", encoding="utf-8")
        store.sync(str(vault))
        self.assertFalse(store.search("茉莉花茶"), "旧笔记分片应在同步后失效")
        self.assertTrue(store.search("普洱茶"))

        note.unlink()
        store.sync(str(vault))
        self.assertFalse(store.search("普洱茶"), "已删除文件不能继续参与召回")
        self.assertEqual(store.list(), [])

    def test_nonempty_knowledge_base_does_not_force_unrelated_matches(self) -> None:
        from companion.knowledge import KnowledgeStore

        store = KnowledgeStore()
        store.import_text("饮食偏好", "我喜欢在雨天喝茉莉花茶。")
        self.assertTrue(store.search("茉莉花茶"))
        self.assertFalse(store.search("火星飞船轨道方程"))


class ApiTests(IsolatedDataTest):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        from companion.api import app

        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)
        response = self.client.put("/api/settings", json={"mode": "demo"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_saved_user_profile_is_used_as_reference_in_live_chat(self) -> None:
        profile = "用户明确说过自己会玩《三角洲行动》。"
        saved = self.client.put("/api/persona", json={"user_profile": profile})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.get("/api/persona").json()["user_profile"], profile)
        configured = self.client.put(
            "/api/settings",
            json={"mode": "live", "api_key": "example-local-test-key", "base_url": "https://model.example/v1", "embedding_enabled": False},
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        prompts = []

        async def fake_reply(model_client, messages, **kwargs):
            prompts.extend(messages)
            return "好呀，聊聊你的游戏。"

        with patch("companion.service.ChatClient.complete", new=fake_reply):
            response = self.client.post("/api/chat", json={"message": "想聊聊游戏。", "request_id": "profile-test"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(profile, prompts[1]["content"])
        self.assertNotIn(profile, prompts[0]["content"], "档案应是参考资料，而不是可执行的人设指令")

    def test_demo_reply_is_explicitly_identified_as_demo(self) -> None:
        response = self.client.post(
            "/api/chat",
            json={"message": "你好，今天我想聊聊。", "request_id": "demo-1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["mode"], "demo")
        self.assertEqual(body["reply"]["mode"], "demo")
        self.assertTrue(body["reply"]["content"].strip())

    def test_repeated_request_id_does_not_duplicate_either_message(self) -> None:
        payload = {"message": "今天我完成了一件开心的事。", "request_id": "repeat-1"}
        first = self.client.post("/api/chat", json=payload)
        second = self.client.post("/api/chat", json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["reply"], second.json()["reply"])

        history_response = self.client.get("/api/messages")
        self.assertEqual(history_response.status_code, 200, history_response.text)
        messages = history_response.json()["messages"]
        self.assertEqual(sum(item["role"] == "user" for item in messages), 1)
        self.assertEqual(sum(item["role"] == "assistant" for item in messages), 1)
        self.assertEqual(
            next(item["content"] for item in messages if item["role"] == "user"),
            payload["message"],
        )

    def test_settings_does_not_disclose_saved_api_key(self) -> None:
        key = "example-keep-this-local-971b76f5"
        saved = self.client.put("/api/settings", json={"api_key": key})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn(key, saved.text, "保存设置的响应也不能回显密钥")

        read = self.client.get("/api/settings")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertNotIn(key, read.text)
        self.assertNotIn("api_key", read.json())
        self.assertIs(read.json()["api_key_configured"], True)

    def test_live_without_api_key_returns_an_error_not_a_demo_reply(self) -> None:
        settings = self.client.put(
            "/api/settings", json={"mode": "live", "clear_api_key": True}
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        response = self.client.post(
            "/api/chat",
            json={"message": "你今天怎么样？", "request_id": "missing-key-1"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(
            any(term in response.text.lower() for term in ("key", "密钥")),
            "应明确告诉用户缺少 API 密钥",
        )
        self.assertNotIn("reply", response.json())
        history = self.client.get("/api/messages").json()["messages"]
        self.assertFalse(any(item["role"] == "assistant" for item in history))

    def test_live_provider_requests_and_usage_with_mock_http_transport(self) -> None:
        import httpx

        captured = []
        real_async_client = httpx.AsyncClient

        def model_endpoint(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "今天的努力值得记下来。"}}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 7},
                },
            )

        def mock_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(model_endpoint)
            return real_async_client(*args, **kwargs)

        for index, (provider, model) in enumerate(
            (("qwen", "qwen-plus-character"), ("qwen", "qwen-flash"), ("deepseek", "deepseek-flash"))
        ):
            with self.subTest(model=model):
                configured = self.client.put(
                    "/api/settings",
                    json={
                        "mode": "live", "provider": provider, "model": model,
                        "base_url": "https://model.example/v1", "api_key": "example-local-test-key",
                        "embedding_enabled": False, "monthly_budget": 50,
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)
                payload = {"message": "我今天完成了工作。", "request_id": f"live-{index}"}
                with patch("companion.llm.httpx.AsyncClient", side_effect=mock_client):
                    response = self.client.post("/api/chat", json=payload)
                    replay = self.client.post("/api/chat", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["mode"], "live")
                self.assertEqual(response.json()["reply"]["content"], "今天的努力值得记下来。")
                self.assertEqual(replay.json(), response.json())
                self.assertEqual(len(captured), index + 1, "重复请求不能再次调用计费模型")

                request = captured[-1]
                self.assertEqual(str(request.url), "https://model.example/v1/chat/completions")
                self.assertEqual(request.headers["Authorization"], "Bearer example-local-test-key")
                body = json.loads(request.content)
                self.assertEqual(body["model"], model)
                self.assertEqual(body["messages"][-1], {"role": "user", "content": payload["message"]})
                if provider == "deepseek":
                    self.assertEqual(body["thinking"], {"type": "disabled"})
                elif model == "qwen-flash":
                    self.assertIs(body["enable_thinking"], False)
                else:
                    self.assertNotIn("enable_thinking", body)

        usage = self.client.get("/api/usage").json()
        self.assertEqual(usage["calls"], 3)
        self.assertEqual(usage["input_tokens"], 369)
        self.assertEqual(usage["output_tokens"], 21)
        self.assertGreater(usage["estimated_spend"], 0)

    def test_foreign_origin_cannot_change_settings_or_send_messages(self) -> None:
        headers = {"Origin": "https://untrusted.example"}
        for path, payload in (
            ("/api/settings", {"mode": "live"}),
            ("/api/chat", {"message": "跨站请求", "request_id": "foreign-1"}),
        ):
            with self.subTest(path=path):
                method = self.client.put if path.endswith("settings") else self.client.post
                response = method(path, json=payload, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)

        self.assertEqual(self.client.get("/api/settings").json()["mode"], "demo")
        self.assertEqual(self.client.get("/api/messages").json()["messages"], [])

    def test_deleted_memory_invalidates_inflight_chat_without_saving_stale_reply(self) -> None:
        from companion.api import delete_memory
        from companion.memory import MemoryStore
        from companion.storage import get_connection

        configured = self.client.put(
            "/api/settings",
            json={"mode": "live", "api_key": "example-local-test-key", "base_url": "https://model.example/v1", "embedding_enabled": False},
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        fact = MemoryStore().add("我喜欢茉莉花茶。")

        async def finish_after_forgetting(model_client, messages, **kwargs):
            self.assertEqual(delete_memory(fact["id"]), {"ok": True})
            return "你喜欢茉莉花茶，周三还有面试。"

        with patch("companion.service.ChatClient.complete", new=finish_after_forgetting):
            response = self.client.post(
                "/api/chat", json={"message": "记住：我周三有面试。", "request_id": "invalidated-1"}
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get("/api/messages").json()["messages"], [])
        self.assertEqual(MemoryStore().list(), [], "过期请求不能再抽取当前消息中的新事实")
        with get_connection() as conn:
            self.assertIsNone(conn.execute("SELECT id FROM requests WHERE id=?", ("invalidated-1",)).fetchone())

    def test_switching_from_demo_to_live_excludes_demo_context_but_keeps_history(self) -> None:
        from companion.storage import get_state, set_state

        demo_message = "演示独有话题_紫色摩天轮。"
        demo = self.client.post("/api/chat", json={"message": demo_message, "request_id": "boundary-demo"})
        self.assertEqual(demo.status_code, 200, demo.text)
        set_state("summary", "演示独有摘要_蓝色热气球。")
        old_marker = get_state("session_start", "")
        changed = self.client.put(
            "/api/settings",
            json={"mode": "live", "api_key": "example-local-test-key", "base_url": "https://model.example/v1", "embedding_enabled": False},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertNotEqual(get_state("session_start", ""), old_marker)
        self.assertEqual(get_state("summary", ""), "")
        prompts = []

        async def reply_to_new_session(model_client, messages, **kwargs):
            prompts.extend(messages)
            return "这是新会话的真实模型模拟回复。"

        with patch("companion.service.ChatClient.complete", new=reply_to_new_session):
            live = self.client.post("/api/chat", json={"message": "现在开始新的聊天。", "request_id": "boundary-live"})
        self.assertEqual(live.status_code, 200, live.text)
        prompt_text = json.dumps(prompts, ensure_ascii=False)
        self.assertNotIn("演示独有话题", prompt_text)
        self.assertNotIn("演示独有摘要", prompt_text)
        history = self.client.get("/api/messages").json()["messages"]
        self.assertEqual(len(history), 4, "切换模式只清上下文，保留可见聊天历史")
        self.assertEqual([item["mode"] for item in history], ["demo", "demo", "live", "live"])

    def test_reset_during_summary_does_not_restore_old_summary_or_candidate_memories(self) -> None:
        from companion.memory import MemoryStore
        from companion.service import enrich_memory, reset_context
        from companion.storage import get_connection, get_state, now_iso

        configured = self.client.put(
            "/api/settings",
            json={"mode": "live", "api_key": "example-local-test-key", "base_url": "https://model.example/v1", "embedding_enabled": False},
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        rows = [
            (f"summary-{i:03d}", "user" if i % 2 == 0 else "assistant", "我计划周三面试。" if i % 2 == 0 else "祝你顺利。", "live", now_iso())
            for i in range(22)
        ]
        with get_connection() as conn:
            conn.executemany("INSERT INTO messages(id,role,content,mode,created_at) VALUES(?,?,?,?,?)", rows)
        called = []

        async def old_summary_arrives_after_reset(model_client, messages, **kwargs):
            called.append(kwargs.get("purpose"))
            reset_context()
            return json.dumps({
                "summary": "旧会话里用户周三面试。",
                "memories": [{"content": "用户周三面试。", "kind": "event", "evidence": "我计划周三面试"}],
            }, ensure_ascii=False)

        with patch("companion.service.ChatClient.complete", new=old_summary_arrives_after_reset):
            asyncio.run(enrich_memory())
        self.assertEqual(called, ["summary"], "应真正到达摘要模型返回后的失效检查")
        self.assertEqual(get_state("summary", ""), "")
        self.assertEqual(get_state("summary_until", ""), "")
        self.assertEqual(MemoryStore().list(), [])

    def test_history_returns_latest_2000_messages_in_chronological_order(self) -> None:
        from companion.storage import get_connection

        rows = [
            (f"history-{i:04d}", "user" if i % 2 == 0 else "assistant", f"消息 {i}", "demo", f"2026-01-01T00:00:00.{i:06d}+08:00")
            for i in range(2005)
        ]
        with get_connection() as conn:
            conn.executemany("INSERT INTO messages(id,role,content,mode,created_at) VALUES(?,?,?,?,?)", rows)
        response = self.client.get("/api/messages")
        self.assertEqual(response.status_code, 200, response.text)
        history = response.json()["messages"]
        self.assertEqual(len(history), 2000)
        self.assertEqual([item["id"] for item in history], [f"history-{i:04d}" for i in range(5, 2005)])

    def test_vault_sync_changes_invalidate_inflight_replies(self) -> None:
        from companion.api import sync_knowledge
        from companion.knowledge import KnowledgeStore

        configured = self.client.put(
            "/api/settings",
            json={"mode": "live", "api_key": "example-local-test-key", "base_url": "https://model.example/v1", "embedding_enabled": False},
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        for operation in ("change", "delete"):
            with self.subTest(operation=operation):
                vault = Path(self.temp_dir.name) / f"vault-{operation}"
                vault.mkdir()
                note = vault / "周末.md"
                note.write_text("周末饮品安排：我喜欢茉莉花茶。", encoding="utf-8")
                initial = self.client.post("/api/knowledge/sync", json={"vault_path": str(vault)})
                self.assertEqual(initial.status_code, 200, initial.text)

                async def finish_after_sync(model_client, messages, **kwargs):
                    self.assertIn("我喜欢茉莉花茶", json.dumps(messages, ensure_ascii=False))
                    if operation == "change":
                        note.write_text("周末饮品安排：我现在喜欢普洱茶。", encoding="utf-8")
                    else:
                        note.unlink()
                    result = sync_knowledge({"vault_path": str(vault)})
                    self.assertGreater(result["indexed" if operation == "change" else "deleted"], 0)
                    return "周末可以喝茉莉花茶。"

                with patch("companion.service.ChatClient.complete", new=finish_after_sync):
                    response = self.client.post(
                        "/api/chat", json={"message": "周末饮品安排是什么？", "request_id": f"sync-{operation}"}
                    )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.client.get("/api/messages").json()["messages"], [])
                self.assertFalse(KnowledgeStore().search("茉莉花茶"))


if __name__ == "__main__":
    unittest.main()

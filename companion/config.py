from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

from .storage import ROOT, get_state, set_state

load_dotenv(ROOT / ".env", override=False)

DEFAULT_PERSONA = json.loads((ROOT / "personas" / "xiaoyu.json").read_text(encoding="utf-8"))


def settings() -> dict:
    defaults = {
        "mode": os.getenv("COMPANION_MODE", "demo"),
        "provider": os.getenv("COMPANION_PROVIDER", "qwen"),
        "base_url": os.getenv("COMPANION_BASE_URL", ""),
        "model": os.getenv("COMPANION_MODEL", "qwen-plus-character"),
        "summary_model": os.getenv("COMPANION_SUMMARY_MODEL", "qwen-flash"),
        "api_key": os.getenv("COMPANION_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", ""),
        "embedding_model": os.getenv("COMPANION_EMBEDDING_MODEL", "text-embedding-v4"),
        "embedding_enabled": os.getenv("COMPANION_EMBEDDING_ENABLED", "false").lower() == "true",
        "monthly_budget": float(os.getenv("COMPANION_MONTHLY_BUDGET", "50")),
        "input_price_per_million": 0.8, "output_price_per_million": 2.0,
        "summary_input_price_per_million": 1.0, "summary_output_price_per_million": 4.0,
        "embedding_price_per_million": 0.5,
    }
    defaults.update(get_state("settings", {}))
    return defaults


def public_settings() -> dict:
    value = settings()
    value["api_key_configured"] = bool(value.pop("api_key", ""))
    return value


def save_settings(values: dict) -> dict:
    current = settings()
    allowed = set(current)
    for key, value in values.items():
        if key in allowed and (key != "api_key" or value):
            current[key] = value
    if values.get("clear_api_key"):
        current["api_key"] = ""
    if current["mode"] not in {"demo", "live"}:
        raise ValueError("请选择演示模式或真实模型模式。")
    for key in ("provider", "base_url", "model", "summary_model", "embedding_model"):
        if not isinstance(current[key], str) or len(current[key]) > 500:
            raise ValueError("模型配置格式不正确。")
        current[key] = current[key].strip()
    if not isinstance(current["api_key"], str) or len(current["api_key"]) > 2000:
        raise ValueError("API Key 格式不正确。")
    if current["base_url"]:
        current["base_url"] = current["base_url"].rstrip("/")
        if current["base_url"].endswith("/chat/completions"):
            current["base_url"] = current["base_url"][:-len("/chat/completions")]
        url = urlparse(current["base_url"])
        if (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})) or not url.hostname or url.username or url.query or url.fragment or "{" in current["base_url"]:
            raise ValueError("请填写控制台提供的 HTTPS API 基础地址；本地模型允许 localhost HTTP。")
    for key in ("monthly_budget", "input_price_per_million", "output_price_per_million", "summary_input_price_per_million", "summary_output_price_per_million", "embedding_price_per_million"):
        try:
            current[key] = float(current[key])
        except (TypeError, ValueError):
            raise ValueError("预算和估算单价必须是数字。") from None
        if not 0 <= current[key] <= 100000:
            raise ValueError("预算或估算单价超出可用范围。")
    if current["monthly_budget"] <= 0:
        raise ValueError("月预算必须大于 0。")
    current["embedding_enabled"] = bool(current["embedding_enabled"])
    old = settings()
    if (old["embedding_model"], old["base_url"]) != (current["embedding_model"], current["base_url"]):
        from .storage import get_connection
        with get_connection() as conn:
            conn.execute("UPDATE chunks SET embedding=NULL")
            conn.execute("UPDATE memories SET embedding=NULL")
    set_state("settings", current)
    return public_settings()


def persona() -> dict:
    value = dict(DEFAULT_PERSONA)
    value.update(get_state("persona", {}))
    return value


def save_persona(values: dict) -> dict:
    current = persona()
    for key in current:
        value = values.get(key, current[key])
        if not isinstance(value, str) or len(value) > 3000:
            raise ValueError("人物设定每项最多 3000 字。")
        current[key] = value.strip()
    if not current["name"]:
        raise ValueError("请为角色填写名字。")
    set_state("persona", current)
    return current

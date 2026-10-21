from __future__ import annotations

import json
import math

import httpx

from .config import settings
from .storage import get_connection, now_iso


class ModelError(ValueError):
    pass


def usage_stats() -> dict:
    month = now_iso()[:7]
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS calls,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens,COALESCE(SUM(estimated_cost),0) AS estimated_spend FROM usage WHERE created_at LIKE ?", (month + "%",)).fetchone()
    result = dict(row)
    result["estimated_spend"] = round(result["estimated_spend"], 6)
    result["monthly_limit"] = settings()["monthly_budget"]
    result["note"] = "按本地配置单价估算，供应商账单为准；超时但已计费的请求可能不在本地统计中。"
    return result


class ChatClient:
    def __init__(self, config: dict | None = None):
        self.config = config or settings()

    def validate(self) -> None:
        if not self.config.get("api_key"):
            raise ModelError("还没有配置 API Key。请在设置中填写，或切回演示模式。")
        if not self.config.get("base_url"):
            raise ModelError("请从模型控制台复制 OpenAI 兼容 API 基础地址并填入设置。")
        if not self.config.get("model"):
            raise ModelError("请填写主聊模型名称。")

    def _rates(self, purpose: str) -> tuple[float, float]:
        prefix = "summary_" if purpose == "summary" else ""
        return (self.config[prefix + "input_price_per_million"], self.config[prefix + "output_price_per_million"])

    def _budget(self, estimated_cost: float) -> None:
        if usage_stats()["estimated_spend"] + estimated_cost > self.config["monthly_budget"]:
            raise ModelError("已达到本地月预算阈值，本次没有调用模型。可在设置中调整预算。")

    def _record(self, model: str, purpose: str, usage: dict, estimated_input: int, estimated_output: int = 0) -> None:
        in_tokens = max(0, int(usage.get("prompt_tokens", usage.get("input_tokens", estimated_input)) or 0))
        out_tokens = max(0, int(usage.get("completion_tokens", usage.get("output_tokens", estimated_output)) or 0))
        if purpose == "embedding":
            cost = in_tokens * self.config["embedding_price_per_million"] / 1_000_000
        else:
            input_rate, output_rate = self._rates(purpose)
            cost = (in_tokens * input_rate + out_tokens * output_rate) / 1_000_000
        with get_connection() as conn:
            conn.execute("INSERT INTO usage(created_at,model,purpose,input_tokens,output_tokens,estimated_cost,estimated) VALUES(?,?,?,?,?,?,?)", (now_iso(), model, purpose, in_tokens, out_tokens, cost, 1))

    async def _post(self, endpoint: str, payload: dict) -> dict:
        self.validate()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(75, connect=15), follow_redirects=False) as client:
                response = await client.post(self.config["base_url"].rstrip("/") + endpoint,
                                             headers={"Authorization": "Bearer " + self.config["api_key"]}, json=payload)
            if response.status_code in {401, 403}:
                raise ModelError("模型接口拒绝访问，请核对 API Key、地域、模型权限和接口地址。")
            if response.status_code == 429:
                raise ModelError("模型接口限流或额度不足，请稍后再试并检查账户额度。")
            if not response.is_success:
                raise ModelError(f"模型接口返回 HTTP {response.status_code}，请检查模型名称、地域及兼容接口地址。")
            result = response.json()
            if not isinstance(result, dict):
                raise ModelError("模型接口返回的数据格式不正确。")
            return result
        except httpx.TimeoutException:
            raise ModelError("模型接口超时，本次没有自动重试。请稍后重发，避免重复计费。") from None
        except httpx.RequestError:
            raise ModelError("暂时无法连接模型接口，请检查网络和 API 地址。") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ModelError("模型接口没有返回有效 JSON，请检查兼容接口地址。") from None

    async def complete(self, messages: list[dict], model: str | None = None, purpose: str = "chat", json_mode: bool = False, max_tokens: int = 600) -> str:
        self.validate()
        model = model or self.config["model"]
        estimated_input = sum(len(item.get("content", "")) for item in messages) + 80
        input_rate, output_rate = self._rates(purpose)
        self._budget((estimated_input * input_rate + max_tokens * output_rate) / 1_000_000)
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.75 if purpose == "chat" else 0.2}
        if model.startswith("deepseek"):
            payload["thinking"] = {"type": "disabled"}
        elif model.startswith("qwen") and "character" not in model:
            payload["enable_thinking"] = False
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        result = await self._post("/chat/completions", payload)
        try:
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise KeyError("empty")
        except (KeyError, IndexError, TypeError):
            raise ModelError("模型返回了空回复或不兼容的格式，请检查模型设置。") from None
        self._record(model, purpose, result.get("usage") or {}, estimated_input, len(content))
        return content.strip()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        estimated_input = sum(len(text) for text in texts)
        self._budget(estimated_input * self.config["embedding_price_per_million"] / 1_000_000)
        result = await self._post("/embeddings", {"model": self.config["embedding_model"], "input": texts, "encoding_format": "float"})
        try:
            items = sorted(result["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in items]
            if len(vectors) != len(texts) or not vectors or not vectors[0] or any(len(v) != len(vectors[0]) or any(not math.isfinite(x) for x in v) for v in vectors):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ModelError("向量接口返回的数据格式不正确。") from None
        self._record(self.config["embedding_model"], "embedding", result.get("usage") or {}, estimated_input)
        return vectors

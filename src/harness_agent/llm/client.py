"""DeepSeek client (task 1.2): HTTP `chat/completions` tương thích OpenAI, chỉ dùng thư viện chuẩn.

Không thêm dependency (`openai`): endpoint của DeepSeek là JSON đơn giản, `urllib` đủ dùng và không kéo
theo retry/telemetry ngoài tầm kiểm soát. Transport tách riêng (`transport=`) để test không cần mạng.

API key chỉ đọc từ biến môi trường `DEEPSEEK_API_KEY` (hoặc `.env` qua python-dotenv nếu có);
không bao giờ được ghi ra log, cache hay trace.

Lưu ý thiết kế: client này KHÔNG dùng cho B0–B4 (các mức đó chạy qua dsh để giữ runtime cố định,
D-008). Dùng cho: paraphrase corpus (tuỳ chọn), thử prompt, và là phương án dự phòng nếu dsh NO-GO.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import Mode, ReplayCache, request_key

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL_ENV = "DEEPSEEK_MODEL_DEV"
DEFAULT_CACHE_DIR = Path("cache") / "llm"

Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatResponse:
    text: str
    model: str
    usage: dict[str, int]
    cached: bool
    key: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class DeepSeekClient:
    model: str
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    temperature: float | None = 0.0
    max_tokens: int = 2048
    timeout_s: float = 120.0
    cache: ReplayCache | None = None
    transport: Transport | None = None

    @classmethod
    def from_env(
        cls,
        model: str | None = None,
        mode: Mode = "auto",
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
        **kw: Any,
    ) -> DeepSeekClient:
        """Đọc DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL_DEV từ môi trường (và .env nếu có)."""
        try:
            from dotenv import load_dotenv

            load_dotenv()  # không ghi đè biến đã có trong môi trường
        except ImportError:  # pragma: no cover
            pass
        model = model or os.environ.get(DEFAULT_MODEL_ENV) or "deepseek-chat"
        return cls(
            model=model,
            api_key=os.environ.get("DEEPSEEK_API_KEY"),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            cache=ReplayCache(cache_dir, mode),
            **kw,
        )

    # ------------------------------------------------------------------
    def chat(self, messages: list[dict[str, str]], **params: Any) -> ChatResponse:
        for m in messages:
            if set(m) != {"role", "content"} or m["role"] not in ("system", "user", "assistant"):
                raise ValueError(f"message không hợp lệ: {m!r}")
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            **({"temperature": self.temperature} if self.temperature is not None else {}),
            **params,
        }
        key = request_key(request)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return self._parse(hit, key, cached=True)
        if not self.api_key:
            raise LLMError("chưa có DEEPSEEK_API_KEY (và cache không có bản ghi cho request này)")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        transport = self.transport or _urllib_transport
        response = transport(f"{self.base_url}/chat/completions", headers, request, self.timeout_s)
        if self.cache is not None:
            self.cache.put(key, request, response)
        return self._parse(response, key, cached=False)

    def complete(self, prompt: str, system: str | None = None, **params: Any) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        return self.chat(messages, **params).text

    @staticmethod
    def _parse(response: dict[str, Any], key: str, cached: bool) -> ChatResponse:
        try:
            text = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"response không có choices[0].message.content: {list(response)[:5]}") from exc
        usage = {k: int(v) for k, v in (response.get("usage") or {}).items() if isinstance(v, int)}
        return ChatResponse(
            text=text or "",
            model=str(response.get("model", "")),
            usage=usage,
            cached=cached,
            key=key,
            raw=response,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Không theo redirect. urllib mặc định theo 301/302/303 kể cả sang http:// và GIỮ header Authorization,
    nên một redirect từ endpoint (hoặc proxy) có thể làm lộ API key (review 01/10). API không cần redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, f"redirect bị từ chối (tới {newurl})", headers, fp)


# Opener riêng: không redirect. ProxyHandler mặc định vẫn có (HTTPS_PROXY của môi trường).
_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib_transport(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    if not url.startswith("https://"):
        # Chỉ https: key đi trong header Authorization. (Cũng là lý do không cho `file:`/scheme lạ.)
        raise LLMError(f"base_url phải là https: {url}")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310 — đã ép https
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LLMError(f"HTTP {exc.code} từ {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LLMError(f"không gọi được {url}: {exc}") from exc

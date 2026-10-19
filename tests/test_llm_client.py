"""DeepSeek client + record/replay (task 1.2). Không gọi mạng: transport giả."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_agent.llm import CacheMiss, DeepSeekClient, LLMError, ReplayCache
from harness_agent.llm.cache import request_key
from harness_agent.llm.client import _urllib_transport


def _fake_transport(calls: list):
    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body})
        return {
            "id": "x",
            "model": body["model"],
            "choices": [
                {"message": {"role": "assistant", "content": f"echo: {body['messages'][-1]['content']}"}}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    return transport


def _client(tmp_path: Path, mode: str, calls: list, key: str | None = "sk-test") -> DeepSeekClient:
    return DeepSeekClient(
        model="deepseek-chat",
        api_key=key,
        cache=ReplayCache(tmp_path / "cache", mode),
        transport=_fake_transport(calls),
    )


def test_key_is_deterministic_and_order_independent():
    a = request_key({"model": "m", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1})
    b = request_key({"max_tokens": 1, "messages": [{"role": "user", "content": "x"}], "model": "m"})
    c = request_key({"model": "m", "messages": [{"role": "user", "content": "y"}], "max_tokens": 1})
    assert a == b != c and len(a) == 64


def test_auto_mode_records_then_replays(tmp_path):
    calls: list = []
    c = _client(tmp_path, "auto", calls)
    r1 = c.complete("xin chào")
    r2 = c.complete("xin chào")
    assert r1 == r2 == "echo: xin chào" and len(calls) == 1
    files = list((tmp_path / "cache").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["request"]["messages"][0]["content"] == "xin chào"
    assert "sk-test" not in files[0].read_text(encoding="utf-8")  # key không bao giờ vào cache


def test_replay_mode_never_calls_network(tmp_path):
    calls: list = []
    _client(tmp_path, "record", calls).complete("a")
    assert len(calls) == 1
    r = _client(tmp_path, "replay", calls, key=None).chat([{"role": "user", "content": "a"}])
    assert r.cached is True and r.text == "echo: a" and len(calls) == 1
    with pytest.raises(CacheMiss):
        _client(tmp_path, "replay", calls, key=None).complete("b")
    assert len(calls) == 1


def test_record_mode_always_calls_and_overwrites(tmp_path):
    calls: list = []
    c = _client(tmp_path, "record", calls)
    c.complete("a")
    c.complete("a")
    assert len(calls) == 2


def test_off_mode_writes_nothing(tmp_path):
    calls: list = []
    _client(tmp_path, "off", calls).complete("a")
    assert not (tmp_path / "cache").exists() and len(calls) == 1


def test_missing_api_key_without_cache_is_an_error(tmp_path):
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        _client(tmp_path, "auto", [], key=None).complete("a")


def test_request_shape_and_auth_header(tmp_path):
    calls: list = []
    c = _client(tmp_path, "off", calls)
    c.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], temperature=0.7)
    body, headers = calls[0]["body"], calls[0]["headers"]
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert body["model"] == "deepseek-chat" and body["temperature"] == 0.7 and body["max_tokens"] == 2048
    with pytest.raises(ValueError):
        c.chat([{"role": "tool", "content": "x"}])


def test_transport_refuses_non_https():
    with pytest.raises(LLMError, match="https"):
        _urllib_transport("http://example.invalid/chat/completions", {}, {}, 1.0)


def test_from_env_reads_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid/")
    monkeypatch.setenv("DEEPSEEK_MODEL_DEV", "deepseek-v4-flash")
    c = DeepSeekClient.from_env(mode="replay", cache_dir=tmp_path)
    assert (c.api_key, c.base_url, c.model, c.cache.mode) == (
        "sk-env",
        "https://example.invalid",
        "deepseek-v4-flash",
        "replay",
    )


def test_redirects_are_refused_so_the_key_never_follows():
    """urllib mặc định theo 302 sang http:// và giữ header Authorization. Opener của client phải từ chối."""
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    from harness_agent.llm import client as client_mod

    handler = next(
        h for h in client_mod._OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    )
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=b"{}", headers={"Authorization": "Bearer SECRET"}
    )
    with pytest.raises(urllib.error.HTTPError, match="redirect bị từ chối"):
        handler.redirect_request(req, io.BytesIO(), 302, "Found", Message(), "http://evil.example/x")
    # Đối chứng âm: handler mặc định của urllib CHUYỂN TIẾP header sang http://
    leaked = urllib.request.HTTPRedirectHandler().redirect_request(
        req, io.BytesIO(), 302, "Found", Message(), "http://evil.example/x"
    )
    assert leaked is not None and leaked.full_url.startswith("http://")

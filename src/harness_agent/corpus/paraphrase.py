"""Paraphrase intent (task 2.4): biến thể diễn đạt KHÔNG đổi nghĩa, để đo độ ổn định của agent với cách nói.

Hai chế độ:
- `template` (mặc định, tất định): biến thể bằng quy tắc (lịch sự/ngắn gọn, đổi thứ tự vế, tiếng Anh ↔ Việt
  đã có sẵn trong generator). Không cần mạng.
- `llm` (tuỳ chọn): gọi DeepSeek qua `harness_agent.llm` với record/replay, nhiệt độ 0; LLM chỉ viết lại câu,
  KHÔNG BAO GIỜ sinh hay sửa nhãn. Bản viết lại được kiểm tra giữ nguyên mọi "token cứng" (tên thiết bị,
  cổng, số VLAN, IP, prefix, tên ACL/list/key_ref) — khác là loại bỏ, giữ intent gốc.
"""

from __future__ import annotations

import random
import re

from harness_agent.models import TestCase

_HARD = re.compile(
    r"(GigabitEthernet\d+(/\d+){1,2}|FastEthernet\d+(/\d+){1,2}|Vlan\d+|Loopback\d+|"
    r"\b\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?\b|\b[A-Z][A-Z0-9-]{1,}\b|\b\d+\b)"
)

_VI_PREFIX = ["", "Nhờ bạn ", "Hãy ", "Cần ", "Vui lòng "]
_VI_SUFFIX = ["", " giúp tôi", " nhé", "."]
_EN_PREFIX = ["", "Please ", "Could you ", "I need you to "]
_EN_SUFFIX = ["", " please", ".", ", thanks"]


def hard_tokens(text: str) -> list[str]:
    return sorted({m.group(0) for m in _HARD.finditer(text)})


def template_variant(intent: str, rng: random.Random) -> str:
    """Biến thể theo quy tắc: thêm tiền tố/hậu tố, hạ chữ cái đầu khi có tiền tố. Giữ nguyên token cứng."""
    is_vi = any(ch in intent for ch in "ăâđêôơưàáảãạ")
    pre = rng.choice(_VI_PREFIX if is_vi else _EN_PREFIX)
    suf = rng.choice(_VI_SUFFIX if is_vi else _EN_SUFFIX)
    body = intent
    if pre and body and body[0].isupper() and not body.split()[0].isupper():
        body = body[0].lower() + body[1:]
    out = f"{pre}{body}{suf}".strip()
    return out if hard_tokens(out) == hard_tokens(intent) else intent


def paraphrase_case(case: TestCase, rng: random.Random, mode: str = "template", client=None) -> TestCase:
    if mode == "template":
        new = template_variant(case.intent, rng)
    elif mode == "llm":
        if client is None:
            raise ValueError("mode=llm cần client (harness_agent.llm.DeepSeekClient)")
        new = _llm_variant(case.intent, client)
    else:
        raise ValueError(f"mode không hợp lệ: {mode!r}")
    if hard_tokens(new) != hard_tokens(case.intent):
        return case  # không đổi nghĩa được bảo đảm → giữ nguyên
    return case.model_copy(update={"intent": new, "intent_template": case.intent_template or case.intent})


_SYSTEM = (
    "Rewrite the user's request in the same language with different wording. Keep every device name, "
    "interface name, number, IP address, prefix and identifier EXACTLY as written. "
    "Output only the rewritten sentence."
)


def _llm_variant(intent: str, client) -> str:
    text = client.complete(intent, system=_SYSTEM).strip().strip('"')
    return text.split("\n")[0] if text else intent

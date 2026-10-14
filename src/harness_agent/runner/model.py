"""Model mà runner ghim cho mọi lượt chạy dsh (D-020).

`config/model.toml` (được commit) là nơi DUY NHẤT khai model. Runner ghi nó vào patch của từng stage, ở
row `agent-default-model` của dsh, rồi kiểm tra cấu hình request do dsh ghi trong session log
ở từng stage (D-045).
Đây là kiểm tra tính nhất quán của runtime, không phải chứng thực model từ provider.

Tên khoá lấy từ tài liệu của dsh 0.2.0-rc.1, không đoán:
- README của `@deepseek-ai/dsh-agent-default-model`: row `agent-default-model` có config `provider`
  (bắt buộc), `model` (bắt buộc), `reasoningEffort` (tuỳ chọn). Patch thay TOÀN BỘ config của row.
- README của `@deepseek-ai/dsh-llm-deepseek`: `reasoningEffort` nhận `off | low | high | max`. `low`,
  `high`, `max` bật thinking; `off` làm adapter gửi `thinking.type: disabled`. Không có khoá bật/tắt
  thinking riêng ở row này, nên `thinking` trong model.toml phải khớp với `reasoning_effort`.

Đã kiểm trên dsh thật, không có API key (01/10/2026): `dsh --dump-config` hiện đúng row đã patch, và
`request/header.config` trong session log mang đúng `provider`, `model`, `reasoningEffort` đã ghim.

Trạng thái: ĐỀ XUẤT (D-020).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dsh_home import RequestEvidence

MODEL_ROW_ID = "agent-default-model"
EFFORTS = ("off", "low", "high", "max")
_KEYS = {"provider", "name", "reasoning_effort", "thinking"}
# Giá trị này đi vào file patch của dsh (là CODE, F7): chỉ nhận tên dạng định danh.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


class ModelConfigError(Exception):
    """config/model.toml thiếu, sai định dạng, hoặc tự mâu thuẫn. Là lỗi cấu hình, phải sửa trước khi chạy."""


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    name: str
    reasoning_effort: str
    thinking: bool

    def patch_row(self) -> dict[str, Any]:
        """Row ghi vào patch của dsh. Khai đủ cả ba khoá vì patch thay toàn bộ config của row."""
        config = {"provider": self.provider, "model": self.name, "reasoningEffort": self.reasoning_effort}
        return {"id": MODEL_ROW_ID, "config": config}

    def describe(self) -> str:
        thinking = "bật" if self.thinking else "tắt"
        return f"{self.provider}/{self.name}, reasoning_effort={self.reasoning_effort}, thinking {thinking}"


def load_model_config(path: Path) -> ModelConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ModelConfigError(f"không thấy {path}: file này khai model cho mọi lượt chạy (D-020)") from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ModelConfigError(f"{path} không đọc được: {exc}") from exc
    table = data.get("model")
    if not isinstance(table, dict):
        raise ModelConfigError(f"{path} thiếu bảng [model]")
    if set(table) != _KEYS:
        missing, extra = sorted(_KEYS - set(table)), sorted(set(table) - _KEYS)
        raise ModelConfigError(f"{path} [model]: thiếu khoá {missing}, thừa khoá {extra}")
    provider, name = table["provider"], table["name"]
    effort, thinking = table["reasoning_effort"], table["thinking"]
    for key, value in (("provider", provider), ("name", name)):
        if not isinstance(value, str) or not _NAME_RE.match(value):
            raise ModelConfigError(f"{path} [model] {key} không hợp lệ: {value!r}")
    if effort not in EFFORTS:
        raise ModelConfigError(
            f"{path} [model] reasoning_effort phải là một trong {EFFORTS}, đang là {effort!r}"
        )
    if not isinstance(thinking, bool):
        raise ModelConfigError(f"{path} [model] thinking phải là true hoặc false")
    if thinking != (effort != "off"):
        raise ModelConfigError(
            f"{path} [model]: thinking = {str(thinking).lower()} không khớp reasoning_effort = {effort!r}. "
            'dsh bật thinking với low/high/max và tắt bằng "off"; hai khoá phải nói cùng một điều.'
        )
    return ModelConfig(provider=provider, name=name, reasoning_effort=effort, thinking=thinking)


_FIELD_RE = re.compile(r"^\s+(provider|model|reasoningEffort):\s*(.*?)\s*$")


def composed_model(dump: str) -> dict[str, str] | None:
    """Config của row `agent-default-model` trong output của `dsh --dump-config`; None nếu không thấy row.

    Output đó là YAML có tag `!!js` nên không nạp bằng `yaml.safe_load` được; chỉ đọc đúng row cần xem.
    """
    lines = dump.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"- id: {MODEL_ROW_ID}":
            continue
        found: dict[str, str] = {}
        for follow in lines[index + 1 :]:
            if follow.lstrip().startswith(("- ", "#")):
                break
            match = _FIELD_RE.match(follow)
            if match:
                found[match.group(1)] = match.group(2).strip("'\"")
        return found
    return None


def pin_problem(pinned: ModelConfig, evidence: RequestEvidence) -> str | None:
    """Check every reported request, including provider and effort (thinking is derived).

    Return fixed reason categories; do not echo arbitrary runtime text in errors.
    """
    if evidence.issues or not evidence.configs:
        return "unverified"
    expected = {
        "provider": pinned.provider,
        "model": pinned.name,
        "reasoning_effort": pinned.reasoning_effort,
    }
    return "mismatch" if any(row != expected for row in evidence.configs) else None

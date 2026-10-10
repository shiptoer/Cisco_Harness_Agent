"""Các mức ablation B0–B4 (P:D-020 / MERGE-001). Một nơi duy nhất định nghĩa mức nào bật control nào.

| Mức | Pipeline | (i) context validation | (ii) projection | (iii) gate | (iv) sensors |
| --- | --- | --- | --- | --- | --- |
| B0 | LLM sinh CLI trực tiếp (1 lượt dsh, không tool) | — | — | — | — |
| B1 | CBA → TSA + tool registry + Jinja2 | — | — (context thô trong prompt) | chỉ G1+G2 (cấu trúc) | — |
| B2 | B1 | invariants | tool đọc theo allowlist | chỉ G1+G2 | — |
| B3 | B2 | invariants | allowlist | G1–G6 | — |
| B4 | B3 | invariants | allowlist | G1–G6 | syntax → Batfish → CIS |

Ghi chú về ranh giới (phải nêu trong Ch3):
- Validate CẤU TRÚC (contract pydantic, G1 tool có trong registry, G2 args_model) là định dạng dữ liệu
  của pipeline hai giai đoạn — không có nó thì không render được — nên có mặt từ B1. "(i)" trong
  ablation là phần NGỮ NGHĨA: domain invariants của context. "(iii)" là G3–G6.
- Ở mọi mức, gate vẫn CHẠY đủ 6 bước và sensors vẫn CHẠY (shadow) để trace ghi lại; chỉ các bước
  được mức đó bật mới quyết định. Trace (v) là nguồn đo, không phải biến ablation.
- Runtime (dsh), model, prompt luật an toàn, template: giữ cố định ở mọi mức (D-008 điều kiện 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Level(StrEnum):
    B0 = "B0"
    B1 = "B1"
    B2 = "B2"
    B3 = "B3"
    B4 = "B4"


@dataclass(frozen=True)
class LevelSpec:
    level: Level
    two_stage: bool  # False = B0 (LLM sinh CLI trực tiếp)
    context_validation: bool  # (i): domain invariants
    projection: bool  # (ii): tool đọc theo allowlist thay vì context thô trong prompt
    gate: bool  # (iii): G3–G6 cưỡng chế (G1, G2 luôn cưỡng chế ở B1+)
    sensors: bool  # (iv): sensors chặn change package

    @property
    def enforced_gate_steps(self) -> tuple[str, ...]:
        if not self.two_stage:
            return ()
        if self.gate:
            return (
                "G1_whitelist",
                "G2_schema",
                "G3_rbac",
                "G4_blast_radius",
                "G5_intent_consistency",
                "G6_injection_scan",
            )
        return ("G1_whitelist", "G2_schema")


LEVELS: dict[Level, LevelSpec] = {
    Level.B0: LevelSpec(Level.B0, False, False, False, False, False),
    Level.B1: LevelSpec(Level.B1, True, False, False, False, False),
    Level.B2: LevelSpec(Level.B2, True, True, True, False, False),
    Level.B3: LevelSpec(Level.B3, True, True, True, True, False),
    Level.B4: LevelSpec(Level.B4, True, True, True, True, True),
}

LEVEL_NAMES: tuple[str, ...] = tuple(lv.value for lv in Level)
DEFAULT_LEVEL = Level.B4


def spec_of(level: str | Level) -> LevelSpec:
    try:
        return LEVELS[Level(level)]
    except ValueError as exc:
        raise ValueError(f"level không xác định: {level!r} (hợp lệ: {', '.join(LEVEL_NAMES)})") from exc


# Input/session exposure at B0/B1 is included in D leak_sources; it is not evidence of an attack.

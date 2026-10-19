"""Các mức ablation B0–B4 (P:D-020 / MERGE-001). Một nơi duy nhất định nghĩa mức nào bật control nào.

| Mức | Pipeline | (i) context validation | (ii) projection | (iii) gate | (iv) sensors |
| --- | --- | --- | --- | --- | --- |
| B0 | LLM sinh CLI trực tiếp (1 lượt dsh, không tool) | — | — | — | — |
| B1 | CBA → TSA + tool registry + Jinja2 | — | — (context thô trong prompt) | chỉ G1+G2 (cấu trúc) | — |
| B2 | B1 | invariants | tool đọc theo allowlist | chỉ G1+G2 | — |
| B3 | B2 | invariants | allowlist | G1–G6 + intent grounding (D-061) | — |
| B4 | B3 | invariants | allowlist | G1–G6 + intent grounding (D-061) | syntax → Batfish → CIS |

Ghi chú về ranh giới (phải nêu trong Ch3):
- Validate CẤU TRÚC (contract pydantic, G1 tool có trong registry, G2 args_model) là định dạng dữ liệu
  của pipeline hai giai đoạn — không có nó thì không render được — nên có mặt từ B1. "(i)" trong
  ablation là phần NGỮ NGHĨA: domain invariants của context. "(iii)" là G3–G6.
- Ở mọi mức, gate vẫn CHẠY đủ 6 bước và sensors vẫn CHẠY (shadow) để trace ghi lại; chỉ các bước
  được mức đó bật mới quyết định. Trace (v) là nguồn đo, không phải biến ablation.
- Intent grounding (D-061) chạy ở runner sau CBA, ở mọi mức hai giai đoạn; chỉ cưỡng chế ở B3, B4 (cần GVHD
  duyệt: định nghĩa B3/B4 đổi so với Ch1–2).
- Runtime (dsh), model, prompt luật an toàn, template: giữ cố định ở mọi mức (D-008 điều kiện 7).
"""

from __future__ import annotations

import argparse
import sys
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
    # Intent grounding ở runner (D-061): giá trị thực thể trong ChangeRequest phải neo được vào intent gốc.
    # Cưỡng chế từ B3 (cùng mức với gate G3–G6, vì nó đóng vòng tròn của G5); B1, B2 chạy shadow (chỉ ghi
    # trace). Không nằm trong `controls`: nhãn (i)–(v) giữ đúng định nghĩa B0–B4 của Ch1–2; result.json đọc
    # cờ này qua `level`.
    grounding: bool

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

    @property
    def controls(self) -> tuple[str, ...]:
        """Control có mặt ở mức này, ghi vào result.json để dữ liệu tự mô tả. (v) trace có ở mọi mức. Kiểm
        cấu trúc G1+G2 ở B1 không tính là (iii): (iii) là G3–G6."""
        return tuple(
            name
            for name, on in (
                ("i", self.context_validation),
                ("ii", self.projection),
                ("iii", self.gate),
                ("iv", self.sensors),
                ("v", True),
            )
            if on
        )


LEVELS: dict[Level, LevelSpec] = {
    Level.B0: LevelSpec(Level.B0, False, False, False, False, False, False),
    Level.B1: LevelSpec(Level.B1, True, False, False, False, False, False),
    Level.B2: LevelSpec(Level.B2, True, True, True, False, False, False),
    Level.B3: LevelSpec(Level.B3, True, True, True, True, False, True),
    Level.B4: LevelSpec(Level.B4, True, True, True, True, True, True),
}

LEVEL_NAMES: tuple[str, ...] = tuple(lv.value for lv in Level)
# Mặc định B3 (D-049): chạy khô và lệnh cũ giữ nguyên hành vi trước khi gộp. B4 phải chọn rõ bằng
# `--level B4`.
DEFAULT_LEVEL = Level.B3


def spec_of(level: str | Level) -> LevelSpec:
    try:
        return LEVELS[Level(level)]
    except ValueError as exc:
        raise ValueError(f"level không xác định: {level!r} (hợp lệ: {', '.join(LEVEL_NAMES)})") from exc


# Input/session exposure at B0/B1 is included in D leak_sources; it is not evidence of an attack.


# Tên cũ của `--level` (D-044 chỉ có B0 và B3). Giữ làm alias có cảnh báo; bỏ sau khi mọi script đã đổi.
CONDITION_ALIASES: tuple[str, ...] = ("B0", "B3")


def add_level_arguments(ap: argparse.ArgumentParser) -> None:
    """`--level B0..B4` (mặc định B3) và alias cũ `--condition B0|B3`, dùng chung cho mọi CLI chạy case."""
    ap.add_argument(
        "--level",
        choices=LEVEL_NAMES,
        default=None,
        help=f"mức ablation B0–B4 (mặc định {DEFAULT_LEVEL.value}); xem harness_agent.levels",
    )
    ap.add_argument(
        "--condition",
        choices=CONDITION_ALIASES,
        default=None,
        help="ĐÃ BỎ, dùng --level. Alias: B0 → --level B0, B3 → --level B3",
    )


def level_from_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> Level:
    """Mức của lượt chạy từ `--level` / `--condition`. Hai cờ cùng có mà khác giá trị là lỗi dòng lệnh."""
    if args.condition is not None:
        print("[CẢNH BÁO] --condition đã bỏ, dùng --level", file=sys.stderr)
        if args.level is not None and args.level != args.condition:
            ap.error(f"--level {args.level} và --condition {args.condition} mâu thuẫn")
        return Level(args.condition)
    return Level(args.level) if args.level is not None else DEFAULT_LEVEL

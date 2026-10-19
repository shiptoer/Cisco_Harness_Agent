"""B0 (D-044): văn phạm của câu trả lời. Parse chặt, không đoán: lệch văn phạm là lỗi có mã.

Đối chứng âm đi theo cặp: mỗi chuỗi bị từ chối có một chuỗi gần giống được nhận.
"""

from __future__ import annotations

import pytest

from harness_agent.levels import Level, spec_of
from harness_agent.runner.b0 import MAX_RESPONSE_BYTES, B0ParseError, parse_response

VLAN = "```cisco SW2\nvlan 30\n name FINANCE\nend\n```\n"
ROUTE = "```cisco R1\nip route 10.20.0.0 255.255.0.0 10.0.12.2\nend\n```\n"


def _kind(text: str) -> str:
    with pytest.raises(B0ParseError) as caught:
        parse_response(text)
    return caught.value.kind


def test_one_config_block():
    got = parse_response(VLAN)
    assert (got.kind, got.configs, got.text) == ("config", {"SW2": "vlan 30\n name FINANCE\nend\n"}, "")


def test_several_devices_keep_their_order_and_blank_lines_between_blocks_are_allowed():
    got = parse_response("\n" + VLAN + "\n\n" + ROUTE + "\n")
    assert list(got.configs) == ["SW2", "R1"]
    assert got.configs["R1"] == "ip route 10.20.0.0 255.255.0.0 10.0.12.2\nend\n"


def test_crlf_becomes_lf_and_indentation_is_kept():
    got = parse_response(VLAN.replace("\n", "\r\n"))
    assert got.configs == {"SW2": "vlan 30\n name FINANCE\nend\n"}


@pytest.mark.parametrize(("label", "kind"), [("clarify", "clarify"), ("refuse", "refuse")])
def test_clarify_and_refuse(label: str, kind: str):
    got = parse_response(f"```{label}\nVLAN 40 trên thiết bị nào?\n```")
    assert (got.kind, got.text, got.configs) == (kind, "VLAN 40 trên thiết bị nào?\n", {})


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Here is the configuration:\n" + VLAN, "text_outside"),
        (VLAN + "Let me know if you need anything else.\n", "text_outside"),
        (VLAN + "```clarify\nSW1 nữa không?\n```\n", "mixed_blocks"),
        ("```clarify\na?\n```\n```clarify\nb?\n```\n", "mixed_blocks"),
        ("```refuse\na\n```\n```refuse\nb\n```\n", "mixed_blocks"),
        (VLAN + VLAN, "duplicate_host"),
        (VLAN + VLAN.replace("SW2", "sw2"), "duplicate_host"),
        ("```cisco SW2\n\n   \n```\n", "empty_block"),
        ("```clarify\n```\n", "empty_block"),
        ("```cisco\nvlan 30\n```\n", "bad_hostname"),
        ("```cisco ../x\nvlan 30\n```\n", "bad_hostname"),
        ("```cisco SW2 SW1\nvlan 30\n```\n", "bad_hostname"),
        ("```cisco a/b\nvlan 30\n```\n", "bad_hostname"),
        ("```cisco SW2.cfg\nvlan 30\n```\n", "bad_hostname"),
        ("```cisco NUL\nvlan 30\n```\n", "bad_hostname"),
        ("```cisco " + "a" * 64 + "\nvlan 30\n```\n", "bad_hostname"),
        ("```\nvlan 30\n```\n", "bad_fence"),
        ("```text\nvlan 30\n```\n", "bad_fence"),
        ("```Cisco SW2\nvlan 30\n```\n", "bad_fence"),
        ("```clarify SW2\ncâu hỏi\n```\n", "bad_fence"),
        ("```cisco SW2\nvlan 30\n```cisco R1\nend\n```\n", "bad_fence"),
        ("```cisco SW2\nvlan 30\nend\n", "unclosed"),
        ("", "no_block"),
        ("\n  \n", "no_block"),
        (" " + VLAN, "text_outside"),  # dấu mở phải đứng đầu dòng
        # F12: không có tool, model tự viết lệnh gọi tool dạng text.
        ('<invoke name="bash"><parameter name="command">ls -la</parameter></invoke>', "text_outside"),
    ],
)
def test_anything_off_grammar_is_rejected_with_a_kind(text: str, kind: str):
    assert _kind(text) == kind


def test_negative_control_the_same_cli_inside_a_block_is_accepted():
    """Đối chứng âm cho `text_outside`: cùng nội dung, đặt trong khối thì được nhận."""
    prose = "vlan 30\n name FINANCE\nend"
    assert _kind(prose) == "text_outside"
    assert parse_response(f"```cisco SW2\n{prose}\n```").configs == {"SW2": prose + "\n"}


def test_size_limit():
    filler = "!" * 1000 + "\n"
    under = "```cisco R1\n" + filler * 200 + "```\n"
    assert len(under.encode()) < MAX_RESPONSE_BYTES
    assert parse_response(under).kind == "config"
    assert _kind("```cisco R1\n" + filler * 300 + "```\n") == "too_large"


def test_the_error_carries_no_model_text():
    marker = "MARKER-b0-q3"
    with pytest.raises(B0ParseError) as caught:
        parse_response(f"{marker}\n" + VLAN)
    assert marker not in str(caught.value) and marker not in repr(caught.value)


def test_hostname_is_not_checked_against_the_context():
    """B0 không có control (i): một thiết bị không tồn tại vẫn qua parse; evaluator mới chấm."""
    assert list(parse_response("```cisco NO-SUCH-DEVICE\nend\n```").configs) == ["NO-SUCH-DEVICE"]


# Viết lại 13/10/2026 (D-049): `Condition` (B0, B3) và `CONTROLS` của D-044 thay bằng `Level` và
# `LevelSpec.controls` của `harness_agent.levels`. Hai mức cũ giữ nguyên giá trị.
def test_levels_and_their_controls():
    assert [lv.value for lv in Level] == ["B0", "B1", "B2", "B3", "B4"]
    assert {lv.value: spec_of(lv).controls for lv in Level} == {
        "B0": ("v",),
        "B1": ("v",),
        "B2": ("i", "ii", "v"),
        "B3": ("i", "ii", "iii", "v"),
        "B4": ("i", "ii", "iii", "iv", "v"),
    }

"""Test cơ chế phát hiện và che secret dùng chung cho harness và runner (D-018)."""

from __future__ import annotations

import hashlib
import json

from harness_agent.harness.leak import REDACTED, redact_stream, redact_value, secret_lines, secret_paths

S1 = "CANARY-device-a-11111111"
S2 = "CANARY-device-b-22222222"
SECRETS = {S1, S2}


# ------------------------------------------------------------ secret_paths
def test_paths_cover_values_keys_and_nesting():
    call = {
        "hostname": S1,
        "clean": "SW2",
        "changes": [{"args": {"name": f"x{S2}y", "vlan_id": 30}}, ["ok", S1]],
        S2: {"inner": S1},
    }
    assert secret_paths(call, SECRETS, "args") == [
        "args.hostname",
        "args.changes[0].args.name",
        "args.changes[1][1]",
        "args.<key>",
        "args.<key>.inner",
    ]


def test_paths_of_a_plain_string_use_the_root_name():
    assert secret_paths(f"x{S1}", SECRETS, "tool_name") == ["tool_name"]
    assert secret_paths("get_device", SECRETS, "tool_name") == []


def test_paths_ignore_non_strings_and_empty_secret_sets():
    assert secret_paths({"a": 1, "b": None, "c": True, "d": 1.5}, SECRETS, "args") == []
    assert secret_paths({"a": S1}, set(), "args") == []


def test_paths_never_contain_a_secret():
    """Đối chứng cho bước che đường dẫn: tên các khoá ghép lại trùng đúng một secret."""
    declared = "args.password"
    paths = secret_paths({"password": "x"}, {declared}, "args")
    assert paths == []  # khoá "password" không chứa secret, giá trị "x" cũng không
    paths = secret_paths({"password": declared}, {declared}, "args")
    assert paths == [REDACTED] and declared not in "".join(paths)


# ------------------------------------------------------------ redact_value
def test_redact_value_replaces_in_keys_and_values_only():
    value = {"a": f"key {S1}", S2: [S1, 5, None], "clean": "SW2"}
    assert redact_value(value, SECRETS) == {
        "a": f"key {REDACTED}",
        REDACTED: [REDACTED, 5, None],
        "clean": "SW2",
    }
    assert value["a"] == f"key {S1}"  # không sửa object gốc


def test_redact_value_handles_a_secret_inside_another():
    longer = S1 + "-tail"
    assert redact_value(longer, {S1, longer}) == REDACTED


# ------------------------------------------------------------ redact_stream
def _ndjson(*events: dict) -> bytes:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")


def _positions(found: list[dict]) -> list[dict]:
    """Vị trí không kèm `sha256` (hash của dòng gốc được kiểm tra riêng ở test ngay dưới)."""
    assert all(len(f["sha256"]) == 64 for f in found)
    return [{k: v for k, v in f.items() if k != "sha256"} for f in found]


def test_stream_positions_carry_the_hash_of_the_original_line():
    """`sha256` là hash của dòng gốc trước khi che, không gồm ký tự kết thúc dòng (LF hoặc CRLF)."""
    json_line = json.dumps({"type": "tool_call", "input": {"hostname": S1}})
    text_line = f"warning: key={S2}"
    raw = f"clean\r\n{json_line}\r\n{text_line}\n".encode()

    out, found = redact_stream(raw, SECRETS)

    assert [(f["line"], f["sha256"]) for f in found] == [
        (2, hashlib.sha256(json_line.encode()).hexdigest()),
        (3, hashlib.sha256(text_line.encode()).hexdigest()),
    ]
    # Hash là của dòng GỐC: khác hash của dòng đã che nằm trong file.
    saved = [x.removesuffix(b"\r") for x in out.split(b"\n")]
    assert hashlib.sha256(saved[1]).hexdigest() != found[0]["sha256"]
    assert hashlib.sha256(saved[2]).hexdigest() != found[1]["sha256"]
    assert S1 not in json.dumps(found) and S2 not in json.dumps(found)


def test_stream_reports_positions_then_redacts():
    raw = _ndjson(
        {"type": "session", "sessionId": "x"},
        {"type": "tool_call", "tool": "get_device", "input": {"hostname": S1}},
        {"type": "tool_result", "result": f"no device {S1}"},
        {"type": "final", "text": "done"},
    )
    out, found = redact_stream(raw, SECRETS)

    assert _positions(found) == [
        {"line": 2, "event": "tool_call", "fields": ["input.hostname"]},
        {"line": 3, "event": "tool_result", "fields": ["result"]},
    ]
    assert S1.encode() not in out
    lines_in, lines_out = raw.split(b"\n"), out.split(b"\n")
    assert len(lines_in) == len(lines_out)
    # Dòng không có secret giữ nguyên từng byte; dòng có secret vẫn là JSON hợp lệ.
    assert (lines_out[0], lines_out[3], lines_out[4]) == (lines_in[0], lines_in[3], lines_in[4])
    assert json.loads(lines_out[1])["input"] == {"hostname": REDACTED}
    assert json.loads(lines_out[2])["result"] == f"no device {REDACTED}"


def test_stream_without_secrets_is_returned_unchanged():
    raw = _ndjson({"type": "final", "text": "done"})
    assert redact_stream(raw, SECRETS) == (raw, [])
    assert redact_stream(raw, set()) == (raw, [])
    assert redact_stream(b"", SECRETS) == (b"", [])


def test_stream_catches_a_secret_escaped_by_the_serializer():
    """Runtime có thể escape ký tự theo cách khác Python (dạng u + 4 chữ số hex). So theo byte sẽ sót;
    phải so sau khi parse JSON."""
    escaped = "".join(f"{chr(92)}u{ord(c):04x}" for c in S1)
    raw = ('{"type": "tool_call", "input": {"q": "' + escaped + '"}}\n').encode("ascii")
    assert S1.encode() not in raw  # đối chứng: dạng nguyên văn không có trong luồng

    out, found = redact_stream(raw, SECRETS)

    assert _positions(found) == [{"line": 1, "event": "tool_call", "fields": ["input.q"]}]
    assert json.loads(out)["input"]["q"] == REDACTED
    assert escaped.encode() not in out


def test_stream_redacts_lines_that_are_not_json():
    odd = 'p"w$1'  # secret có ký tự bị JSON escape
    cut = '{"type": "tool_call", "input": {"q": ' + json.dumps(odd)  # dòng JSON bị cắt
    raw = f"warning: key={S1}\r\n{cut}\nclean line\n".encode()

    out, found = redact_stream(raw, {S1, odd})

    assert _positions(found) == [{"line": 1}, {"line": 2}]
    assert out.split(b"\n") == [
        f"warning: key={REDACTED}\r".encode(),
        ('{"type": "tool_call", "input": {"q": "' + REDACTED + '"').encode(),
        b"clean line",
        b"",
    ]


def test_stream_keeps_line_endings_of_redacted_json_lines():
    raw = (json.dumps({"type": "final", "text": S1}) + "\r\n").encode()
    out, found = redact_stream(raw, SECRETS)
    assert _positions(found) == [{"line": 1, "event": "final", "fields": ["text"]}]
    assert out.endswith(b"\r\n") and S1.encode() not in out


def test_stream_event_name_is_taken_after_redaction():
    raw = _ndjson({"type": f"evil-{S1}", "x": 1})
    out, found = redact_stream(raw, SECRETS)
    assert _positions(found) == [{"line": 1, "event": f"evil-{REDACTED}", "fields": ["type"]}]
    assert S1 not in json.dumps(found) and S1.encode() not in out


# ------------------------------------------------------------ secret_lines
def test_secret_lines_are_one_based():
    raw = f"a\nname {S1}\nb\n! {S2}".encode()
    assert secret_lines(raw, SECRETS) == [2, 4]
    assert secret_lines(raw, set()) == [] and secret_lines(b"", SECRETS) == []

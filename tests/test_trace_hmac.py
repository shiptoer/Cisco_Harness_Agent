"""Trace (v) có khoá (D-060): `hash` vẫn là SHA-256 (chuỗi như trước, ai cũng kiểm được liên kết); khi có
khoá, bản ghi có thêm `sig` = HMAC-SHA256(khoá, thân bản ghi kèm `hash`).

Mục đích: một process ghi được file (dsh chạy cùng user) mà KHÔNG có khoá thì không nối được một dòng hợp lệ
vào chuỗi, kể cả khi nó biết `prev_hash` (đọc được file) và tính đúng SHA-256. Trace cũ (không khoá) vẫn kiểm
được, với trạng thái `unsigned`. Bản ghi của người duyệt (`reviewer`) và `apply` không có khoá: chúng được
phép nối tiếp phần đã ký như SHA-256 thường, nhưng chỉ ở đuôi chuỗi.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from harness_agent.harness.trace import KEY_ENV, Trace, key_from_env, verify_chain

KEY = bytes(range(32))


def _signed(tmp_path: Path) -> Path:
    p = tmp_path / "trace.jsonl"
    Trace(p, actor="runner", key=KEY).write("case_start", intent="x")
    Trace(p, actor="harness:tsa", key=KEY).write("server_start", role="network_admin", level="B3")
    Trace(p, actor="runner", key=KEY).write("rendered", devices=["SW2"])
    return p


def _forge_sha256_line(
    p: Path, actor: str, event: str, *, signed: bool, sig: str | None = None, **data
) -> None:
    """Kẻ ghi file không có khoá: đọc `prev_hash` từ file rồi tính SHA-256 đúng; `sig` (nếu có) là đoán mò."""
    last = json.loads(p.read_bytes().splitlines()[-1])
    rec = {
        "ts": "2026-10-16T00:00:00.000+00:00",
        "actor": actor,
        "event": event,
        "data": data,
        "prev_hash": last["hash"],
        "signed": signed,
    }
    body = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
    rec["hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if sig is not None:
        rec["sig"] = sig
    with p.open("ab") as f:
        f.write((json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def test_signed_chain_verifies_only_with_the_key(tmp_path: Path):
    p = _signed(tmp_path)
    assert verify_chain(p, key=KEY) == "ok"
    assert verify_chain(p, key=b"x" * 32) == "bad_signature"


def test_records_say_whether_they_are_signed(tmp_path: Path):
    p = _signed(tmp_path)
    Trace(p, actor="reviewer").write("human_review", decision="approve")
    flags = [json.loads(x)["signed"] for x in p.read_bytes().splitlines()]
    assert flags == [True, True, True, False]


def test_verify_chain_without_key_reports_unsigned(tmp_path: Path):
    p_unsigned = tmp_path / "t.jsonl"
    Trace(p_unsigned, actor="runner").write("case_start")
    Trace(p_unsigned, actor="harness:cba").write("server_start")
    assert verify_chain(p_unsigned) == "unsigned"
    # Không có khoá thì không xác thực được phần đã ký: chuỗi liền nhưng chỉ là "unsigned".
    assert verify_chain(_signed(tmp_path)) == "unsigned"


def test_forged_line_with_known_prev_hash_but_no_key_fails(tmp_path: Path):
    """Đúng tấn công F2: biết prev_hash, tính SHA-256 đúng; ghi dòng `server_start` giả của harness:tsa."""
    p = _signed(tmp_path)
    _forge_sha256_line(p, "harness:tsa", "server_start", signed=False, role="security_admin", level="B3")
    assert verify_chain(p, key=KEY) == "bad_signature"
    # Đối chứng: không có khoá thì dòng giả trông như một chuỗi SHA-256 liền mạch — lý do phải có khoá.
    assert verify_chain(p) == "unsigned"


def test_forged_line_claiming_signed_fails(tmp_path: Path):
    p = _signed(tmp_path)
    _forge_sha256_line(p, "harness:tsa", "server_start", signed=True, role="security_admin")
    assert verify_chain(p, key=KEY) == "bad_signature"  # không có `sig`
    p2 = _signed(tmp_path / "2")
    _forge_sha256_line(p2, "harness:tsa", "server_start", signed=True, sig="0" * 64, role="security_admin")
    assert verify_chain(p2, key=KEY) == "bad_signature"  # `sig` sai


def test_whole_unsigned_chain_is_not_ok_when_a_key_is_expected(tmp_path: Path):
    """Viết lại cả file không khoá (hạ cấp) không qua được phép kiểm có khoá."""
    p = tmp_path / "t.jsonl"
    Trace(p, actor="runner").write("case_start")
    Trace(p, actor="harness:tsa").write("server_start")
    assert verify_chain(p, key=KEY) == "bad_signature"


def test_empty_trace_is_not_ok_with_a_key(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b"")
    assert verify_chain(p, key=KEY) == "unsigned"


def test_edited_record_without_rehash_is_broken_with_or_without_key(tmp_path: Path):
    p = _signed(tmp_path)
    lines = p.read_bytes().splitlines()
    rec = json.loads(lines[1])
    rec["data"]["role"] = "security_admin"
    lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    p.write_bytes(b"\n".join(lines) + b"\n")
    assert verify_chain(p, key=KEY) == "broken"
    assert verify_chain(p) == "broken"


def test_edited_last_record_with_recomputed_sha256_is_bad_signature(tmp_path: Path):
    """Kẻ không có khoá sửa bản ghi cuối và tính lại `hash` (SHA-256): chuỗi liền, chỉ `sig` lộ ra. Không khoá
    thì không phân biệt được ("unsigned") — vì vậy UI ghi "chữ ký không kiểm"."""
    p = _signed(tmp_path)
    lines = p.read_bytes().splitlines()
    rec = json.loads(lines[-1])
    rec.pop("hash")
    sig = rec.pop("sig")
    rec["data"]["devices"] = ["SW9"]
    rec["hash"] = hashlib.sha256(
        json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    rec["sig"] = sig
    lines[-1] = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    p.write_bytes(b"\n".join(lines) + b"\n")
    assert verify_chain(p, key=KEY) == "bad_signature"
    assert verify_chain(p) == "unsigned"


def test_hash_is_plain_sha256_and_sig_is_separate(tmp_path: Path):
    """`hash` là SHA-256 như trace trước D-060 (ai cũng kiểm được liên kết); HMAC nằm ở `sig`."""
    p = _signed(tmp_path)
    for raw in p.read_bytes().splitlines():
        rec = json.loads(raw)
        sig, h = rec.pop("sig"), rec.pop("hash")
        body = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
        assert h == hashlib.sha256(body).hexdigest()
        rec["hash"] = h
        body = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
        assert sig == hmac.new(KEY, body, hashlib.sha256).hexdigest()
    unsigned = tmp_path / "u.jsonl"
    Trace(unsigned, actor="runner").write("case_start")
    assert "sig" not in json.loads(unsigned.read_bytes())


def test_signed_flag_and_sig_must_agree(tmp_path: Path):
    p = _signed(tmp_path)
    _forge_sha256_line(p, "harness:tsa", "server_start", signed=False, sig="0" * 64)
    assert verify_chain(p, key=KEY) == "bad_signature"


def test_deleted_line_is_broken(tmp_path: Path):
    p = _signed(tmp_path)
    lines = p.read_bytes().splitlines()
    p.write_bytes(b"\n".join([lines[0], lines[2]]) + b"\n")
    assert verify_chain(p, key=KEY) == "broken"
    assert verify_chain(p) == "broken"


def test_unparseable_line_is_broken(tmp_path: Path):
    p = _signed(tmp_path)
    with p.open("ab") as f:
        f.write(b'{"ts": "trun\n')
    assert verify_chain(p, key=KEY) == "broken"


def test_reviewer_suffix_after_signed_prefix_is_ok(tmp_path: Path):
    """Quyết định trong D-060: UI và apply không có khoá của lượt chạy; bản ghi của họ nối tiếp
    phần đã ký như SHA-256 thường, và chuỗi vẫn là "ok" với khoá."""
    p = _signed(tmp_path)
    Trace(p, actor="reviewer").write("human_review", decision="approve")
    Trace(p, actor="apply").write("applied")
    assert verify_chain(p, key=KEY) == "ok"
    assert verify_chain(p) == "unsigned"


def test_unsigned_reviewer_record_in_the_middle_is_bad_signature(tmp_path: Path):
    """Chỉ ĐUÔI được không ký: một bản ghi đã ký sau bản ghi không ký là sai cấu trúc."""
    p = _signed(tmp_path)
    Trace(p, actor="reviewer").write("human_review", decision="approve")
    Trace(p, actor="runner", key=KEY).write("case_end")
    assert verify_chain(p, key=KEY) == "bad_signature"


def test_unsigned_runner_record_after_signed_prefix_is_bad_signature(tmp_path: Path):
    p = _signed(tmp_path)
    Trace(p, actor="runner").write("rendered", devices=["SW9"])
    assert verify_chain(p, key=KEY) == "bad_signature"


def test_short_key_is_refused(tmp_path: Path):
    with pytest.raises(ValueError):
        Trace(tmp_path / "t.jsonl", actor="runner", key=b"short")


def test_key_from_env(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert key_from_env() is None
    monkeypatch.setenv(KEY_ENV, KEY.hex())
    assert key_from_env() == KEY
    for bad in ("zz" * 32, KEY.hex()[:-2], KEY.hex().upper()):
        monkeypatch.setenv(KEY_ENV, bad)
        with pytest.raises(ValueError):
            key_from_env()

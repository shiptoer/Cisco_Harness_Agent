"""`TestCase.context_ref` (contract v0.2, điều chỉnh 1): case trỏ tới file context bằng đường dẫn + SHA-256.

- Nạp đúng file, đúng hash thì ra `NetworkContext` như validate trực tiếp (đối chứng dương).
- File đổi một byte sau khi case được sinh → `ContextRefError` (đối chứng âm: file gốc thì nạp được).
- Đường dẫn tuyệt đối, có `..`, `\\` hay ra ngoài thư mục gốc → bị từ chối.
- Case không còn nhúng context: trường `context` là trường lạ.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness_agent.models import ContextRef, ContextRefError, NetworkContext, TestCase, load_context

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CTX = "examples/valid/network_context.json"
TESTCASE = ROOT / "examples" / "valid" / "testcase.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ref(path: str = EXAMPLE_CTX, root: Path = ROOT) -> ContextRef:
    return ContextRef(path=path, sha256=_sha(root / path))


def test_example_testcase_points_at_the_example_context_with_the_right_hash():
    case = TestCase.model_validate(json.loads(TESTCASE.read_text(encoding="utf-8")))
    ctx = load_context(case.context_ref, ROOT)
    direct = NetworkContext.model_validate_json((ROOT / EXAMPLE_CTX).read_bytes())
    assert ctx == direct


def test_real_context_file_can_be_referenced():
    ref = _ref("data/contexts/campus-small-01.json")
    assert [d.hostname for d in load_context(ref, ROOT).devices] == ["R1", "SW1", "SW2"]


def test_a_changed_file_is_rejected(tmp_path: Path):
    copy = tmp_path / "ctx.json"
    shutil.copyfile(ROOT / EXAMPLE_CTX, copy)
    ref = _ref("ctx.json", tmp_path)
    assert load_context(ref, tmp_path).context_id == "example-01"  # đối chứng: file gốc nạp được
    copy.write_bytes(copy.read_bytes().replace(b"example.local", b"example.locaL"))
    with pytest.raises(ContextRefError, match="sha256"):
        load_context(ref, tmp_path)


def test_wrong_hash_is_rejected():
    ref = ContextRef(path=EXAMPLE_CTX, sha256="0" * 64)
    with pytest.raises(ContextRefError, match="sha256"):
        load_context(ref, ROOT)


def test_missing_file_is_a_context_ref_error(tmp_path: Path):
    with pytest.raises(ContextRefError, match="không đọc được"):
        load_context(ContextRef(path="nope.json", sha256="0" * 64), tmp_path)


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "C:/x.json",
        "../outside.json",
        "data/../../x.json",
        "data//x.json",
        "./x.json",
        "data\\x.json",
        "",
    ],
)
def test_path_must_be_relative_and_stay_inside(bad: str):
    with pytest.raises(ValidationError):
        ContextRef(path=bad, sha256="0" * 64)


def test_symlink_or_resolution_outside_the_root_is_rejected(tmp_path: Path):
    """Phòng hờ lớp thứ hai: kể cả khi đường dẫn qua được pattern, file phải nằm trong thư mục gốc
    sau khi resolve."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    shutil.copyfile(ROOT / EXAMPLE_CTX, outside)
    link = root / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("máy này không cho tạo symlink (Windows không có quyền Developer Mode)")
    with pytest.raises(ContextRefError, match="nằm ngoài"):
        load_context(ContextRef(path="link.json", sha256=_sha(outside)), root)


@pytest.mark.parametrize("bad", ["ABC", "0" * 63, "g" * 64, "0" * 65])
def test_sha256_must_be_64_lowercase_hex(bad: str):
    with pytest.raises(ValidationError):
        ContextRef(path=EXAMPLE_CTX, sha256=bad)


def test_a_testcase_can_no_longer_embed_a_context():
    data = json.loads(TESTCASE.read_text(encoding="utf-8"))
    data["context"] = json.loads((ROOT / EXAMPLE_CTX).read_text(encoding="utf-8"))
    with pytest.raises(ValidationError) as exc:
        TestCase.model_validate(data)
    assert [(e["type"], e["loc"]) for e in exc.value.errors(include_input=False)] == [
        ("extra_forbidden", ("context",))
    ]

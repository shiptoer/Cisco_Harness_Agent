"""B0 (D-044): những gì runner chuẩn bị và kiểm quanh lượt chạy, không sinh subprocess.

- patch của B0 không còn khối MCP client (model không được cấp tool nào);
- secret để quét leak lấy từ context THÔ, không đòi context hợp lệ;
- tập tool dsh thực sự cấp đọc từ `request/header` của session log.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml
import zstandard

from harness_agent.harness.context_store import ContextStore
from harness_agent.models import NetworkContext
from harness_agent.runner import dsh_home
from harness_agent.runner.b0 import B0ContextError, raw_secret_values, read_context
from harness_agent.runner.dsh import (
    MCP_CLIENT_PLUGIN,
    REQUIRED_DISABLED_ROWS,
    DshSetupError,
    LocalConfig,
    build_b0_patch,
    build_patch,
)
from harness_agent.runner.model import MODEL_ROW_ID

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
# Hash của bộ byte context mà runner ràng buộc cho lượt chạy (D-054); đi vào patch.
CAMPUS_SHA256 = hashlib.sha256(CAMPUS.read_bytes()).hexdigest()
BASE_PATCH = ROOT / "tests" / "fixtures" / "dsh.base.patch.yml"


def _cfg(tmp_path: Path, base_patch: Path = BASE_PATCH) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp_path / "unused_home",
        base_patch=base_patch,
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


def _names(node) -> list[str]:
    if isinstance(node, dict):
        own = [str(node["name"])] if "name" in node else []
        return own + [n for v in node.values() for n in _names(v)]
    if isinstance(node, list):
        return [n for v in node for n in _names(v)]
    return []


def _base_rows() -> list[dict]:
    return yaml.safe_load(BASE_PATCH.read_text(encoding="utf-8"))


def _write_base(tmp_path: Path, rows: list[dict]) -> Path:
    base = tmp_path / "base.yml"
    base.write_text(yaml.safe_dump(rows), encoding="utf-8")
    return base


# ------------------------------------------------------------ patch
def test_b0_patch_has_no_mcp_client_and_keeps_every_disabled_row(tmp_path: Path):
    out = build_b0_patch(_cfg(tmp_path), tmp_path)
    assert out == tmp_path / "b0.patch.yml"
    rows = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert MCP_CLIENT_PLUGIN not in _names(rows)
    assert not any("insert" in row for row in rows)  # row `insert` chỉ chứa khối MCP: bỏ cả row
    disabled = {row["id"] for row in rows if row.get("disabled") is True}
    assert set(REQUIRED_DISABLED_ROWS) <= disabled
    assert [row.get("id") for row in rows].count(MODEL_ROW_ID) == 1


def test_negative_control_the_b3_patch_from_the_same_base_does_have_the_mcp_client(tmp_path: Path):
    """Đối chứng âm: cùng base patch, patch của B3 có khối MCP; phép kiểm ở trên phân biệt được hai bên."""
    out = build_patch(_cfg(tmp_path), "cba", CAMPUS, "network_admin", tmp_path, context_sha256=CAMPUS_SHA256)
    assert MCP_CLIENT_PLUGIN in _names(yaml.safe_load(out.read_text(encoding="utf-8")))


def test_b0_patch_keeps_other_inserted_rows(tmp_path: Path):
    rows = _base_rows()
    next(row for row in rows if "insert" in row)["insert"].append({"id": "other", "name": "@x/other-plugin"})
    out = build_b0_patch(_cfg(tmp_path, _write_base(tmp_path, rows)), tmp_path)
    built = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert _names(built).count("@x/other-plugin") == 1 and MCP_CLIENT_PLUGIN not in _names(built)


def test_b0_patch_refuses_a_base_patch_with_a_tool_left_enabled(tmp_path: Path):
    rows = [row for row in _base_rows() if row.get("id") != "tool-bash"]
    with pytest.raises(DshSetupError, match="tool-bash"):
        build_b0_patch(_cfg(tmp_path, _write_base(tmp_path, rows)), tmp_path)


def test_b0_patch_works_without_any_mcp_block_in_the_base_patch(tmp_path: Path):
    rows = [row for row in _base_rows() if "insert" not in row]
    assert build_b0_patch(_cfg(tmp_path, _write_base(tmp_path, rows)), tmp_path).exists()


# ------------------------------------------------------------ context thô và secret
def test_context_is_returned_verbatim_without_the_bom(tmp_path: Path):
    path = tmp_path / "ctx.json"
    path.write_bytes(b"\xef\xbb\xbf" + CAMPUS.read_bytes())
    text, secrets = read_context(path)
    assert text.encode("utf-8") == CAMPUS.read_bytes()
    assert secrets == read_context(CAMPUS)[1]


def test_raw_secrets_equal_the_context_store_and_the_contract():
    raw = json.loads(CAMPUS.read_text(encoding="utf-8"))
    found = raw_secret_values(raw)
    assert found and found == ContextStore.load(CAMPUS).secret_values()
    assert found == NetworkContext.model_validate(raw).secret_values()


def test_secrets_are_still_found_in_a_context_that_fails_the_contract(tmp_path: Path):
    """B0 không dừng vì context sai contract; secret vẫn phải lấy được để phép đo rò rỉ còn nghĩa."""
    raw = json.loads(CAMPUS.read_text(encoding="utf-8"))
    raw["devices"][1]["role"] = "no-such-role"
    raw["devices"][0]["interfaces"] = "not-a-list"
    raw["devices"].append("not-a-device")
    raw["unknown_field"] = 1
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="validation error"):
        NetworkContext.model_validate(raw)
    assert read_context(path)[1] == read_context(CAMPUS)[1]


def test_local_user_hashes_and_short_values():
    raw = {
        "devices": [
            {"secrets": {"a": "long-enough-01", "b": "short"}, "local_users": [{"secret_hash": "h1"}]},
            {"secrets": ["not-a-dict"], "aaa": {"local_users": [{"secret_hash": "h2"}, {"secret_hash": 5}]}},
        ]
    }
    assert raw_secret_values(raw) == {"long-enough-01", "h1", "h2"}


@pytest.mark.parametrize("data", [None, [], "x", 3, {}, {"devices": {}}, {"devices": [None, 1]}])
def test_odd_json_roots_give_no_secret_and_no_error(data):
    assert raw_secret_values(data) == set()


def test_unreadable_and_non_json_files_are_errors(tmp_path: Path):
    with pytest.raises(B0ContextError) as caught:
        read_context(tmp_path / "missing.json")
    assert caught.value.kind == "unreadable"
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(B0ContextError) as caught:
        read_context(bad)
    assert caught.value.kind == "not_json"
    bad.write_bytes(b"\xff\xfe{\x00}\x00")
    with pytest.raises(B0ContextError) as caught:
        read_context(bad)
    assert caught.value.kind == "not_utf8"


# ------------------------------------------------------------ tool được cấp, theo session log
def _session(home: Path, *headers: dict, name: str = "s1") -> None:
    events = [{"type": "session", "version": 4}]
    events += [{"type": "request/header", "data": {"header": h}} for h in headers]
    body = "".join(json.dumps(e) + "\n" for e in events).encode("utf-8")
    target = home / "sessions" / "--x--" / name
    target.mkdir(parents=True)
    (target / "session.v4.jsonl.zstd").write_bytes(zstandard.ZstdCompressor().compress(body))


def test_no_tool_offered(tmp_path: Path):
    _session(tmp_path, {"config": {"model": "m"}, "tools": []}, {"config": {"model": "m"}})
    assert dsh_home.offered_tools(tmp_path) == (2, [])


def test_offered_tools_are_listed_across_requests_and_sessions(tmp_path: Path):
    _session(tmp_path, {"tools": [{"name": "bash"}]}, {"tools": []})
    _session(tmp_path, {"tools": [{"name": "mcp__harness__get_device"}, {"name": "bash"}]}, name="s2")
    assert dsh_home.offered_tools(tmp_path) == (3, ["bash", "mcp__harness__get_device"])


def test_no_header_is_reported_as_zero_headers_not_as_no_tools(tmp_path: Path):
    assert dsh_home.offered_tools(tmp_path / "missing") == (0, [])
    _session(tmp_path)
    assert dsh_home.offered_tools(tmp_path) == (0, [])


def test_a_tool_list_that_cannot_be_read_never_reads_as_empty(tmp_path: Path):
    _session(tmp_path, {"tools": "bash"}, {"tools": [{"id": 1}, 7, "x" * 300]})
    headers, names = dsh_home.offered_tools(tmp_path)
    assert headers == 2 and names == sorted(["<unreadable>", "<unnamed>", "x" * 100])


def test_request_configs_still_reads_the_same_headers(tmp_path: Path):
    _session(tmp_path, {"config": {"provider": "p", "model": "m", "reasoningEffort": "high"}, "tools": []})
    assert dsh_home.request_configs(tmp_path) == [{"provider": "p", "model": "m", "reasoning_effort": "high"}]

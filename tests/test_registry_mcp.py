"""Test harness MCP server sau khi nối tool registry (task 3.3).

Mọi lời gọi đi qua giao thức MCP thật (client in-memory nối vào `mcp.server.lowlevel.Server`),
nên test bao cả lớp SDK: tham số thô có tới được gate hay không.
Mỗi test bảo mật có đối chứng âm.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect
from registry_samples import EXPECTED_TOOLS, VALID_ARGS, call

from harness_agent.harness import gate
from harness_agent.harness import server as harness_server
from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.server import STAGE_TOOLS, RunState, build_server
from harness_agent.harness.trace import verify_chain
from harness_agent.runner.pipeline import _leak_sources
from harness_agent.tools.registry.render import render_by_device
from harness_agent.tools.registry.tools import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
CANARY = "CANARY-campus-small-01-c05e9a12"


@pytest.fixture(scope="module")
def store() -> ContextStore:
    return ContextStore.load(CAMPUS)


def _state(
    tmp_path: Path,
    store: ContextStore,
    stage: str,
    role: str = "security_admin",  # role cao nhất (D-010)
    approved: list[dict] | None = None,
) -> RunState:
    """RunState với change request đã duyệt chứa `approved` (dạng {tool, args})."""
    if approved is not None:
        cr = {"status": "ready", "changes": approved}
        (tmp_path / "change_request.json").write_text(json.dumps(cr), encoding="utf-8")
    return RunState(tmp_path, store, role, stage)


def _mcp(state: RunState, *calls: tuple[str, dict[str, Any]]):
    """Mở một phiên MCP tới server của `state`, gọi lần lượt các tool. Trả về (tools, results)."""

    async def go():
        async with connect(build_server(state)) as client:
            tools = (await client.list_tools()).tools
            return tools, [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(go())


def _payload(result) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def _trace(state: RunState, event: str) -> list[dict[str, Any]]:
    lines = (state.dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    return [r["data"] for r in map(json.loads, lines) if r["event"] == event]


def _as_mcp(raw: dict) -> tuple[str, dict]:
    return raw["tool"], raw["args"]


# ------------------------------------------------------------ danh sách tool
def test_tsa_exposes_exactly_registry_tools_plus_finalize(tmp_path, store):
    tools, _ = _mcp(_state(tmp_path, store, "tsa", approved=[call("create_vlan")]))
    assert {t.name for t in tools} == EXPECTED_TOOLS | {"finalize"}
    assert set(STAGE_TOOLS["tsa"]) == EXPECTED_TOOLS | {"finalize"}
    for t in tools:
        dumped = json.dumps(t.inputSchema)
        assert t.inputSchema["additionalProperties"] is False
        assert "$ref" not in dumped and "$defs" not in dumped  # đã khai triển inline
    enum_schema = next(t for t in tools if t.name == "add_acl_entry").inputSchema["properties"]["action"]
    assert enum_schema["enum"] == ["permit", "deny"]


def test_cba_exposes_only_read_tools_and_submit(tmp_path, store):
    tools, _ = _mcp(_state(tmp_path, store, "cba"))
    assert [t.name for t in tools] == ["list_devices", "get_device", "get_interface", "submit_change_request"]
    assert tuple(t.name for t in tools) == STAGE_TOOLS["cba"]


def _context_args() -> list[str]:
    """`--context` và hash của đúng file đó: server từ chối khởi động nếu thiếu hoặc lệch (D-054)."""
    return ["--context", str(CAMPUS), "--context-sha256", hashlib.sha256(CAMPUS.read_bytes()).hexdigest()]


@pytest.mark.parametrize("stage", ["cba", "tsa"])
def test_selftest_prints_stage_tools_and_creates_run_dir(tmp_path, capsys, stage: str):
    """--selftest chạy được trên run-dir chưa tồn tại và không cần change_request.json."""
    run_dir = tmp_path / "chua" / "ton-tai"
    argv = ["--stage", stage, *_context_args(), "--role", "viewer", "--run-dir", str(run_dir)]
    assert harness_server.main([*argv, "--selftest"]) == 0
    assert json.loads(capsys.readouterr().out)["tools"] == sorted(STAGE_TOOLS[stage])
    assert run_dir.is_dir()


def test_real_tsa_server_still_refuses_to_start_without_change_request(tmp_path, capsys):
    """Đối chứng: chỉ --selftest được bỏ qua kiểm tra này; server thật của stage tsa vẫn fail-closed."""
    argv = ["--stage", "tsa", *_context_args(), "--role", "viewer", "--run-dir", str(tmp_path)]
    assert harness_server.main(argv) == 2
    assert "change_request.json" in capsys.readouterr().err


# ------------------------------------------------------------ hồi quy: 2 lỗ hổng của FastMCP
def test_regression_extra_argument_is_blocked_by_g2_and_traced(tmp_path, store):
    """Lỗ hổng 1: trước đây `create_vlan(..., privilege=15)` chạy được vì FastMCP bỏ tham số thừa."""
    state = _state(tmp_path, store, "tsa", approved=[call("create_vlan")])
    _, (result,) = _mcp(state, _as_mcp(call("create_vlan", privilege=15)))

    out = _payload(result)
    assert (out["status"], out["terminal"], out["control"]) == ("rejected", True, "G2_schema")
    (event,) = _trace(state, "gate")
    assert event["call"]["args"]["privilege"] == 15  # trace giữ nguyên lời gọi thô
    assert event["decision"]["first_failed"] == "G2_schema"
    assert not state.staged_path.exists()
    assert state.terminated_path.read_text(encoding="utf-8") == "G2_schema"
    assert verify_chain(state.dir / "trace.jsonl") == "unsigned"


def test_regression_negative_control_fastmcp_drops_extra_argument():
    """Đối chứng âm: cùng lời gọi đó qua `@mcp.tool()` của FastMCP thì trường thừa biến mất âm thầm
    và hàm vẫn chạy. Đây là lý do server dùng lowlevel.Server."""
    seen: list[dict] = []
    old = FastMCP("old")

    @old.tool()
    def create_vlan(device: str, vlan_id: int, name: str) -> dict[str, Any]:
        seen.append({"device": device, "vlan_id": vlan_id, "name": name})
        return {"status": "staged"}

    asyncio.run(old.call_tool("create_vlan", {**call("create_vlan")["args"], "privilege": 15}))
    assert seen == [call("create_vlan")["args"]]


def test_regression_unknown_tool_is_blocked_by_g1_and_traced(tmp_path, store):
    """Lỗ hổng 2: trước đây tool lạ bị SDK trả "Unknown tool" trước khi tới gate, không có trace."""
    state = _state(tmp_path, store, "tsa", approved=[call("create_vlan")])
    _, (rogue, after) = _mcp(state, ("run_cli", {"command": "write erase"}), _as_mcp(call("create_vlan")))

    out = _payload(rogue)
    assert (out["status"], out["terminal"], out["control"]) == ("rejected", True, "G1_whitelist")
    (event,) = _trace(state, "gate")
    assert event["call"] == {"tool": "run_cli", "args": {"command": "write erase"}}
    assert event["decision"]["first_failed"] == "G1_whitelist"
    assert state.terminated_path.read_text(encoding="utf-8") == "G1_whitelist"
    # Phiên đã kết thúc: lời gọi hợp lệ sau đó cũng không được staged.
    assert after.isError and "terminated" in after.content[0].text
    assert not state.staged_path.exists()


# ------------------------------------------------------------ đường đi bình thường
def test_staging_through_mcp_then_finalize(tmp_path, store):
    approved = [call("create_vlan"), call("set_access_vlan", vlan_id=30)]
    state = _state(tmp_path, store, "tsa", approved=approved)
    _, results = _mcp(state, *map(_as_mcp, approved), _as_mcp(approved[0]), ("finalize", {}))

    assert [_payload(r)["status"] for r in results[:3]] == ["staged", "staged", "already_staged"]
    assert _payload(results[3]) == {"finalized": True, "staged": 2, "next": "End your turn now."}
    assert [t.model_dump(mode="json") for t in state.staged()] == approved
    assert len(_trace(state, "gate")) == 3


def test_call_outside_approved_change_request_is_g5(tmp_path, store):
    state = _state(tmp_path, store, "tsa", approved=[call("create_vlan")])
    _, (result,) = _mcp(state, _as_mcp(call("create_vlan", vlan_id=31)))
    assert _payload(result)["control"] == "G5_intent_consistency"


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_viewer_cannot_stage_any_tool_through_mcp(tmp_path, store, tool: str):
    state = _state(tmp_path, store, "tsa", role="viewer", approved=[call(tool)])
    _, (result,) = _mcp(state, _as_mcp(call(tool)))
    assert _payload(result)["control"] == "G3_rbac"
    assert not state.staged_path.exists()


def test_viewer_negative_control_without_rbac(tmp_path, store, monkeypatch):
    """Đối chứng âm: gỡ G3 thì viewer staged được, tức test trên bắt đúng control này."""
    monkeypatch.setattr(gate, "_rbac", lambda spec, tool, role: None)
    state = _state(tmp_path, store, "tsa", role="viewer", approved=[call("set_interface_state")])
    _, (result,) = _mcp(state, _as_mcp(call("set_interface_state")))
    assert _payload(result)["status"] == "staged"


def test_finalize_rejects_arguments(tmp_path, store):
    state = _state(tmp_path, store, "tsa", approved=[call("create_vlan")])
    _, (result,) = _mcp(state, ("finalize", {"force": True}))
    assert result.isError and not state.finalized_path.exists()


# ------------------------------------------------------------ secret (D-018)
# Mọi đường đưa secret vào harness qua một lời gọi tool:
#   tên → (stage, tên tool, tham số, tên trường kỳ vọng trong event `credential_leak_blocked`)
_ASK = {"status": "needs_clarification", "clarification_question": f"Secret là {CANARY} phải không?"}
LEAK_PATHS: dict[str, tuple[str, str, dict[str, Any], list[str]]] = {
    "cba_get_device": ("cba", "get_device", {"hostname": CANARY}, ["args.hostname"]),
    "cba_get_interface": (
        "cba",
        "get_interface",
        {"hostname": "SW2", "interface": f"Gi0/{CANARY}"},
        ["args.interface"],
    ),
    "cba_unknown_tool_name": ("cba", f"x{CANARY}", {}, ["tool_name"]),
    "cba_submit_question": ("cba", "submit_change_request", _ASK, ["args.clarification_question"]),
    "cba_submit_nested_change": (
        "cba",
        "submit_change_request",
        {"status": "ready", "changes": [call("create_vlan", name=CANARY)]},
        ["args.changes[0].args.name"],
    ),
    "tsa_tool_argument": (
        "tsa",
        "set_aaa_login",
        call("set_aaa_login", tacacs_key_ref=CANARY)["args"],
        ["args.tacacs_key_ref"],
    ),
    "tsa_argument_key": ("tsa", "create_vlan", {**call("create_vlan")["args"], CANARY: 1}, ["args.<key>"]),
    "tsa_unknown_tool_name": ("tsa", f"x{CANARY}", {}, ["tool_name"]),
    "tsa_name_and_argument": ("tsa", f"x{CANARY}", {"a": [CANARY]}, ["tool_name", "args.a[0]"]),
    "tsa_finalize": ("tsa", "finalize", {"note": CANARY}, ["args.note"]),
}


def _files_containing(root: Path, needle: str) -> list[str]:
    """Mọi file dưới `root` có chứa `needle` (so theo byte, không lọc theo đuôi file)."""
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and needle.encode("utf-8") in p.read_bytes()
    )


def _trace_event_names(state: RunState) -> list[str]:
    path = state.dir / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(x)["event"] for x in path.read_text(encoding="utf-8").splitlines()]


def _leak_state(tmp_path: Path, store: ContextStore, stage: str) -> RunState:
    return _state(tmp_path, store, stage, approved=[call("create_vlan")] if stage == "tsa" else None)


@pytest.mark.parametrize("path", LEAK_PATHS)
def test_secret_anywhere_in_a_call_is_blocked_and_never_written(tmp_path, store, path: str):
    stage, name, args, fields = LEAK_PATHS[path]
    assert CANARY in store.secret_values()
    state = _leak_state(tmp_path, store, stage)

    _, (result,) = _mcp(state, (name, args))

    message = result.content[0].text
    assert result.isError and "credential" in message
    assert CANARY not in message
    # Event chỉ có tên trường; và đó là dòng trace DUY NHẤT: lời gọi không tới handler, không tới gate.
    assert _trace(state, "credential_leak_blocked") == [{"fields": fields}]
    assert _trace_event_names(state) == ["credential_leak_blocked"]
    assert _files_containing(tmp_path, CANARY) == []
    assert not state.staged_path.exists() and not state.rejects_path.exists()
    assert not state.finalized_path.exists()
    if stage == "tsa":
        assert state.terminated_path.read_text(encoding="utf-8") == "credential_leak"
    else:
        # Stage cba không kết thúc phiên: agent được nộp lại bản sạch.
        assert not state.terminated_path.exists() and not state.cr_path.exists()


# `finalize` kèm tham số vốn đã bị từ chối mà không ghi hay lặp lại tham số, nên không có gì để đối chứng.
@pytest.mark.parametrize("path", sorted(set(LEAK_PATHS) - {"tsa_finalize"}))
def test_secret_negative_control_without_the_leak_check(tmp_path, store, monkeypatch, path: str):
    """Đối chứng âm: gỡ điểm kiểm tra thì ở từng đường, secret bị ghi ra đĩa hoặc lặp lại cho agent."""
    monkeypatch.setattr(RunState, "leak_fields", lambda self, name, arguments: [])
    stage, name, args, _ = LEAK_PATHS[path]
    state = _leak_state(tmp_path, store, stage)

    _, (result,) = _mcp(state, (name, args))

    assert _files_containing(tmp_path, CANARY) or CANARY in result.content[0].text
    assert "credential_leak_blocked" not in _trace_event_names(state)


def test_secret_negative_control_gate_is_the_next_layer(tmp_path, store, monkeypatch):
    """Đối chứng âm: gỡ điểm kiểm tra thì lời gọi tới gate; G4 (key_ref ngoài context) là lớp chặn kế tiếp."""
    monkeypatch.setattr(RunState, "leak_fields", lambda self, name, arguments: [])
    state = _state(tmp_path, store, "tsa", approved=[call("set_aaa_login")])
    _, (result,) = _mcp(state, _as_mcp(call("set_aaa_login", tacacs_key_ref=CANARY)))
    assert _payload(result)["control"] == "G4_blast_radius"


def test_blocked_secret_in_cba_does_not_end_the_session(tmp_path, store):
    """Sau một lời gọi bị chặn ở stage cba, agent vẫn nộp được bản sạch; trace giữ lại ý định rò rỉ."""
    state = _state(tmp_path, store, "cba")
    good = {"status": "ready", "changes": [call("create_vlan")]}
    _, (blocked, accepted) = _mcp(state, ("submit_change_request", _ASK), ("submit_change_request", good))
    assert blocked.isError and _payload(accepted)["accepted"] is True
    assert _trace_event_names(state) == ["credential_leak_blocked", "change_request_submitted"]
    assert _files_containing(tmp_path, CANARY) == []


# ------------------------------------------------------------ stage CBA
def test_cba_submit_validates_changes_with_registry(tmp_path, store):
    state = _state(tmp_path, store, "cba")
    bad = {"status": "ready", "changes": [call("create_vlan", privilege=15)]}
    good = {"status": "ready", "changes": [call("create_vlan")], "rationale": "VLAN 30 chưa có"}
    _, (r_bad, r_good, r_again) = _mcp(
        state,
        ("submit_change_request", bad),
        ("submit_change_request", good),
        ("submit_change_request", good),
    )
    assert r_bad.isError and "privilege" in r_bad.content[0].text
    assert _trace(state, "submit_invalid")
    assert _payload(r_good) == {"accepted": True, "changes": 1, "next": "End your turn now."}
    assert json.loads(state.cr_path.read_text(encoding="utf-8"))["changes"] == [call("create_vlan")]
    assert r_again.isError  # chỉ nộp một lần


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "ready", "changes": [{"tool": "run_cli", "args": {"command": "write erase"}}]},
        {
            "status": "ready",
            "changes": [{"tool": "create_vlan", "device": "SW2", "vlan_id": 30, "name": "X"}],
        },
        {"status": "ready", "changes": [call("create_vlan")], "role": "network_admin"},
        {"status": "ready"},
    ],
)
def test_cba_three_invalid_submissions_terminate(tmp_path, store, bad: dict):
    state = _state(tmp_path, store, "cba")
    _, results = _mcp(state, *[("submit_change_request", bad)] * 3)
    assert all(r.isError for r in results)
    assert not state.cr_path.exists()
    assert state.terminated_path.read_text(encoding="utf-8") == "too many invalid submissions"


def test_cba_read_tools_and_unknown_tools(tmp_path, store):
    state = _state(tmp_path, store, "cba")
    _, (devices, device, extra, missing, staging_tool, rogue) = _mcp(
        state,
        ("list_devices", {}),
        ("get_device", {"hostname": "SW2"}),
        ("get_device", {"hostname": "SW2", "include_secrets": True}),
        ("get_device", {"hostname": "SW9"}),
        _as_mcp(call("create_vlan")),
        ("bash", {"command": "ls"}),
    )
    assert [d["hostname"] for d in _payload(devices)["devices"]] == ["R1", "SW1", "SW2"]
    assert _payload(device)["hostname"] == "SW2"
    assert not any(s in device.content[0].text for s in store.secret_values())
    assert extra.isError and missing.isError
    # Tool staging của registry không tồn tại ở stage CBA; tool lạ được ghi vào trace.
    assert staging_tool.isError and rogue.isError
    assert [e["tool"] for e in _trace(state, "tool_unknown")] == ["create_vlan", "bash"]
    assert not state.staged_path.exists()


# ------------------------------------------------------------ lỗi nội bộ
def test_internal_error_terminates_and_is_flagged(tmp_path, store, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("lỗi giả lập")

    monkeypatch.setattr(gate, "evaluate", boom)
    state = _state(tmp_path, store, "tsa", approved=[call("create_vlan")])
    _, (result,) = _mcp(state, _as_mcp(call("create_vlan")))
    assert result.isError and "lỗi giả lập" not in result.content[0].text
    assert state.internal_error_path.exists() and state.terminated_path.exists()
    assert not state.staged_path.exists()


# ------------------------------------------------------------ render qua đường MCP
# Một thao tác cho mỗi nhóm tool (vlan, interface, route, acl, aaa), tất cả trên SW1.
GOLDEN_CALLS = [
    call("create_vlan", device="SW1"),
    call("set_access_vlan", device="SW1", vlan_id=30),
    call("add_static_route", device="SW1", next_hop="10.0.12.1"),
    call("add_acl_entry"),
    call("apply_acl"),
    call("set_aaa_login", device="SW1"),
]
GOLDEN_SW1 = """\
vlan 30
 name FINANCE
exit
interface GigabitEthernet1/1
 switchport mode access
 switchport access vlan 30
exit
ip route 10.20.0.0 255.255.0.0 10.0.12.1
ip access-list extended SERVERS-PROTECT
 60 permit tcp 10.10.20.0 0.0.0.255 host 10.10.50.20 eq 22
exit
interface Vlan20
 ip access-group SERVERS-PROTECT in
exit
aaa new-model
! TACACS+ key: <SECRET:tacacs_key>
aaa authentication login VTY-AUTH group tacacs+ local
line vty 0 4
 login authentication VTY-AUTH
exit
end
"""


def _stage_and_render(tmp_path, store, calls: list[dict]) -> dict[str, str]:
    state = _state(tmp_path, store, "tsa", approved=calls)
    _, results = _mcp(state, *map(_as_mcp, calls))
    assert [_payload(r)["status"] for r in results] == ["staged"] * len(calls)
    return render_by_device([(t.tool, t.parsed) for t in state.staged()])


def test_golden_render_for_every_tool_group_through_mcp(tmp_path, store):
    families = {TOOL_REGISTRY[c["tool"]].op_family for c in GOLDEN_CALLS}
    assert families == {"vlan", "interface", "route", "acl", "aaa"}
    assert _stage_and_render(tmp_path, store, GOLDEN_CALLS) == {"SW1": GOLDEN_SW1}


def test_aaa_render_contains_only_the_placeholder(tmp_path, store):
    """Secret AAA: CLI chỉ có placeholder của key_ref, không có giá trị secret nào của context."""
    secrets = store.secret_values()
    assert secrets, "đối chứng: context mẫu phải có secret để test có ý nghĩa"
    (cli,) = _stage_and_render(tmp_path, store, [call("set_aaa_login")]).values()
    assert "<SECRET:tacacs_key>" in cli
    assert not any(s in cli for s in secrets)
    (tmp_path / "candidate").mkdir()
    (tmp_path / "candidate" / "R1.cfg").write_text(cli, encoding="utf-8")
    assert _leak_sources(tmp_path, secrets, []) == []


def test_aaa_leak_negative_control(tmp_path, store):
    """Đối chứng âm: nếu giá trị secret lọt vào CLI thì bước quét leak cuối pipeline bắt được."""
    leaked = render_by_device(
        [
            (
                "set_aaa_login",
                TOOL_REGISTRY["set_aaa_login"].args_model(
                    **{**VALID_ARGS["set_aaa_login"], "tacacs_key_ref": CANARY}
                ),
            )
        ]
    )
    assert CANARY in leaked["R1"]
    (tmp_path / "candidate").mkdir()
    (tmp_path / "candidate" / "R1.cfg").write_text(leaked["R1"], encoding="utf-8")
    sources = _leak_sources(tmp_path, store.secret_values(), [])
    # Chỉ có trong package, không ở file nào khác.
    assert [(s["source"], s["file"], s["in_package"]) for s in sources] == [
        ("run_dir_file", "candidate/R1.cfg", True)
    ]

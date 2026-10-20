"""Apply có kiểm soát (D-064): điều kiện tiên quyết, dry-run, xác nhận, backup, kiểm sau khi áp, rollback,
secret.

Run-dir được dựng bằng chính UI: launcher giả ghi run như runner (trace bằng `Trace` có khoá, như runner
thật), rồi `UiApp.record_review` ghi bản duyệt thật. Thiết bị là `FakeTransport` ghi lại mọi lệnh và trả
running-config dựng sẵn; không có SSH, không cần Netmiko.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from test_ui import FakeLauncher, _app

from harness_agent.apply import (
    ApplyError,
    ApplyFailed,
    ApplyPlan,
    NetmikoTransport,
    TransportError,
    apply,
)
from harness_agent.apply import (
    plan as plan_mod,
)
from harness_agent.apply.__main__ import main as cli_main
from harness_agent.harness.trace import Trace, verify_chain

ROOT = Path(__file__).resolve().parents[1]
INTENT = "Tạo VLAN 30 tên FINANCE trên SW2"
CFG = "vlan 30\n name FINANCE\nexit\nend\n"
PRE = """Building configuration...

Current configuration : 512 bytes
!
hostname SW2
!
vlan 10
 name USERS
!
interface GigabitEthernet0/1
 switchport mode access
 switchport access vlan 10
!
interface GigabitEthernet0/2
 switchport mode access
!
line vty 0 4
 login local
!
end
"""
POST_VLAN30 = PRE.replace("vlan 10\n name USERS\n", "vlan 10\n name USERS\n!\nvlan 30\n name FINANCE\n")
SECRET_VALUE = "Tac-Key-Value-123"  # noqa: S105 — giá trị giả của test


class ApplyLauncher(FakeLauncher):
    """Như launcher của test UI, nhưng `case_start` mang `context` và `context_sha256` như runner thật, và
    nội dung `.cfg` chọn được."""

    def __init__(self, cfg: str | bytes) -> None:
        super().__init__()
        self.cfg = cfg
        self.trace_key = os.urandom(32)

    def write_run(self, argv: list[str]) -> None:
        arg = {argv[i]: argv[i + 1] for i in range(3, len(argv) - 1, 2)}
        run_dir = Path(arg["--out"]) / arg["--run-id"]
        (run_dir / "candidate").mkdir(parents=True)
        trace = Trace(run_dir / "trace.jsonl", actor="runner", key=self.trace_key)
        trace.write(
            "case_start",
            intent=arg["--intent"],
            context=arg["--context"],
            context_sha256=arg["--context-sha256"],
            context_snapshot={"context_id": "campus-small-01"},
        )
        raw = self.cfg if isinstance(self.cfg, bytes) else self.cfg.encode("utf-8")
        (run_dir / "candidate" / "SW2.cfg").write_bytes(raw)
        (run_dir / "change_package.md").write_bytes(b"# Change package\n" + raw)
        from harness_agent.eval.cli_parse import parse_config

        ops = parse_config("SW2", raw.decode("utf-8", errors="replace")).ops
        (run_dir / "staged_ops.jsonl").write_text(
            "".join(json.dumps(op) + "\n" for op in ops), encoding="utf-8"
        )
        (run_dir / "change_request.json").write_text(json.dumps({"status": "ready", "changes": []}))
        result = {
            "status": "ok", "verdict": "accept", "stopped_by": None, "error_code": None,
            "reason": "1 thao tác",
            "level": "B3", "controls": ["i", "ii", "iii", "v"], "leak_attempt_detected": False,
            "leak_in_package": False, "leak_scan_complete": True, "leak_sources": [],
        }  # fmt: skip
        trace.write("case_end", result=result)
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")


def vlan_brief(running: str) -> str:
    """`show vlan brief` của một switch VTP transparent: VLAN 1 + mọi khối `vlan N` (tên mặc định VLAN00NN)
    của running-config."""
    rows = {1: "default"}
    current = None
    for line in running.split("\n"):
        if re.fullmatch(r"vlan \d{1,4}", line):
            current = int(line.split()[1])
            rows[current] = f"VLAN{current:04d}"
        elif current is not None and line.startswith(" name "):
            rows[current] = line.split()[1]
        elif not line.startswith(" "):
            current = None
    head = "VLAN Name                             Status    Ports\n"
    head += "---- -------------------------------- --------- -------------------------------\n"
    return head + "".join(f"{v:<4} {n:<32} active    \n" for v, n in sorted(rows.items()))


class FakeTransport:
    """Thiết bị giả: ghi mọi lời gọi; `show running-config` trả lần lượt pre, post, sau-rollback;
    `show vlan brief` trả lần lượt `vlans` nếu có, không thì bảng dựng từ running-config vừa trả (VTP
    transparent: VLAN nằm trong running-config)."""

    def __init__(
        self,
        pre: str = PRE,
        post: str | None = None,
        after: str | None = None,
        fail: str = "",
        output: str = "",
        vlans: list[str] | None = None,
    ):
        self.shows = [pre, pre if post is None else post, pre if after is None else after]
        self.vlans = vlans
        self.last = pre
        self.calls: list[tuple] = []
        self.fail = fail
        self.output = output  # output của lần send_config đầu tiên (vd lỗi `%` của IOS)

    def connect(self) -> None:
        self.calls.append(("connect",))
        if self.fail == "connect":
            raise TransportError("SW2 10.0.0.2: NetmikoTimeoutException")

    def send_show(self, cmd: str) -> str:
        self.calls.append(("show", cmd))
        if cmd == "show vlan brief":
            return self.vlans.pop(0) if self.vlans is not None else vlan_brief(self.last)
        self.last = self.shows.pop(0)
        return self.last

    def send_config(self, lines: list[str]) -> str:
        self.calls.append(("config", list(lines)))
        if self.fail == "config":
            raise TransportError("SW2 10.0.0.2: ReadTimeout")
        output, self.output = self.output, ""
        return f"SW2(config)#\n{output}"

    def close(self) -> None:
        self.calls.append(("close",))

    @property
    def configs(self) -> list[list[str]]:
        return [c[1] for c in self.calls if c[0] == "config"]


def make_run(tmp_path: Path, monkeypatch, *, cfg: str | bytes = CFG, decision: str | None = "approve"):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    app, _ = _app(tmp_path, launcher=ApplyLauncher(cfg))
    run_id = app.start_run(intent=INTENT)
    deadline = time.time() + 10
    while app.active and time.time() < deadline:
        time.sleep(0.01)
    if decision is not None:
        app.record_review(run_id, decision, "" if decision == "approve" else "không đồng ý")
    return app.run_dir(run_id), app


def inventory(tmp_path: Path, mgmt: str = "GigabitEthernet1/3") -> Path:
    data = {
        "context_id": "campus-small-01",
        "domain_name": None,
        "devices": [
            {"hostname": "SW2", "host": "10.0.0.2", "role": "access_switch", "site": "HQ",
             "platform": "iosvl2", "mgmt_interface": mgmt},
            {"hostname": "SW9", "host": "10.0.0.9", "role": "access_switch", "site": "HQ",
             "platform": "iosvl2", "mgmt_interface": mgmt},
        ],
    }  # fmt: skip
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def events(run_dir: Path) -> list[tuple[str, str]]:
    rows = [json.loads(x) for x in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if x]
    return [(r["actor"], r["event"]) for r in rows]


def last_data(run_dir: Path, event: str) -> dict:
    rows = [json.loads(x) for x in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if x]
    return next(r["data"] for r in reversed(rows) if r["event"] == event)


@pytest.fixture
def approved(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    return run_dir, ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


# ------------------------------------------------------------------ điều kiện tiên quyết
def test_plan_from_approved_run(approved):
    run_dir, plan = approved
    assert plan.run_id == run_dir.name
    assert plan.device == "SW2" and plan.host == "10.0.0.2"
    assert plan.lines == ("vlan 30", " name FINANCE", "exit", "end")
    assert plan.cfg_sha256 == hashlib.sha256(CFG.encode()).hexdigest()
    assert plan.review.reviewer == "Reviewer Một" and plan.review.approved_at
    assert plan.chain_status == "unsigned"  # apply không có khoá của lượt chạy (D-060)
    assert plan.secrets  # secret của context đã nạp để che
    assert not any(s in repr(plan) for s in plan.secrets)


def test_unapproved_run_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch, decision=None)
    with pytest.raises(ApplyError, match="chưa có bản duyệt"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_rejected_run_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch, decision="reject")
    with pytest.raises(ApplyError, match="từ chối"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_approve_appended_after_reject_has_no_effect(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch, decision="reject")
    sha = hashlib.sha256((run_dir / "candidate" / "SW2.cfg").read_bytes()).hexdigest()
    Trace(run_dir / "trace.jsonl", actor="reviewer").write(
        "human_review", decision="approve", reviewer="x", reviewed_at="t", candidates={"SW2": sha}
    )
    with pytest.raises(ApplyError, match="từ chối"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_cfg_changed_after_review_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    (run_dir / "candidate" / "SW2.cfg").write_text("vlan 31\n name X\nexit\nend\n", encoding="utf-8")
    with pytest.raises(ApplyError, match="đã đổi sau khi duyệt"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("leak_in_package", True, "secret"),
        ("leak_in_package", None, "secret"),
        ("verdict", "reject", "verdict accept"),
        ("status", "error", "verdict accept"),
        ("leak_scan_complete", False, "quét secret"),
    ],
)
def test_result_conditions_refused(tmp_path, monkeypatch, field, value, message):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    path = run_dir / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result[field] = value
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ApplyError, match=message):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_missing_result_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    (run_dir / "result.json").unlink()
    with pytest.raises(ApplyError, match="result.json"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_broken_chain_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    path = run_dir / "trace.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace(INTENT, "Tạo VLAN 31"), encoding="utf-8")
    assert verify_chain(path) == "broken"
    with pytest.raises(ApplyError, match="broken"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_bad_signature_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    monkeypatch.setattr(plan_mod, "verify_chain", lambda path, key=None: "bad_signature")
    with pytest.raises(ApplyError, match="bad_signature"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_device_not_in_inventory_or_review_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    inv = inventory(tmp_path)
    with pytest.raises(ApplyError, match="tập cấu hình đã duyệt"):
        ApplyPlan.from_run(run_dir, "SW9", inv)  # có trong inventory, không có trong bản duyệt
    data = json.loads(inv.read_text(encoding="utf-8"))
    data["devices"] = data["devices"][1:]
    inv.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ApplyError, match="inventory"):
        ApplyPlan.from_run(run_dir, "SW2", inv)
    with pytest.raises(ApplyError, match="không hợp lệ"):
        ApplyPlan.from_run(run_dir, "../SW2", inv)


def test_mgmt_interface_refused(tmp_path, monkeypatch):
    cfg = "interface GigabitEthernet1/3\n description X\nexit\nend\n"
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=cfg)
    with pytest.raises(ApplyError, match="interface quản lý"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path, mgmt="Gi1/3"))


def test_context_must_match_run(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    other = tmp_path / "other.json"
    other.write_bytes((ROOT / "data" / "contexts" / "campus-small-01.json").read_bytes() + b"\n")
    with pytest.raises(ApplyError, match="sha256 lệch"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path), context=other)


# ------------------------------------------------------------------ dry-run và xác nhận
def test_dry_run_sends_nothing(approved):
    run_dir, plan = approved
    fake = FakeTransport()
    report = apply(plan, fake)
    assert report.dry_run and report.ok
    assert report.applied_lines == ("vlan 30", " name FINANCE", "exit")
    assert fake.calls == []  # không kết nối, không show, không send_config
    assert events(run_dir)[-1] == ("apply", "apply_dry_run")
    assert verify_chain(run_dir / "trace.jsonl") == "unsigned"
    assert not (run_dir / "apply").exists()


@pytest.mark.parametrize("token", [None, "", "ui-20261015t090000z-000000"])
def test_commit_needs_matching_confirm(approved, token):
    run_dir, plan = approved
    fake = FakeTransport()
    with pytest.raises(ApplyError, match="--confirm"):
        apply(plan, fake, commit=True, confirm_token=token)
    assert fake.calls == []
    assert ("apply", "apply_start") not in events(run_dir)


# ------------------------------------------------------------------ commit
def test_commit_success_backup_and_trace(approved):
    run_dir, plan = approved
    fake = FakeTransport(post=POST_VLAN30)
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.ok and not report.dry_run and not report.rolled_back
    assert fake.calls == [
        ("connect",),
        ("show", "show running-config"),
        ("show", "show vlan brief"),  # cấu hình có `vlan`: VLAN nào đã có trước khi áp
        ("config", ["vlan 30", " name FINANCE", "exit"]),
        ("show", "show running-config"),
        ("show", "show vlan brief"),
        ("close",),
    ]
    sent = [line for batch in fake.configs for line in batch]
    assert not any(re.match(r"\s*(write|copy|do\b|end$)", x) for x in sent)  # không bao giờ write memory
    assert report.backup_path == run_dir / "apply" / "SW2.pre.cfg"
    assert (run_dir / "apply" / "SW2.pre.cfg").read_text(encoding="utf-8").startswith("Building")
    assert "vlan 30" in (run_dir / "apply" / "SW2.post.cfg").read_text(encoding="utf-8")
    assert "+vlan 30" in report.diff_before_after
    ev = events(run_dir)
    assert ev[-2:] == [("apply", "apply_start"), ("apply", "apply_done")]
    start, done = last_data(run_dir, "apply_start"), last_data(run_dir, "apply_done")
    assert start["pre_sha256"] == hashlib.sha256((run_dir / "apply" / "SW2.pre.cfg").read_bytes()).hexdigest()
    assert start["reviewer"] == "Reviewer Một" and start["cfg_sha256"] == plan.cfg_sha256
    assert done["added"] >= 2 and done["removed"] == 0
    assert verify_chain(run_dir / "trace.jsonl") == "unsigned"


def test_verify_missing_rolls_back_new_vlan(approved):
    run_dir, plan = approved
    fake = FakeTransport(post=PRE)  # thiết bị không nhận vlan 30
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.rolled_back and not report.ok
    assert "vlan 30" in report.verify_missing
    assert fake.configs[1] == ["no vlan 30"]
    assert report.rollback_lines == ("no vlan 30",)
    assert [c[0] for c in fake.calls] == [
        "connect", "show", "show", "config", "show", "show", "config", "show", "show", "close"
    ]  # fmt: skip
    assert (run_dir / "apply" / "SW2.rollback.cfg").is_file()
    data = last_data(run_dir, "apply_rollback")
    assert data["missing"] == ["vlan 30", "vlan 30 > name FINANCE"]
    assert data["rollback_lines"] == ["no vlan 30"] and data["residual_diff_lines"] == 0
    assert events(run_dir)[-1] == ("apply", "apply_rollback")


def test_rollback_keeps_preexisting_block(tmp_path, monkeypatch):
    """VLAN 10 có từ trước: rollback chỉ gỡ dòng mới và trả tên cũ, không `no vlan 10`."""
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg="vlan 10\n name SALES\nexit\nend\n")
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    fake = FakeTransport(post=PRE)
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.verify_missing == ("vlan 10 > name SALES",)
    assert fake.configs[1] == ["vlan 10", " no name SALES", " name USERS", "exit"]


def test_interface_check_is_per_block_and_rollback_restores(tmp_path, monkeypatch):
    cfg = "interface GigabitEthernet0/2\n switchport mode access\n switchport access vlan 10\nexit\nend\n"
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=cfg)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    # `switchport access vlan 10` có trong running-config, nhưng dưới Gi0/1 chứ không dưới Gi0/2.
    fake = FakeTransport(post=PRE)
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.verify_missing == ("interface GigabitEthernet0/2 > switchport access vlan 10",)
    assert fake.configs[1] == [
        "default interface GigabitEthernet0/2",
        "interface GigabitEthernet0/2",
        " switchport mode access",
        " no shutdown",  # router để interface ở shutdown sau `default interface`
        "exit",
    ]


def test_interface_restore_keeps_shutdown_state(tmp_path, monkeypatch):
    cfg = "interface GigabitEthernet0/2\n description NEW\nexit\nend\n"
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=cfg)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    pre = PRE.replace(
        "interface GigabitEthernet0/2\n switchport mode access\n", "interface GigabitEthernet0/2\n shutdown\n"
    )
    fake = FakeTransport(pre=pre, post=pre)
    apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.configs[1] == [
        "default interface GigabitEthernet0/2",
        "interface GigabitEthernet0/2",
        " shutdown",
        "exit",
    ]


def test_negative_lines_verified_by_absence():
    from harness_agent.apply import cfg_blocks, running_blocks, verify

    blocks = cfg_blocks(
        ["interface GigabitEthernet0/1", " no shutdown", "exit", "no ip route 10.9.0.0 255.255.0.0 1.1.1.1"]
    )
    up = running_blocks("interface GigabitEthernet0/1\n switchport mode access\n")
    assert verify(blocks, up) == []
    down = running_blocks("interface GigabitEthernet0/1\n shutdown\nip route 10.9.0.0 255.255.0.0 1.1.1.1\n")
    assert verify(blocks, down) == [
        "interface GigabitEthernet0/1 > no shutdown",
        "no ip route 10.9.0.0 255.255.0.0 1.1.1.1",
    ]


def test_cfg_changed_between_plan_and_commit_refused(approved):
    run_dir, plan = approved
    (run_dir / "candidate" / "SW2.cfg").write_text("vlan 99\nend\n", encoding="utf-8")
    fake = FakeTransport(post=POST_VLAN30)
    with pytest.raises(ApplyError, match="đã đổi"):
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.calls == []


def test_existing_backup_not_overwritten(approved):
    run_dir, plan = approved
    apply(plan, FakeTransport(post=POST_VLAN30), commit=True, confirm_token=plan.run_id)
    before = (run_dir / "apply" / "SW2.pre.cfg").read_bytes()
    fake = FakeTransport(post=POST_VLAN30)
    with pytest.raises(ApplyError, match="không ghi đè"):
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.calls == [] and (run_dir / "apply" / "SW2.pre.cfg").read_bytes() == before


def test_transport_error_during_send_is_failed_and_traced(approved):
    run_dir, plan = approved
    fake = FakeTransport(fail="config")
    with pytest.raises(ApplyFailed, match="không xác định"):
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.calls[-1] == ("close",)
    assert last_data(run_dir, "apply_error")["stage"] == "send"


def test_connect_error_is_refusal_not_failure(approved):
    run_dir, plan = approved
    fake = FakeTransport(fail="connect")
    with pytest.raises(ApplyError) as info:
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert not isinstance(info.value, ApplyFailed)
    assert ("config",) not in [c[:1] for c in fake.calls]
    assert last_data(run_dir, "apply_error")["stage"] == "connect"


# ------------------------------------------------------------------ secret
# Cấu hình chỉ có placeholder, không có secret.
# Không template nào hiện đặt placeholder ngoài ghi chú (vị trí key thật chờ TV3), nên allowlist từ chối
# mọi dòng gửi đi có placeholder (`test_placeholder_outside_comment_refused_by_allowlist`). Để chạy cơ chế
# thay giá trị lúc gửi (giữ cho template có key sau này), các test secret dùng `placeholder_carrier`:
# allowlist thật, chỉ thay placeholder bằng một từ hợp lệ TRƯỚC khi kiểm.
SECRET_CFG = "aaa new-model\n! TACACS+ key: <SECRET:tac_key>\n"  # noqa: S105
SECRET_CFG += "interface GigabitEthernet0/2\n description <SECRET:tac_key>\nexit\nend\n"


@pytest.fixture
def placeholder_carrier(monkeypatch):
    from harness_agent.apply.execute import SECRET_RE

    monkeypatch.setattr(plan_mod, "check_preapply", lambda *args: None)
    real = plan_mod.check_lines
    monkeypatch.setattr(
        plan_mod,
        "check_lines",
        lambda lines, target: real(tuple(SECRET_RE.sub("KEYREF", x) for x in lines), target),
    )


def test_placeholder_outside_comment_refused_by_allowlist(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=SECRET_CFG)
    with pytest.raises(ApplyError, match="dòng 4"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_secret_resolved_only_at_send_time(tmp_path, monkeypatch, placeholder_carrier):
    run_dir, app = make_run(tmp_path, monkeypatch, cfg=SECRET_CFG)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    ctx_secret = sorted(app.secrets)[0]
    # Dry-run không cần (và không đọc) giá trị secret.
    dry = apply(plan)
    assert dry.required_env == ("CHA_SECRET_TAC_KEY",)
    assert " description <SECRET:tac_key>" in dry.applied_lines
    monkeypatch.setenv("CHA_SECRET_TAC_KEY", SECRET_VALUE)
    post = (
        PRE.replace("hostname SW2\n", "hostname SW2\naaa new-model\n")
        .replace(
            " switchport mode access\n!\nline",
            f" switchport mode access\n description {SECRET_VALUE}\n!\nline",
        )
        .replace("!\nend", f"!\nbanner motd ^{ctx_secret}^\n!\nend")
    )
    fake = FakeTransport(pre=PRE.replace("!\nend", f"!\nbanner motd ^{ctx_secret}^\n!\nend"), post=post)
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.ok, report.verify_missing
    # Giá trị thật chỉ có trong lệnh gửi tới thiết bị; ghi chú `!` không được gửi.
    assert fake.configs == [
        ["aaa new-model", "interface GigabitEthernet0/2", f" description {SECRET_VALUE}", "exit"]
    ]
    stores = [(run_dir / "trace.jsonl").read_text(encoding="utf-8")]
    stores += [p.read_text(encoding="utf-8") for p in (run_dir / "apply").iterdir()]
    stores += [report.diff_before_after, repr(report)]
    for text in stores:
        assert SECRET_VALUE not in text
        assert ctx_secret not in text
    assert "[REDACTED]" in (run_dir / "apply" / "SW2.pre.cfg").read_text(encoding="utf-8")


def test_secret_redaction_negative_control(tmp_path, monkeypatch, placeholder_carrier):
    """Gỡ phép che giá trị đã thay → giá trị lọt vào backup: test trên bắt được."""
    from harness_agent.apply import execute

    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=SECRET_CFG)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    monkeypatch.setenv("CHA_SECRET_TAC_KEY", SECRET_VALUE)
    monkeypatch.setattr(execute, "resolve", lambda lines, environ=None: (lines, set()))
    post = PRE.replace("!\nend", f"!\ninterface Vlan1\n description {SECRET_VALUE}\n!\nend")
    apply(plan, FakeTransport(post=post), commit=True, confirm_token=plan.run_id)
    assert SECRET_VALUE in (run_dir / "apply" / "SW2.post.cfg").read_text(encoding="utf-8")


@pytest.mark.parametrize("value", [None, "", "abc\nusername x privilege 15"])
def test_bad_or_missing_secret_refused_before_connect(tmp_path, monkeypatch, value, placeholder_carrier):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=SECRET_CFG)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    if value is None:
        monkeypatch.delenv("CHA_SECRET_TAC_KEY", raising=False)
    else:
        monkeypatch.setenv("CHA_SECRET_TAC_KEY", value)
    fake = FakeTransport()
    with pytest.raises(ApplyError, match="CHA_SECRET_TAC_KEY") as info:
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.calls == []
    assert "username" not in str(info.value)


# ------------------------------------------------------------------ đối chứng âm của bản duyệt
def test_negative_control_review_check_removed(tmp_path, monkeypatch):
    """Gỡ phép kiểm bản duyệt → lượt CHƯA duyệt được áp: chứng minh test `unapproved` bắt đúng control."""
    run_dir, _ = make_run(tmp_path, monkeypatch, decision=None)
    sha = hashlib.sha256((run_dir / "candidate" / "SW2.cfg").read_bytes()).hexdigest()
    monkeypatch.setattr(
        plan_mod, "approved_review", lambda records: {"candidates": {"SW2": sha}, "reviewer": "không ai"}
    )
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    fake = FakeTransport(post=POST_VLAN30)
    apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.configs == [["vlan 30", " name FINANCE", "exit"]]


# ------------------------------------------------------------------ CLI
def _cli(run_dir: Path, tmp_path: Path, *extra: str) -> list[str]:
    return ["--run-dir", str(run_dir), "--device", "SW2", "--inventory", str(inventory(tmp_path)), *extra]


def test_cli_dry_run(tmp_path, monkeypatch, capsys):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    assert cli_main(_cli(run_dir, tmp_path)) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "| vlan 30" in out and f"--commit --confirm {run_dir.name}" in out


@pytest.mark.parametrize(
    "extra", [("--commit",), ("--confirm", "x"), ("--commit", "--confirm", "ui-20991231t000000z-abcdef")]
)
def test_cli_commit_usage_refused(tmp_path, monkeypatch, capsys, extra):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    assert cli_main(_cli(run_dir, tmp_path, *extra)) == 2
    assert "[TỪ CHỐI]" in capsys.readouterr().err
    assert ("apply", "apply_start") not in events(run_dir)


def test_cli_unapproved_refused(tmp_path, monkeypatch, capsys):
    run_dir, _ = make_run(tmp_path, monkeypatch, decision=None)
    assert cli_main(_cli(run_dir, tmp_path)) == 2
    assert "chưa có bản duyệt" in capsys.readouterr().err


def test_cli_missing_args_is_usage_error(capsys):
    assert cli_main(["--device", "SW2"]) == 2


@pytest.mark.parametrize(("post", "code"), [(POST_VLAN30, 0), (PRE, 1)])
def test_cli_commit_exit_codes(tmp_path, monkeypatch, capsys, post, code):
    import harness_agent.apply.__main__ as cli

    run_dir, _ = make_run(tmp_path, monkeypatch)
    fake = FakeTransport(post=post)
    monkeypatch.setattr(cli, "NetmikoTransport", lambda target: fake)
    assert cli_main(_cli(run_dir, tmp_path, "--commit", "--confirm", run_dir.name)) == code
    out = capsys.readouterr().out
    assert ("[OK]" in out) == (code == 0) and ("[ROLLBACK]" in out) == (code == 1)


def test_cli_transport_failure_after_start_exit_1(tmp_path, monkeypatch, capsys):
    import harness_agent.apply.__main__ as cli

    run_dir, _ = make_run(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "NetmikoTransport", lambda target: FakeTransport(fail="config"))
    assert cli_main(_cli(run_dir, tmp_path, "--commit", "--confirm", run_dir.name)) == 1


# ------------------------------------------------------------------ cô lập
@pytest.mark.slow
def test_netmiko_imported_lazily_and_credentials_from_env(monkeypatch):
    import subprocess

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import harness_agent.apply.transport; "
            "assert 'netmiko' not in sys.modules and 'paramiko' not in sys.modules",
        ],
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    from harness_agent.collector.fetch import InventoryDevice

    device = InventoryDevice("SW2", "10.0.0.2", "access_switch", "HQ", "iosvl2", "GigabitEthernet1/3")
    monkeypatch.delenv("COLLECTOR_USER", raising=False)
    monkeypatch.delenv("COLLECTOR_PASSWORD", raising=False)
    with pytest.raises(TransportError, match="COLLECTOR_USER"):
        NetmikoTransport(device).connect()
    with pytest.raises(TransportError, match="show running-config"):
        NetmikoTransport(device).send_show("show version")


def test_apply_not_reachable_from_measurement_or_ui():
    """Apply không bao giờ tự động: runner, evaluator, corpus, UI, web, harness và mọi script chạy lô không
    import nó."""
    pattern = re.compile(r"harness_agent\.apply|from harness_agent import .*\bapply\b|from \.\.apply")
    offenders = []
    for folder in ("runner", "eval", "corpus", "ui", "web", "harness", "check", "sensors", "tools"):
        for path in (ROOT / "src" / "harness_agent" / folder).rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.relative_to(ROOT).as_posix())
    for path in (ROOT / "scripts").glob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


# ------------------------------------------------------------------ fix vòng 1: allowlist, mức, snapshot
@pytest.mark.parametrize(
    "cfg",
    [
        "vlan 30\n name X\nexit\nexit\nwr\nend\n",  # exit thừa → EXEC → write memory
        "vlan 30\n name X\nexit\nwri mem\nend\n",
        "cop run st\nend\n",
        "rel\nend\n",
        "exit\nend\n",
        " name X\nend\n",  # dòng con không có khối mở
        "interface GigabitEthernet0/2\n name X\nexit\nend\n",  # dòng con của khối khác loại
        "vlan 30\nend\nvlan 31\n",  # lệnh sau end
        "vlan 30\n name X\nexit\nusername admin privilege 15 secret Zz9-SeCr3t\nend\n",
        "interface GigabitEthernet0/2\n description ok\n do write memory\nexit\nend\n",
    ],
)
def test_config_outside_template_allowlist_refused(tmp_path, monkeypatch, cfg):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=cfg)
    with pytest.raises(ApplyError, match=r"dòng \d+") as info:
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    assert "Zz9" not in str(info.value) and "wr" not in str(info.value).split(":")[0]


def _rendered_samples() -> list[tuple[str, str]]:
    from registry_samples import VALID_ARGS

    from harness_agent.tools.registry.render import render_by_device
    from harness_agent.tools.registry.tools import TOOL_REGISTRY

    variants = [(tool, args) for tool, args in VALID_ARGS.items()]
    variants += [
        ("set_interface_state", {**VALID_ARGS["set_interface_state"], "enabled": False}),
        (
            "add_static_route",
            {"device": "R1", "prefix": "10.20.0.0/16", "exit_interface": "GigabitEthernet0/1"},
        ),
        ("add_acl_entry", {**VALID_ARGS["add_acl_entry"], "src": "any", "dst": "any", "dst_port": None}),
        ("set_aaa_login", {**VALID_ARGS["set_aaa_login"], "tacacs_key_ref": None, "methods": ["local"]}),
    ]
    out = []
    for tool, args in variants:
        model = TOOL_REGISTRY[tool].args_model.model_validate(args)
        out += [(tool, text) for text in render_by_device([(tool, model)]).values()]
    return out


def test_every_template_rendering_passes_allowlist():
    from harness_agent.collector.fetch import InventoryDevice

    samples = _rendered_samples()
    assert {tool for tool, _ in samples} == set(__import__("registry_samples").EXPECTED_TOOLS)
    device = InventoryDevice("X", "10.0.0.1", "router", "HQ", "ios", "Ethernet9/9")
    for _tool, text in samples:
        plan_mod.check_lines(tuple(text.splitlines()), device)  # không raise
    # Ghép nhiều thao tác trong một file (như change package nhiều thao tác) cũng qua.
    plan_mod.check_lines(tuple("".join(t.removesuffix("end\n") for _, t in samples).splitlines()), device)


@pytest.mark.parametrize("level", ["B0", "B1", "B2", None])
def test_run_below_b3_refused(tmp_path, monkeypatch, level):
    run_dir, _ = make_run(tmp_path, monkeypatch)
    path = run_dir / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if level is None:
        result.pop("level")
    else:
        result["level"] = level
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ApplyError, match="B3/B4"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_non_utf8_cfg_refused(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg="vlan 30\n name CAF\xc9\nexit\nend\n".encode("latin-1"))
    with pytest.raises(ApplyError, match="UTF-8"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    assert cli_main(_cli(run_dir, tmp_path)) == 2


@pytest.mark.parametrize(
    "pre",
    ["", PRE.removesuffix("end\n"), PRE.replace("hostname SW2", "hostname SW9"), "% Authorization failed.\n"],
)
def test_invalid_pre_snapshot_refused_nothing_sent(approved, pre):
    run_dir, plan = approved
    fake = FakeTransport(pre=pre)
    with pytest.raises(ApplyError, match="hostname SW2") as info:
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert not isinstance(info.value, ApplyFailed)
    assert fake.configs == [] and fake.calls[-1] == ("close",)
    assert last_data(run_dir, "apply_error")["stage"] == "backup"
    assert ("apply", "apply_start") not in events(run_dir)
    assert not (run_dir / "apply" / "SW2.pre.cfg").exists()


def test_snapshot_hostname_case_insensitive(approved):
    run_dir, plan = approved
    pre = PRE.replace("hostname SW2", "hostname sw2")
    post = pre.replace("!\nend", "!\nvlan 30\n name FINANCE\n!\nend")
    report = apply(plan, FakeTransport(pre=pre, post=post), commit=True, confirm_token=plan.run_id)
    assert report.ok


def test_device_errors_trigger_rollback(tmp_path, monkeypatch, capsys):
    import harness_agent.apply.__main__ as cli

    run_dir, _ = make_run(tmp_path, monkeypatch)
    fake = FakeTransport(post=POST_VLAN30, output="% Invalid input detected at '^' marker.\n")
    monkeypatch.setattr(cli, "NetmikoTransport", lambda target: fake)
    assert cli_main(_cli(run_dir, tmp_path, "--commit", "--confirm", run_dir.name)) == 1
    assert fake.configs[1] == ["no vlan 30"]
    out = capsys.readouterr().out
    assert "1 dòng lỗi (%)" in out and "[ROLLBACK]" in out and "còn 0 dòng lệch" in out
    assert last_data(run_dir, "apply_rollback")["device_errors"] == 1


def test_residual_diff_reported(approved):
    run_dir, plan = approved
    report = apply(plan, FakeTransport(post=PRE, after=POST_VLAN30), commit=True, confirm_token=plan.run_id)
    assert report.rolled_back and report.residual_diff_lines > 0
    assert last_data(run_dir, "apply_rollback")["residual_diff_lines"] == report.residual_diff_lines


@pytest.mark.parametrize("field", ["lines", "target", "host", "inventory", "run_id"])
def test_hand_built_plan_cannot_push(approved, tmp_path, field):
    import dataclasses

    from harness_agent.collector.fetch import InventoryDevice

    run_dir, plan = approved
    changes = {
        "lines": ("vlan 99", "exit", "end"),
        "target": InventoryDevice("SW2", "10.9.9.9", "access_switch", "HQ", "iosvl2", "GigabitEthernet1/3"),
        "host": "10.9.9.9",
        "inventory": None,
        "run_id": "ui-20991231t000000z-abcdef",
    }
    forged = dataclasses.replace(plan, **{field: changes[field]})
    fake = FakeTransport(post=POST_VLAN30)
    with pytest.raises(ApplyError):
        apply(forged, fake, commit=True, confirm_token=forged.run_id)
    assert fake.calls == []


def test_recheck_rereads_review(approved):
    """Bản duyệt bị xoá khỏi trace (chuỗi vẫn liền mạch vì xoá đuôi) sau khi lập kế hoạch → không áp."""
    run_dir, plan = approved
    path = run_dir / "trace.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(x for x in lines if '"human_review"' not in x), encoding="utf-8")
    fake = FakeTransport(post=POST_VLAN30)
    with pytest.raises(ApplyError, match="chưa có bản duyệt"):
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.calls == []


@pytest.mark.parametrize("exc", [OSError("/secret/path"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x")])
def test_cli_maps_read_errors_to_exit_2(tmp_path, monkeypatch, capsys, exc):
    import harness_agent.apply.__main__ as cli

    run_dir, _ = make_run(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cli.ApplyPlan, "from_run", boom)
    assert cli_main(_cli(run_dir, tmp_path)) == 2
    err = capsys.readouterr().err
    assert type(exc).__name__ in err and "/secret/path" not in err


# ---------------------------------------------------------- fix vòng 2: ký tự điều khiển, allowlist hẹp
CTRL_PROBES = [
    "interface GigabitEthernet0/2\n description x\x1awrite memory\nexit\nend\n",  # Ctrl-Z = end trên IOS
    "ip route 10.20.0.0 255.255.0.0 10.0.12.2\x1awrite memory\nend\n",
    "aaa new-model\naaa authentication login VTY local\x1awrite memory\nend\n",
    "vlan 30\n name FIN\x1awr\nexit\nend\n",
    "ip access-list extended A\n 10 permit ip any any\x1awrite memory\nexit\nend\n",
    "vlan 30\n\tname FIN\nexit\nend\n",  # tab
    "vlan 30\n name FIN\x7f\nexit\nend\n",  # DEL
    "vlan 30\n name FIN\u0085\nexit\nend\n",  # C1 NEL
    "vlan 30\n name FIN\u2028wr\nexit\nend\n",  # LINE SEPARATOR
    "vlan 30\n name\u00a0FIN\nexit\nend\n",  # NBSP
    "vlan 30\r\n name FIN\nexit\nend\n",  # CR
    "vlan 30\n name FIN\x0bwr\nexit\nend\n",  # VT: splitlines() từng tách ở đây
]


@pytest.mark.parametrize("cfg", CTRL_PROBES)
def test_control_and_non_ascii_characters_refused(tmp_path, monkeypatch, cfg):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=cfg)
    with pytest.raises(ApplyError, match=r"^dòng \d+: ký tự ngoài ASCII") as info:
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    assert "write" not in str(info.value)


def test_control_character_negative_control(tmp_path, monkeypatch):
    """Gỡ phép kiểm ký tự → dòng có Ctrl-Z được gửi nguyên văn tới thiết bị: test trên bắt đúng control."""
    monkeypatch.setattr(plan_mod, "check_preapply", lambda *args: None)
    monkeypatch.setattr(plan_mod, "_PRINTABLE_LINE", re.compile(r"(?s).*"))
    monkeypatch.setattr(
        plan_mod, "ALLOWED_CHILD", {**plan_mod.ALLOWED_CHILD, "interface": (re.compile(r"(?s).+"),)}
    )
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=CTRL_PROBES[0])
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    fake = FakeTransport(post=PRE)
    apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert " description x\x1awrite memory" in fake.configs[0]


@pytest.mark.parametrize(
    "cfg",
    [
        "aaa authentication login default none\nend\n",
        "aaa authentication login VTY local enable\nend\n",
        "aaa authentication login VTY local local local local\nend\n",
        "interface Tunnel0\n description x\nexit\nend\n",
        "interface Port-channel1\n description x\nexit\nend\n",
        "interface range GigabitEthernet0/1 - 2\n shutdown\nexit\nend\n",
        "interface GigabitEthernet0/1.10\n shutdown\nexit\nend\n",
        "ip access-list extended A\n 10 permit foo bar\nexit\nend\n",
        "ip access-list extended A\n 10 permit ip any any log\nexit\nend\n",
        "ip access-list extended A\n 10 permit ip any any eq 22\nexit\nend\n",  # cổng chỉ với tcp/udp
        "ip access-list extended A\n 10 permit tcp any any range 1 2\nexit\nend\n",
        "ip access-list extended A\n 10 permit tcp object-group X any\nexit\nend\n",
        "ip route 10.0.0.0 255.0.0.0 Null0\nend\n",
        "ip route 10.0.0.0 255.0.0.0 10.0.0.1 250 name X\nend\n",
        "ip route vrf X 10.0.0.0 255.0.0.0 10.0.0.1\nend\n",
        "vlan 30\n name FINANCE-DEPT-WITH-A-VERY-LONG-NAME-X\nexit\nend\n",  # > 32 ký tự
        "vlan 30\n name A;B\nexit\nend\n",
        "interface GigabitEthernet0/2\n description a<b>\nexit\nend\n",
        "interface GigabitEthernet0/2\n description " + "x" * 81 + "\nexit\nend\n",
        "interface GigabitEthernet0/2\n ip access-group A in extra\nexit\nend\n",
        "line vty 0 15\n login authentication VTY\nexit\nend\n",
        "line vty 0 4\n login local\nexit\nend\n",
    ],
)
def test_allowlist_is_exactly_template_shapes(tmp_path, monkeypatch, cfg):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=cfg)
    with pytest.raises(ApplyError, match=r"^dòng \d+"):
        ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))


def test_hostname_must_be_top_level_command():
    from harness_agent.apply.execute import snapshot_ok

    assert snapshot_ok("hostname SW2\n!\nend\n", "SW2")
    assert not snapshot_ok("banner motd ^\n hostname SW2\n^\nend\n", "SW2")
    assert not snapshot_ok("! hostname SW2\nend\n", "SW2")
    assert not snapshot_ok("hostname SW2-B\nend\n", "SW2")
    assert not snapshot_ok("interface Gi0/1\n description hostname SW2\nend\n", "SW2")


# ------------------------------------------------------------------ VLAN ngoài running-config (VTP)
# VTP server/client giữ VLAN trong vlan.dat, KHÔNG in trong running-config. Rollback chỉ dựa vào
# running-config sẽ thấy VLAN 30 "chưa có" và gửi `no vlan 30`, xoá một VLAN vốn có.
PRE_VTP = PRE.replace("vlan 10\n name USERS\n!\n", "")
VTP_HEAD = (
    "VLAN Name                             Status    Ports\n"
    "---- -------------------------------- --------- -------------------------------\n"
)
VTP_BRIEF_PRE = (
    VTP_HEAD
    + "1    default                          active    Gi0/0, Gi0/3, Gi1/0, Gi1/1\n"
    + "                                                Gi1/2\n"
    + "10   USERS                            active    Gi0/1\n"
    + "30   OLDNAME                          active    \n"
    + "1002 fddi-default                     act/unsup \n"
)
VTP_BRIEF_ABSENT = VTP_BRIEF_PRE.replace("30   OLDNAME                          active    \n", "")
VTP_BRIEF_FINANCE = VTP_BRIEF_PRE.replace("OLDNAME", "FINANCE")


def test_vtp_server_vlan_verified_by_show_vlan_brief(approved):
    """VLAN 30 không bao giờ hiện trong running-config (VTP server), nhưng `show vlan brief` có `30 FINANCE
    active` → áp đủ, không rollback."""
    _, plan = approved
    fake = FakeTransport(pre=PRE_VTP, post=PRE_VTP, vlans=[VTP_BRIEF_PRE, VTP_BRIEF_FINANCE])
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.ok and not report.rolled_back, report.verify_missing
    assert fake.configs == [["vlan 30", " name FINANCE", "exit"]]


def test_vtp_server_preexisting_vlan_never_deleted_by_rollback(approved):
    run_dir, plan = approved
    # Thiết bị không nhận tên mới: verify thiếu → rollback. VLAN 30 có từ trước (vlan.dat): trả tên cũ.
    fake = FakeTransport(pre=PRE_VTP, post=PRE_VTP, vlans=[VTP_BRIEF_PRE, VTP_BRIEF_PRE, VTP_BRIEF_PRE])
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.rolled_back and report.verify_missing == ("vlan 30 > name FINANCE",)
    assert fake.configs[1] == ["vlan 30", " name OLDNAME", "exit"]
    assert not any(x.startswith("no vlan") for batch in fake.configs for x in batch)
    assert last_data(run_dir, "apply_rollback")["residual_vlans"] == 0


def test_vtp_server_vlan_confirmed_absent_is_removed(approved):
    _, plan = approved
    fake = FakeTransport(pre=PRE_VTP, post=PRE_VTP, vlans=[VTP_BRIEF_ABSENT] * 3)
    report = apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert report.rolled_back and fake.configs[1] == ["no vlan 30"]


def test_vtp_negative_control_wrong_table_deletes_vlan(approved, monkeypatch):
    """Đối chứng âm: bảng VLAN trước khi áp bị bỏ qua (coi như VLAN 30 chưa có) → rollback xoá VLAN 30 có
    sẵn. Test trên bắt đúng lỗi này."""
    from harness_agent.apply import execute

    _, plan = approved
    real = execute.vlan_table
    monkeypatch.setattr(execute, "vlan_table", lambda text: {k: v for k, v in real(text).items() if k != 30})
    fake = FakeTransport(pre=PRE_VTP, post=PRE_VTP, vlans=[VTP_BRIEF_PRE] * 3)
    apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert fake.configs[1] == ["no vlan 30"]


@pytest.mark.parametrize(
    "brief",
    [
        "",
        "% Invalid input detected at '^' marker.\n",
        VTP_HEAD,
        "30 X active\n",
    ],
)
def test_unreadable_vlan_table_refused_nothing_sent(approved, brief):
    run_dir, plan = approved
    fake = FakeTransport(pre=PRE_VTP, vlans=[brief])
    with pytest.raises(ApplyError, match="show vlan brief") as info:
        apply(plan, fake, commit=True, confirm_token=plan.run_id)
    assert not isinstance(info.value, ApplyFailed)
    assert fake.configs == [] and fake.calls[-1] == ("close",)
    assert last_data(run_dir, "apply_error")["error"] == "pre_vlan_table_invalid"


def test_rollback_never_emits_no_vlan_without_confirmed_absence():
    from harness_agent.apply import cfg_blocks, rollback_lines, running_blocks
    from harness_agent.apply.execute import vlan_table

    blocks = cfg_blocks(["vlan 30", " name FINANCE", "exit"])
    pre = running_blocks(PRE_VTP)
    assert rollback_lines(blocks, pre) == []  # không có bảng VLAN: không biết → không xoá
    assert rollback_lines(blocks, pre, vlan_table(VTP_BRIEF_ABSENT)) == ["no vlan 30"]
    assert rollback_lines(blocks, pre, vlan_table(VTP_BRIEF_PRE)) == ["vlan 30", " name OLDNAME", "exit"]
    assert rollback_lines(blocks, pre, vlan_table(VTP_BRIEF_FINANCE)) == []  # tên cũ trùng tên mới


def test_vlan_table_parser():
    from harness_agent.apply.execute import vlan_table

    table = vlan_table(VTP_BRIEF_PRE)
    assert table == {
        1: ("default", "active"),
        10: ("USERS", "active"),
        30: ("OLDNAME", "active"),
        1002: ("fddi-default", "act/unsup"),
    }
    assert vlan_table(VTP_BRIEF_PRE.replace("\n", "\r\n")) == table
    for bad in ("", VTP_HEAD, VTP_BRIEF_PRE.replace("1    default", "1    "), VTP_BRIEF_ABSENT + "40\n"):
        assert vlan_table(bad) is None


def test_netmiko_transport_allows_show_vlan_brief_only():
    from harness_agent.collector.fetch import InventoryDevice

    device = InventoryDevice("SW2", "10.0.0.2", "access_switch", "HQ", "iosvl2", "GigabitEthernet1/3")
    with pytest.raises(TransportError, match="chưa kết nối"):
        NetmikoTransport(device).send_show("show vlan brief")
    with pytest.raises(TransportError, match="show vlan brief"):
        NetmikoTransport(device).send_show("show vlan")


# ------------------------------------------------------------------ `aaa new-model` không có username
AAA_CFG = (
    "aaa new-model\naaa authentication login VTY local\nline vty 0 4\n login authentication VTY\nexit\nend\n"
)


def test_dry_run_warns_aaa_new_model_without_username_in_context(tmp_path, monkeypatch):
    import dataclasses

    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=AAA_CFG)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    assert plan.context_local_users == 1  # campus-small-01: SW2 có `netadmin`
    assert apply(plan).warnings == ()  # đối chứng: context có username → không cảnh báo
    bare = dataclasses.replace(plan, context_local_users=0)
    report = apply(bare)
    assert report.dry_run and len(report.warnings) == 1 and "aaa new-model" in report.warnings[0]
    assert last_data(run_dir, "apply_dry_run")["warnings"] == list(report.warnings)


def test_commit_warns_aaa_new_model_without_username_in_running_config(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=AAA_CFG)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    post = PRE.replace("hostname SW2\n", "hostname SW2\n" + AAA_CFG.removesuffix("end\n"))
    report = apply(plan, FakeTransport(post=post), commit=True, confirm_token=plan.run_id)
    assert report.ok and len(report.warnings) == 1 and "username" in report.warnings[0]
    assert last_data(run_dir, "apply_start")["warnings"] == list(report.warnings)


def test_commit_no_warning_when_running_config_has_username(tmp_path, monkeypatch):
    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=AAA_CFG)
    plan = ApplyPlan.from_run(run_dir, "SW2", inventory(tmp_path))
    pre = PRE.replace("hostname SW2\n", "hostname SW2\nusername netadmin privilege 15 secret 9 $9$abc\n")
    post = pre.replace("hostname SW2\n", "hostname SW2\n" + AAA_CFG.removesuffix("end\n"))
    report = apply(plan, FakeTransport(pre=pre, post=post), commit=True, confirm_token=plan.run_id)
    assert report.ok and report.warnings == ()


def test_cli_prints_lockout_warning_to_stderr(tmp_path, monkeypatch, capsys):
    import dataclasses

    import harness_agent.apply.__main__ as cli

    run_dir, _ = make_run(tmp_path, monkeypatch, cfg=AAA_CFG)
    real = cli.ApplyPlan.from_run
    monkeypatch.setattr(
        cli.ApplyPlan,
        "from_run",
        classmethod(lambda c, *a, **k: dataclasses.replace(real(*a, **k), context_local_users=None)),
    )
    assert cli_main(_cli(run_dir, tmp_path)) == 0
    err = capsys.readouterr().err
    assert "[CẢNH BÁO]" in err and "aaa new-model" in err

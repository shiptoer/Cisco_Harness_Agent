"""Test bộ khung chạy thử: context store, gate, render và pipeline hai giai đoạn chạy khô (fake_dsh).

Không gọi LLM, không cần API key — chạy được trên CI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness_agent.harness.context_store import ContextError, ContextStore
from harness_agent.harness.leak import secret_lines
from harness_agent.models import parse_tool_call
from harness_agent.runner.dsh import DshSetupError, LocalConfig, build_patch
from harness_agent.runner.pipeline import CaseResult, ErrorCode, _is_package_file, expect_mismatches, run_case
from harness_agent.tools.registry.render import render_by_device

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
CASES = sorted((ROOT / "data" / "cases").glob("*.json"))


@pytest.fixture(scope="module")
def store() -> ContextStore:
    return ContextStore.load(CAMPUS)


# ---------------------------------------------------------------- context
def test_projection_never_leaks_secrets(store: ContextStore):
    dumped = json.dumps([store.project_device(h) for h in store.hostnames])
    dumped += json.dumps(store.project_device_list())
    secrets = store.secret_values()
    assert secrets, "đối chứng: context mẫu phải có secret để test có ý nghĩa"
    assert not any(s in dumped for s in secrets)
    assert "aaa" not in dumped and "secret" not in dumped.lower()


def test_projection_is_allowlist(store: ContextStore, tmp_path: Path):
    raw = json.loads(CAMPUS.read_text(encoding="utf-8"))
    raw["devices"][2]["interfaces"][1]["future_field"] = "LEAK-ME"
    p = tmp_path / "ctx.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    s = ContextStore.load(p)
    assert "LEAK-ME" not in json.dumps(s.project_device("SW2"))


def test_untrusted_text_only_in_get_interface(store: ContextStore):
    assert "untrusted_text" not in json.dumps(store.project_device("SW2"))
    assert "untrusted_text" in store.project_interface("SW2", "GigabitEthernet0/1", include_untrusted=True)


def test_corrupted_context_rejected(tmp_path: Path):
    raw = json.loads(CAMPUS.read_text(encoding="utf-8"))
    raw["devices"][2]["interfaces"][1]["access_vlan"] = 777  # VLAN không tồn tại
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContextError) as ei:
        ContextStore.load(p)
    assert ei.value.violations[0].code == "CTX_DANGLING_VLAN"


# ----------------------------------------------------------------- render
def _tc(tool: str, **args):
    return parse_tool_call({"tool": tool, "args": args})


def test_render_golden():
    calls = [
        _tc("create_vlan", device="SW2", vlan_id=30, name="FINANCE"),
        _tc("set_access_vlan", device="SW2", interface="GigabitEthernet1/1", vlan_id=30),
        _tc("add_static_route", device="R1", prefix="10.20.0.0/16", next_hop="10.0.12.2"),
        _tc("set_interface_state", device="R1", interface="GigabitEthernet0/2", enabled=True),
    ]
    ops = [(c.tool, c.parsed) for c in calls]
    out = render_by_device(ops)
    assert list(out) == ["SW2", "R1"]  # thứ tự thiết bị theo lần xuất hiện đầu tiên
    assert out["SW2"] == (
        "vlan 30\n name FINANCE\nexit\ninterface GigabitEthernet1/1\n switchport mode access\n"
        " switchport access vlan 30\nexit\nend\n"
    )
    assert (
        out["R1"]
        == "ip route 10.20.0.0 255.255.0.0 10.0.12.2\ninterface GigabitEthernet0/2\n no shutdown\nexit\nend\n"
    )
    assert render_by_device(ops) == out  # deterministic


def test_render_refuses_unvalidated_args():
    """Renderer chỉ nhận args đã qua args_model của đúng tool đó."""
    route = _tc("add_static_route", device="R1", prefix="10.20.0.0/16", next_hop="10.0.12.2")
    with pytest.raises(TypeError):
        render_by_device([("create_vlan", {"device": "SW2", "vlan_id": 30, "name": "X\nend"})])
    with pytest.raises(TypeError):
        render_by_device([("create_vlan", route.parsed)])
    with pytest.raises(KeyError):
        render_by_device([("run_cli", route.parsed)])


# ------------------------------------------------------------ patch + e2e
def _fake_cfg(tmp_path: Path) -> LocalConfig:
    return LocalConfig(
        dsh_command=[sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        dsh_home=tmp_path / "dsh_home",
        base_patch=ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        python_exe=Path(sys.executable),
        requires_api_key=False,
    )


def test_patch_requires_disabled_rows(tmp_path: Path):
    cfg = _fake_cfg(tmp_path)
    bad = tmp_path / "base.yml"
    bad.write_text(
        cfg.base_patch.read_text(encoding="utf-8").replace("- id: tool-bash\n  disabled: true\n", ""),
        encoding="utf-8",
    )
    cfg.base_patch = bad
    with pytest.raises(DshSetupError, match="tool-bash"):
        build_patch(cfg, "cba", CAMPUS, "network_admin", tmp_path)


def _bare_name_cfg(tmp_path: Path, name: str, python_exe: str | None = None) -> Path:
    toml = tmp_path / "cfg.toml"
    toml.write_text(
        f'[dsh]\ncommand = ["{name}", "scripts/fake_dsh.py"]\nhome = "{(tmp_path / "home").as_posix()}"\n'
        f'requires_api_key = false\n\n[python]\nexe = "{python_exe or name}"\n',
        encoding="utf-8",
    )
    return toml


def test_fake_config_resolves_bare_python_to_running_interpreter():
    """config/fake.toml ghi "python" trần ở [dsh] command và [python] exe: phải thành sys.executable."""
    cfg = LocalConfig.load(ROOT / "config" / "fake.toml")
    assert cfg.dsh_command[0] == sys.executable
    assert cfg.python_exe == Path(sys.executable)


@pytest.mark.slow
@pytest.mark.parametrize("name", ["python", "python3", "python.exe", "PYTHON.EXE"])
def test_bare_python_does_not_depend_on_path(tmp_path: Path, monkeypatch, name: str):
    """Chạy `.venv\\Scripts\\python.exe` mà không Activate: PATH không có venv (hoặc trỏ tới Python gốc).

    Đối chứng âm: với PATH này, cách cũ (tìm "python" qua PATH) không ra python đang chạy, nên
    harness server và fake_dsh sẽ chạy ngoài venv và thiếu package của repo.
    """
    empty = tmp_path / "no-python-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which(name) is None, "đối chứng: PATH này không tìm thấy python"

    cfg = LocalConfig.load(_bare_name_cfg(tmp_path, name))
    assert cfg.dsh_command[0] == sys.executable
    assert cfg.python_exe == Path(sys.executable)
    proc = subprocess.run(  # noqa: S603 — lệnh lấy từ config test, không từ input
        [cfg.dsh_command[0], "-c", "import sys, harness_agent; print(sys.prefix)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(proc.stdout.strip()) == Path(sys.prefix)


def test_bare_dsh_command_not_on_path_is_setup_error(tmp_path: Path):
    with pytest.raises(DshSetupError, match="không tìm thấy"):
        LocalConfig.load(_bare_name_cfg(tmp_path, "khong-co-lenh-nay-xyz", Path(sys.executable).as_posix()))


@pytest.mark.slow
@pytest.mark.parametrize("case_file", CASES, ids=lambda p: p.stem)
def test_dry_run_cases(case_file: Path, tmp_path: Path, monkeypatch):
    case = json.loads(case_file.read_text(encoding="utf-8"))
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(ROOT / case["fake_scenario"]))
    r = run_case(
        _fake_cfg(tmp_path), ROOT / case["context"], case["intent"], case["role"], tmp_path, case_file.stem
    )
    assert expect_mismatches(r, case["expect"]) == [], r.reason
    # Ở mọi case: secret của context không nằm trong file nào ngoài change package (D-018).
    secrets = ContextStore.load(ROOT / case["context"]).secret_values()
    run_dir = Path(r.run_dir)
    leaked = [
        p.relative_to(run_dir).as_posix()
        for p in run_dir.rglob("*")
        if p.is_file() and not _is_package_file(run_dir, p) and secret_lines(p.read_bytes(), secrets)
    ]
    assert secrets and leaked == []


def test_expect_mismatches_compares_leak_sources_by_kind():
    r = CaseResult(run_id="x", status="error", verdict=None, error_code=ErrorCode.SECRET_IN_ARGS)
    r.leak_sources = [
        {"source": "raw_stream", "stage": "cba", "stream": "stdout", "line": 2},
        {"source": "raw_stream", "stage": "cba", "stream": "stdout", "line": 5},
        {"source": "harness_blocked", "stage": "cba", "fields": ["args.x"]},
    ]
    ok = {
        "status": "error",
        "error_code": "SECRET_IN_ARGS",
        "leak_sources": ["raw_stream", "harness_blocked"],
    }
    assert expect_mismatches(r, ok) == []
    assert expect_mismatches(r, {**ok, "leak_sources": []}) != []
    assert expect_mismatches(r, {**ok, "leak_sources": ["raw_stream"]}) != []
    assert expect_mismatches(r, {**ok, "error_code": None}) != []
    # Trường lạ trong expect (vd gõ sai tên) không được âm thầm coi là khớp.
    assert expect_mismatches(r, {"verdic": None}) != []


@pytest.mark.slow
def test_rerun_ignores_stale_files_in_run_dir(tmp_path: Path, monkeypatch):
    """Chạy lại cùng run_id: file sót từ lần trước (TERMINATED, change_request.json) không được đọc lại."""
    case_file = ROOT / "data" / "cases" / "clean-vlan30.json"
    case = json.loads(case_file.read_text(encoding="utf-8"))
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(ROOT / case["fake_scenario"]))
    stale = tmp_path / "clean-vlan30"
    stale.mkdir()
    (stale / "TERMINATED").write_text("G6_injection_scan", encoding="utf-8")
    (stale / "rejects.jsonl").write_text("{}\n", encoding="utf-8")
    r = run_case(
        _fake_cfg(tmp_path), ROOT / case["context"], case["intent"], case["role"], tmp_path, "clean-vlan30"
    )
    assert (r.status, r.verdict, r.stopped_by) == ("ok", "accept", None), r.reason
    assert not (stale / "rejects.jsonl").exists()


@pytest.mark.parametrize("bad_run_id", ["../escape", "a/b", "."])
def test_run_id_cannot_escape_out_root(tmp_path: Path, bad_run_id: str):
    out_root = tmp_path / "runs"
    out_root.mkdir()
    victim = tmp_path / "escape"
    victim.mkdir()
    (victim / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="run_id"):
        run_case(_fake_cfg(tmp_path), CAMPUS, "x", "network_admin", out_root, bad_run_id)
    assert (victim / "keep.txt").exists()


# ------------------------------------------------- task 3.3: tool mới qua pipeline
def _run_named_case(name: str, tmp_path: Path, monkeypatch):
    case = json.loads((ROOT / "data" / "cases" / f"{name}.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(ROOT / case["fake_scenario"]))
    return run_case(_fake_cfg(tmp_path), ROOT / case["context"], case["intent"], case["role"], tmp_path, name)


@pytest.mark.slow
def test_acl_case_candidate_config(tmp_path: Path, monkeypatch):
    r = _run_named_case("clean-acl-entry", tmp_path, monkeypatch)
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    cfg = (tmp_path / "clean-acl-entry" / "candidate" / "SW1.cfg").read_text(encoding="utf-8")
    assert cfg == (
        "ip access-list extended SERVERS-PROTECT\n"
        " 60 permit tcp 10.10.20.0 0.0.0.255 host 10.10.50.20 eq 22\nexit\nend\n"
    )


@pytest.mark.slow
def test_aaa_case_change_package_has_placeholder_only(tmp_path: Path, monkeypatch, store: ContextStore):
    r = _run_named_case("clean-aaa-tacacs", tmp_path, monkeypatch)
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    assert (r.leak_attempt_detected, r.leak_in_package) == (False, False)
    run_dir = tmp_path / "clean-aaa-tacacs"
    cfg = (run_dir / "candidate" / "R1.cfg").read_text(encoding="utf-8")
    package = (run_dir / "change_package.md").read_text(encoding="utf-8")
    assert "! TACACS+ key: <SECRET:tacacs_key>" in cfg
    assert not any(s in cfg + package for s in store.secret_values())


@pytest.mark.slow
def test_runner_rejects_staged_ops_outside_approved_change_request(tmp_path: Path, monkeypatch):
    """So khớp hai chiều (D-016): thao tác có trong staged_ops.jsonl mà không có trong change request
    đã duyệt thì runner báo ERROR và không render, kể cả khi dòng đó hợp lệ về schema."""
    from harness_agent.runner import pipeline

    real_run_stage = pipeline._run_stage
    forged = {
        "tool": "set_interface_state",
        "args": {"device": "SW2", "interface": "GigabitEthernet0/0", "enabled": False},
    }

    def run_stage_then_forge(cfg, stage, ctx, role, run_dir, *rest):
        result = real_run_stage(cfg, stage, ctx, role, run_dir, *rest)
        if stage == "tsa":
            with (run_dir / "staged_ops.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(forged) + "\n")
        return result

    # Đối chứng: không chèn gì thì case này accept.
    assert _run_named_case("clean-vlan30", tmp_path, monkeypatch).verdict == "accept"

    monkeypatch.setattr(pipeline, "_run_stage", run_stage_then_forge)
    r = _run_named_case("clean-vlan30", tmp_path, monkeypatch)
    assert (r.status, r.verdict, r.error_code) == ("error", None, "STAGED_MISMATCH")
    assert "ngoài change request" in r.reason
    assert not (tmp_path / "clean-vlan30" / "candidate").exists()


def test_internal_error_marker_makes_stage_an_error(tmp_path: Path):
    from harness_agent.runner.dsh import DshResult
    from harness_agent.runner.pipeline import _check_stage

    ok = DshResult(0, [{"type": "final"}], [], "", tmp_path / "o", tmp_path / "e", 0)
    assert _check_stage(ok, "tsa", tmp_path) is None
    (tmp_path / "INTERNAL_ERROR").write_text("RuntimeError: x", encoding="utf-8")
    code, reason = _check_stage(ok, "tsa", tmp_path)
    assert code == "HARNESS_INTERNAL_ERROR" and "lỗi nội bộ" in reason


@pytest.mark.slow
@pytest.mark.parametrize("mutation", ["duplicate", "reordered"])
def test_runner_rejects_changed_staged_sequence(tmp_path: Path, monkeypatch, mutation: str):
    """Runner independently checks staged multiplicity and order."""
    from harness_agent.runner import pipeline

    real_run_stage = pipeline._run_stage
    corrupted = False

    def run_stage_then_corrupt(cfg, stage, ctx, role, run_dir, prompt, trace, *rest):
        nonlocal corrupted

        result = real_run_stage(cfg, stage, ctx, role, run_dir, prompt, trace, *rest)

        if stage == "tsa":
            staged_path = run_dir / "staged_ops.jsonl"
            lines = [line for line in staged_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert len(lines) == 2, "Fixture must stage two operations"
            assert json.loads(lines[0]) != json.loads(lines[1])

            if mutation == "duplicate":
                lines.insert(1, lines[0])
            else:
                lines.reverse()

            staged_path.write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
            )
            corrupted = True

        return result

    # Positive control: the unchanged pipeline must accept this case.
    baseline = _run_named_case("clean-vlan30", tmp_path, monkeypatch)
    assert (baseline.status, baseline.verdict) == ("ok", "accept")

    monkeypatch.setattr(pipeline, "_run_stage", run_stage_then_corrupt)

    def forbid_render(*args, **kwargs):
        pytest.fail("Runner must reject before calling the renderer")

    monkeypatch.setattr(pipeline, "render_by_device", forbid_render)

    result = _run_named_case("clean-vlan30", tmp_path, monkeypatch)

    assert corrupted, "Test must actually modify staged_ops.jsonl"
    assert (
        result.status,
        result.verdict,
        result.error_code,
    ) == ("error", None, "STAGED_MISMATCH")

    run_dir = tmp_path / "clean-vlan30"
    assert not (run_dir / "candidate").exists()
    assert not (run_dir / "change_package.md").exists()


@pytest.mark.slow
@pytest.mark.parametrize(
    "marker_text",
    [
        "",
        "{broken",
        "[]",
        "{}",
        '{"staged": true}',
        '{"staged": "2"}',
        '{"staged": -1}',
        '{"staged": 1}',
        '{"staged": 2, "extra": true}',
    ],
    ids=[
        "empty",
        "invalid_json",
        "wrong_root",
        "missing_count",
        "boolean_count",
        "string_count",
        "negative_count",
        "wrong_count",
        "extra_field",
    ],
)
def test_runner_rejects_invalid_finalize_marker(tmp_path: Path, monkeypatch, marker_text: str):
    from harness_agent.runner import pipeline

    real_run_stage = pipeline._run_stage
    marker_changed = False

    def run_stage_then_corrupt(cfg, stage, ctx, role, run_dir, prompt, trace, *rest):
        nonlocal marker_changed
        result = real_run_stage(cfg, stage, ctx, role, run_dir, prompt, trace, *rest)

        if stage == "tsa":
            marker = run_dir / "finalized.json"
            assert json.loads(marker.read_text(encoding="utf-8")) == {"staged": 2}

            marker.write_text(marker_text, encoding="utf-8")
            marker_changed = True

        return result

    monkeypatch.setattr(pipeline, "_run_stage", run_stage_then_corrupt)

    def forbid_render(*args, **kwargs):
        pytest.fail("Invalid finalize marker must stop before render")

    monkeypatch.setattr(pipeline, "render_by_device", forbid_render)

    result = _run_named_case("clean-vlan30", tmp_path, monkeypatch)

    assert marker_changed
    assert (
        result.status,
        result.verdict,
        result.error_code,
    ) == ("error", None, "HARNESS_STATE_INVALID")
    assert "finalized.json" in result.reason

    run_dir = tmp_path / "clean-vlan30"
    assert not (run_dir / "candidate").exists()
    assert not (run_dir / "change_package.md").exists()


@pytest.mark.slow
def test_runner_accepts_valid_finalize_marker(tmp_path: Path, monkeypatch):
    result = _run_named_case("clean-vlan30", tmp_path, monkeypatch)

    assert (result.status, result.verdict) == ("ok", "accept"), result.reason

    run_dir = tmp_path / "clean-vlan30"
    assert json.loads((run_dir / "finalized.json").read_text(encoding="utf-8")) == {"staged": 2}
    assert (run_dir / "candidate" / "SW2.cfg").is_file()
    assert (run_dir / "change_package.md").is_file()


@pytest.mark.slow
def test_prepare_stops_before_tsa(tmp_path: Path, monkeypatch):
    import hashlib

    from harness_agent.runner import pipeline

    case = json.loads((ROOT / "data/cases/clean-vlan30.json").read_text(encoding="utf-8"))
    monkeypatch.setenv(
        "FAKE_DSH_SCENARIO",
        str(ROOT / case["fake_scenario"]),
    )

    real_run_stage = pipeline._run_stage
    attempted = []

    def cba_only(cfg, stage, ctx, role, run_dir, prompt, trace, *rest):
        attempted.append(stage)
        if stage != "cba":
            pytest.fail("Prepare must not start TSA")
        return real_run_stage(cfg, stage, ctx, role, run_dir, prompt, trace, *rest)

    def forbid_render(*args, **kwargs):
        pytest.fail("Prepare must not render configuration")

    monkeypatch.setattr(pipeline, "_run_stage", cba_only)
    monkeypatch.setattr(pipeline, "render_by_device", forbid_render)

    result = pipeline.run_case(
        _fake_cfg(tmp_path),
        CAMPUS,
        case["intent"],
        case["role"],
        tmp_path / "runs",
        "prepare-test",
        prepare_only=True,
    )

    assert (
        result.status,
        result.verdict,
        result.error_code,
    ) == ("pending_review", None, None), result.reason
    assert result.stopped_by is None
    assert attempted == ["cba"]
    assert set(result.tool_calls) == {"cba"}

    run_dir = tmp_path / "runs" / "prepare-test"
    review = json.loads((run_dir / "review.json").read_text(encoding="utf-8"))
    assert review["state"] == "pending_review"
    assert review["intent"] == case["intent"]
    assert review["role"] == case["role"]
    assert review["context_sha256"] == hashlib.sha256(CAMPUS.read_bytes()).hexdigest()
    assert (
        review["proposal_sha256"]
        == hashlib.sha256((run_dir / "change_request.json").read_bytes()).hexdigest()
    )

    for name in (
        "staged_ops.jsonl",
        "finalized.json",
        "candidate",
        "change_package.md",
    ):
        assert not (run_dir / name).exists()


@pytest.mark.slow
def test_prepare_refuses_preselected_contract(tmp_path: Path):
    with pytest.raises(ValueError, match="prepare_only"):
        run_case(
            _fake_cfg(tmp_path),
            CAMPUS,
            "prepare request",
            "network_admin",
            tmp_path / "runs",
            "prepare-invalid",
            prepare_only=True,
            confirmed_intent_path=tmp_path / "confirmed.json",
        )

    assert not (tmp_path / "runs").exists()

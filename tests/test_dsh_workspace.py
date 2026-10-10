"""Thư mục làm việc của dsh: cố định, tên trung tính, ngoài repo, có khoá (D-025).

dsh gửi nguyên văn đường dẫn thư mục làm việc cho model. Khi nó là `runs/<run_id>/.dsh_cwd` (D-023) thì tên
case đi vào prompt. Các test ở đây chốt: đường dẫn không phụ thuộc case; thư mục rỗng trước mỗi stage; hai
lượt chạy không dùng chung cùng lúc (lượt thứ hai dừng ngay, không chờ, không đụng kết quả của lượt kia);
runner không bao giờ xoá thư mục nó không tạo ra.

`fake_dsh` báo `cwd` trong event `session` như dsh thật, nên test đọc được thư mục làm việc của nó.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from harness_agent.runner import dsh as dsh_module
from harness_agent.runner import pipeline, workspace
from harness_agent.runner.dsh import REPO_ROOT, DshSetupError, LocalConfig, run_dsh
from harness_agent.runner.pipeline import CaseResult, run_case

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
FAKE_DSH = ROOT / "scripts" / "fake_dsh.py"
CANARY = "CANARY-campus-small-01-c05e9a12"

VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
CLEAN = {
    "cba": [{"tool": "submit_change_request", "args": {"status": "ready", "changes": [VLAN]}}],
    "tsa": "auto",
}
# Một "lượt chạy khác": giữ khoá cho tới khi stdin đóng.
HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from harness_agent.runner import workspace\n"
    "with workspace.hold(Path(sys.argv[1])):\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.read()\n"
)
# Một "dsh" chỉ in ra những gì nó thấy trong thư mục làm việc của mình.
LIST_CWD = (
    'import json, os; print(json.dumps({"type": "final", "text": json.dumps(sorted(os.listdir(".")))}))'
)


def _cfg(tmp_path: Path, **overrides) -> LocalConfig:
    base = {
        "dsh_command": [sys.executable, str(FAKE_DSH)],
        "dsh_home": tmp_path / "unused_home",
        "base_patch": ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        "python_exe": Path(sys.executable),
        "requires_api_key": False,
    }
    return LocalConfig(**{**base, **overrides})


def _run(tmp_path: Path, monkeypatch, run_id: str = "case", **cfg) -> CaseResult:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(CLEAN), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    return run_case(_cfg(tmp_path, **cfg), CAMPUS, "intent thử", "network_admin", tmp_path / "runs", run_id)


def _events(run_dir: Path, stage: str) -> list[dict]:
    return [json.loads(x) for x in (run_dir / f"{stage}.ndjson").read_bytes().splitlines() if x.strip()]


def _session_cwd(run_dir: Path, stage: str) -> Path:
    first = _events(run_dir, stage)[0]
    assert first["type"] == "session"
    return Path(first["cwd"])


def _trace(run_dir: Path, event: str) -> list[dict]:
    records = [json.loads(x) for x in (run_dir / "trace.jsonl").read_bytes().splitlines() if x.strip()]
    return [r for r in records if r["event"] == event]


def _fixed(tmp_path: Path) -> Path:
    return tmp_path / "cha" / "workspace"


@contextlib.contextmanager
def _another_run_holding(ws: Path):
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", HOLDER, str(ws)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=ROOT
    )
    try:
        assert proc.stdout.readline().strip() == b"held"
        yield proc
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()


# ------------------------------------------------------------ vị trí
def test_default_workspace_has_a_neutral_name_outside_the_repo(tmp_path: Path, monkeypatch):
    ws = workspace.default_workspace()
    assert (ws.parent.name, ws.name) == ("cha", "workspace")
    assert workspace.check_location(ws, REPO_ROOT) == ws.resolve()

    monkeypatch.setenv("LOCALAPPDATA" if sys.platform == "win32" else "XDG_CACHE_HOME", str(tmp_path))
    assert workspace.default_workspace() == tmp_path / "cha" / "workspace"


@pytest.mark.parametrize(
    "bad",
    [
        REPO_ROOT,
        REPO_ROOT / "runs" / "clean-vlan30" / ".dsh_cwd",  # vị trí của D-023
        REPO_ROOT.parent,  # chứa repo: dọn nó là xoá repo
        Path.home(),
        Path(Path.home().anchor),
        Path("relative") / "dir",
    ],
    ids=["repo", "inside-repo", "contains-repo", "home", "drive-root", "relative"],
)
def test_unsafe_locations_are_rejected(bad: Path):
    with pytest.raises(workspace.WorkspaceError):
        workspace.check_location(bad, REPO_ROOT)


def _toml(tmp_path: Path, extra: str = "", requires_api_key: bool = False) -> Path:
    path = tmp_path / "cfg.toml"
    path.write_text(
        f'[dsh]\ncommand = ["python", "scripts/fake_dsh.py"]\n'
        f'base_patch = "tests/fixtures/dsh.base.patch.yml"\n'
        f'requires_api_key = {str(requires_api_key).lower()}\n{extra}\n[python]\nexe = "python"\n',
        encoding="utf-8",
    )
    return path


def test_config_uses_the_fixed_default_unless_told_otherwise(tmp_path: Path):
    assert LocalConfig.load(_toml(tmp_path)).dsh_workspace == workspace.default_workspace().resolve()
    assert LocalConfig.load(_toml(tmp_path, requires_api_key=True)).dsh_workspace is not None

    chosen = (tmp_path / "ws").as_posix()
    assert (
        LocalConfig.load(_toml(tmp_path, f'workspace = "{chosen}"')).dsh_workspace
        == (tmp_path / "ws").resolve()
    )

    assert LocalConfig.load(_toml(tmp_path, "temp_workspace = true")).dsh_workspace is None
    assert LocalConfig.load(ROOT / "config" / "fake.toml").dsh_workspace is None  # chạy khô: thư mục tạm


def test_config_errors(tmp_path: Path):
    inside = (REPO_ROOT / "runs" / "ws").as_posix()
    with pytest.raises(DshSetupError, match="ngoài repo"):
        LocalConfig.load(_toml(tmp_path, f'workspace = "{inside}"'))
    # Lượt chạy thật không được dùng thư mục tạm: đường dẫn sẽ khác nhau ở mỗi case.
    with pytest.raises(DshSetupError, match="temp_workspace"):
        LocalConfig.load(_toml(tmp_path, "temp_workspace = true", requires_api_key=True))
    outside = (tmp_path / "ws").as_posix()
    with pytest.raises(DshSetupError, match="một trong hai"):
        LocalConfig.load(_toml(tmp_path, f'temp_workspace = true\nworkspace = "{outside}"'))


# ------------------------------------------------------------ đường dẫn không phụ thuộc case
@pytest.mark.slow
def test_fixed_workspace_is_identical_for_every_case_and_never_names_the_case(tmp_path: Path, monkeypatch):
    ws = _fixed(tmp_path)
    seen: set[str] = set()
    for run_id in ("adv-protected-uplink", "clean-vlan30"):
        r = _run(tmp_path, monkeypatch, run_id=run_id, dsh_workspace=ws)
        assert (r.status, r.verdict) == ("ok", "accept"), r.reason
        run_dir = Path(r.run_dir)
        for stage in ("cba", "tsa"):
            cwd = _session_cwd(run_dir, stage)
            assert cwd.samefile(ws)
            assert run_id not in str(cwd) and run_dir not in cwd.parents
            seen.add(os.path.normcase(str(cwd)))
        assert _trace(run_dir, "case_start")[0]["data"]["dsh_workspace"] == str(ws)
        checks = [
            (c["data"]["stage"], c["data"]["entries"], c["data"]["emptied"])
            for c in _trace(run_dir, "dsh_cwd_checked")
        ]
        assert checks == [("cba", [], True), ("tsa", [], True)]
        assert list(ws.iterdir()) == []
    assert len(seen) == 1, "mọi case, mọi stage phải cho dsh cùng một đường dẫn"
    assert workspace.lock_file(ws).read_text(encoding="utf-8").startswith(workspace.LOCK_MAGIC)


@pytest.mark.slow
def test_harness_still_starts_in_both_stages(tmp_path: Path, monkeypatch):
    """Harness server lấy `cwd` từ patch (gốc repo), không phụ thuộc thư mục làm việc của dsh."""
    r = _run(tmp_path, monkeypatch, dsh_workspace=_fixed(tmp_path))
    starts = [s["actor"] for s in _trace(Path(r.run_dir), "server_start")]
    assert starts == ["harness:cba", "harness:tsa"]
    assert (Path(r.run_dir) / "change_package.md").exists()


@pytest.mark.slow
def test_negative_control_running_dsh_in_the_repo_root_is_visible(tmp_path: Path, monkeypatch):
    """Đối chứng âm: gọi dsh ở gốc repo thì event `session` báo gốc repo, tức phép đo ở các test trên thật
    sự nhìn thấy thư mục làm việc của dsh."""
    real_run = subprocess.run

    def run_in_repo_root(cmd, **kwargs):
        return real_run(cmd, **{**kwargs, "cwd": REPO_ROOT})

    monkeypatch.setattr(dsh_module.subprocess, "run", run_in_repo_root)
    r = _run(tmp_path, monkeypatch)

    assert _session_cwd(Path(r.run_dir), "cba").samefile(REPO_ROOT)


# ------------------------------------------------------------ rỗng trước mỗi stage
def _with_leftover(ws: Path) -> Path:
    with workspace.hold(ws):  # lượt chạy trước: tạo thư mục và file khoá
        pass
    (ws / "sub").mkdir()
    (ws / "sub" / "old.txt").write_text("của lượt trước", encoding="utf-8")
    (ws / "leftover.txt").write_text("của lượt trước", encoding="utf-8")
    return ws


@pytest.mark.slow
def test_workspace_is_emptied_before_dsh_starts(tmp_path: Path, monkeypatch):
    ws = _with_leftover(_fixed(tmp_path))
    r = _run(tmp_path, monkeypatch, dsh_workspace=ws, dsh_command=[sys.executable, "-c", LIST_CWD])
    assert _events(Path(r.run_dir), "cba")[-1] == {"type": "final", "text": "[]"}


@pytest.mark.slow
def test_negative_control_without_emptying_dsh_sees_the_leftovers(tmp_path: Path, monkeypatch):
    ws = _with_leftover(_fixed(tmp_path))
    monkeypatch.setattr(pipeline.workspace, "make_empty", lambda *_a, **_k: None)
    r = _run(tmp_path, monkeypatch, dsh_workspace=ws, dsh_command=[sys.executable, "-c", LIST_CWD])
    assert _events(Path(r.run_dir), "cba")[-1] == {"type": "final", "text": '["leftover.txt", "sub"]'}


def test_a_non_empty_directory_the_runner_did_not_create_is_never_cleared(tmp_path: Path, monkeypatch):
    """Config trỏ nhầm vào một thư mục có dữ liệu: runner từ chối, không xoá gì, kể cả run-dir cũ."""
    ws = tmp_path / "my-documents"
    ws.mkdir()
    (ws / "keep.txt").write_text("dữ liệu của người dùng", encoding="utf-8")
    old = tmp_path / "runs" / "case"
    old.mkdir(parents=True)
    (old / "result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DshSetupError, match="không rỗng và không do runner tạo"):
        _run(tmp_path, monkeypatch, dsh_workspace=ws)

    assert (ws / "keep.txt").read_text(encoding="utf-8") == "dữ liệu của người dùng"
    assert (old / "result.json").exists()
    assert not workspace.lock_file(ws).exists()


def test_make_empty_refuses_a_directory_this_run_has_not_locked(tmp_path: Path):
    target = tmp_path / "d"
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError, match="chưa được khoá"):
        workspace.make_empty(target)
    assert (target / "keep.txt").exists()


def _link_dir(link: Path, target: Path) -> str:
    """Tạo liên kết thư mục `link` → `target`: symlink, hoặc junction trên Windows khi không có quyền tạo
    symlink (junction không cần quyền gì). Trả về loại đã tạo."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except OSError:
        if sys.platform != "win32":
            pytest.skip("máy này không cho tạo symlink")
    made = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("máy này không cho tạo symlink lẫn junction")
    return "junction"


@pytest.mark.slow
def test_emptying_does_not_follow_links_out_of_the_workspace(tmp_path: Path):
    """Một liên kết trong workspace trỏ ra ngoài: dọn workspace gỡ liên kết, không xoá thứ nó trỏ tới."""
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    (outside / "sub" / "keep.txt").write_text("x", encoding="utf-8")
    ws = _fixed(tmp_path)
    with workspace.hold(ws):
        kind = _link_dir(ws / "link", outside)
        assert (ws / "link" / "keep.txt").exists(), f"{kind} phải trỏ tới thư mục bên ngoài"
        workspace.make_empty(ws)
        assert list(ws.iterdir()) == []
    assert (outside / "keep.txt").exists() and (outside / "sub" / "keep.txt").exists()


# ------------------------------------------------------------ khoá
@pytest.mark.slow
def test_second_run_stops_at_once_and_touches_nothing_while_another_run_holds_the_lock(
    tmp_path: Path, monkeypatch
):
    ws = _fixed(tmp_path)
    old = tmp_path / "runs" / "case"
    old.mkdir(parents=True)
    (old / "result.json").write_text('{"của": "lượt kia"}', encoding="utf-8")

    with _another_run_holding(ws):
        started = time.monotonic()
        with pytest.raises(DshSetupError, match="đang được một lượt chạy khác dùng") as caught:
            _run(tmp_path, monkeypatch, dsh_workspace=ws)
        assert time.monotonic() - started < 5, "phải báo lỗi ngay, không chờ khoá"
        assert "pid=" in str(caught.value)
        # Khoá được lấy trước khi runner dọn run-dir: kết quả của lượt đang chạy còn nguyên.
        assert (old / "result.json").read_text(encoding="utf-8") == '{"của": "lượt kia"}'
        assert sorted(p.name for p in old.iterdir()) == ["result.json"]

    r = _run(tmp_path, monkeypatch, dsh_workspace=ws)  # lượt kia xong: chạy được
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason


@pytest.mark.slow
def test_lock_is_released_when_the_holder_is_killed(tmp_path: Path):
    """Khoá của hệ điều hành: process giữ khoá chết thì khoá tự nhả; không có file khoá "mồ côi" phải
    xoá tay."""
    ws = _fixed(tmp_path)
    with _another_run_holding(ws) as proc:
        info = workspace.lock_file(ws).read_text(encoding="utf-8").splitlines()
        assert info[0] == workspace.LOCK_MAGIC and info[1].startswith("pid=")
        with pytest.raises(workspace.WorkspaceError), workspace.hold(ws):
            pass
        # pid trong file khoá là process Python thật (trong venv trên Windows, `proc` chỉ là launcher).
        os.kill(int(info[1].removeprefix("pid=")), signal.SIGTERM)
        proc.wait(timeout=30)
        deadline = time.monotonic() + 10
        while True:
            try:
                with workspace.hold(ws):
                    break
            except workspace.WorkspaceError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    assert workspace.lock_file(ws).exists()  # file khoá ở lại: nó là dấu "thư mục do runner tạo"


def test_hold_is_reentrant_within_one_process(tmp_path: Path):
    ws = _fixed(tmp_path)
    with workspace.hold(ws), workspace.hold(ws):
        workspace.make_empty(ws)
    with workspace.hold(ws):  # đã nhả hẳn: lấy lại được
        pass


@pytest.mark.slow
def test_run_cases_script_reports_a_held_workspace_as_a_config_error(tmp_path: Path):
    ws = _fixed(tmp_path)
    config = _toml(tmp_path, f'workspace = "{ws.as_posix()}"')
    out = tmp_path / "out"
    command = [
        sys.executable,
        "scripts/run_cases.py",
        "--config",
        str(config),
        "--out",
        str(out),
        "clean-vlan30",
    ]

    with _another_run_holding(ws):
        proc = subprocess.run(command, capture_output=True, cwd=ROOT, timeout=120, check=False)  # noqa: S603
    assert proc.returncode == 2, proc.stdout.decode("utf-8", "replace")
    assert "[LỖI CẤU HÌNH]" in proc.stderr.decode("utf-8") and "lượt chạy khác" in proc.stderr.decode("utf-8")
    assert not out.exists(), "không case nào được bắt đầu"

    proc = subprocess.run(command, capture_output=True, cwd=ROOT, timeout=300, check=False)  # noqa: S603
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "1/1 case đúng kỳ vọng" in proc.stdout.decode("utf-8")
    session = json.loads((out / "clean-vlan30" / "cba.ndjson").read_bytes().splitlines()[0])
    assert Path(session["cwd"]).samefile(ws)


# ------------------------------------------------------------ thư mục tạm (chạy khô, test)
@pytest.mark.slow
def test_without_a_fixed_workspace_each_case_gets_its_own_temporary_one(tmp_path: Path, monkeypatch):
    """Mặc định của `LocalConfig(...)` dựng trong test và của `config/fake.toml`: không khoá, nên nhiều
    worker của `pytest -n auto` chạy song song được."""
    cwds = []
    for run_id in ("a-case", "b-case"):
        r = _run(tmp_path, monkeypatch, run_id=run_id)
        assert (r.status, r.verdict) == ("ok", "accept"), r.reason
        cwds.append(_session_cwd(Path(r.run_dir), "cba"))
    assert cwds[0] != cwds[1]
    for cwd in cwds:
        assert cwd.name.startswith(workspace.TEMP_PREFIX)
        assert not cwd.exists(), "thư mục tạm được xoá khi case xong"
        assert not workspace.lock_file(cwd).exists()
        assert REPO_ROOT not in cwd.parents and tmp_path not in cwd.parents


# ------------------------------------------------------------ dsh để lại file
@pytest.mark.slow
def test_a_file_dropped_by_dsh_is_recorded_scanned_for_secrets_and_removed(tmp_path: Path, monkeypatch):
    """Một "dsh" ghi file bằng đường dẫn tương đối: file nằm trong thư mục làm việc (không phải gốc repo),
    trace ghi lại, secret trong đó hiện ở `leak_sources`, và file bị dọn trước stage sau."""
    ws = _fixed(tmp_path)
    name = f"dropped-{uuid.uuid4().hex}.txt"
    code = (
        f"import os; open({name!r}, 'w').write('key {CANARY}'); "
        'print(\'{"type": "session", "sessionId": "s"}\'); print(\'{"type": "final", "text": ""}\')'
    )
    try:
        r = _run(tmp_path, monkeypatch, dsh_workspace=ws, dsh_command=[sys.executable, "-c", code])
        run_dir = Path(r.run_dir)

        assert r.status == "error"  # "dsh" này không khởi động harness: không bao giờ thành accept
        assert not (REPO_ROOT / name).exists()
        check = _trace(run_dir, "dsh_cwd_checked")[0]["data"]
        assert (check["entries"], check["emptied"]) == ([name], True)
        in_cwd = [s for s in r.leak_sources if s["source"] == "dsh_cwd"]
        assert [(s["stage"], s["file"]) for s in in_cwd] == [("cba", name)]
        # "dsh" này không ghi gì vào DSH_HOME: mục còn lại nói rõ phép quét ở đó không đủ (commit
        # LEAK_SCAN_INCOMPLETE). Không mục nào bị bỏ qua im lặng.
        rest = [s for s in r.leak_sources if s["source"] != "dsh_cwd"]
        assert rest == [{"source": "dsh_home", "stage": "cba", "status": "empty"}]
        assert r.leak_scan_complete is False
        assert r.leak_attempt_detected is True and r.leak_in_package is False
        assert list(ws.iterdir()) == []
        assert CANARY not in (run_dir / "result.json").read_text(encoding="utf-8")
    finally:
        (REPO_ROOT / name).unlink(missing_ok=True)  # chỉ có khi test đỏ: không để rác lại trong repo


@pytest.mark.slow
def test_negative_control_a_dropped_file_without_a_secret_is_recorded_but_is_not_a_leak(
    tmp_path: Path, monkeypatch
):
    """Đối chứng âm cho test trên: cùng "dsh", cùng file, nội dung KHÔNG có secret. File vẫn được ghi nhận
    và dọn, nhưng không có mục `dsh_cwd` nào chỉ tới nó: test trên đỏ là vì secret, không phải vì có file."""
    ws = _fixed(tmp_path)
    name = f"dropped-{uuid.uuid4().hex}.txt"
    code = (
        f"import os; open({name!r}, 'w').write('key khong-phai-secret'); "
        'print(\'{"type": "session", "sessionId": "s"}\'); print(\'{"type": "final", "text": ""}\')'
    )
    try:
        r = _run(tmp_path, monkeypatch, dsh_workspace=ws, dsh_command=[sys.executable, "-c", code])

        check = _trace(Path(r.run_dir), "dsh_cwd_checked")[0]["data"]
        assert (check["entries"], check["emptied"], check["files_scanned"]) == ([name], True, 1)
        assert [s for s in r.leak_sources if s["source"] == "dsh_cwd"] == []
        assert r.leak_attempt_detected is False
        assert list(ws.iterdir()) == []
    finally:
        (REPO_ROOT / name).unlink(missing_ok=True)


def test_run_dsh_refuses_to_run_without_a_working_directory(tmp_path: Path, monkeypatch):
    """Không có mặc định ngầm: quên đặt `dsh_cwd` là lỗi cấu hình, dsh không được khởi động."""
    called = []
    monkeypatch.setattr(dsh_module.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(DshSetupError, match="dsh_cwd"):
        run_dsh(_cfg(tmp_path), tmp_path / "cba.patch.yml", "việc", tmp_path / "cba")
    assert called == []


def test_names_carrying_a_context_secret_are_redacted_in_the_trace(tmp_path: Path):
    workdir = tmp_path / "cwd"
    (workdir / "sub").mkdir(parents=True)
    (workdir / "sub" / "note.txt").write_text("x", encoding="utf-8")
    (workdir / "canary-SECRET-1.txt").write_text("x", encoding="utf-8")

    assert pipeline._entries(workdir, {"SECRET-1"}) == ["[REDACTED]", "sub", "sub/note.txt"]


# ------------------------------------------------------------ đường dẫn tương đối trong [dsh] command
def test_relative_script_in_the_command_is_resolved_against_the_repo_root():
    """`config/fake.toml` ghi `scripts/fake_dsh.py`. dsh không chạy ở gốc repo, nên đường dẫn này phải
    thành tuyệt đối khi đọc config; để nguyên thì nó được tìm trong thư mục làm việc rỗng."""
    cfg = LocalConfig.load(ROOT / "config" / "fake.toml")
    assert Path(cfg.dsh_command[1]).is_absolute()
    assert Path(cfg.dsh_command[1]).samefile(FAKE_DSH)


def test_only_existing_repo_files_are_rewritten():
    resolve = dsh_module._resolve_command
    absolute = str(FAKE_DSH)
    assert resolve([sys.executable, "--json", "-", "no/such/file.js", absolute]) == [
        sys.executable,
        "--json",
        "-",
        "no/such/file.js",
        absolute,
    ]

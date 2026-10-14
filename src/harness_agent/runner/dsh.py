"""Gọi DeepSeek Harness (dsh) như một process KHÔNG tin cậy.

- Cấu hình cục bộ (đường dẫn node, dsh, python) đọc từ `config/local.toml` — file tin cậy, không commit.
- Patch cho từng stage được SINH từ `config/dsh.base.patch.yml` (bản đã kiểm chứng ở spike 1.1):
  chỉ thay khối MCP client trỏ tới harness. Không dùng `!!js` (F7).
- Output của dsh được ghi nguyên byte (UTF-8) ra file; không qua console/PowerShell (F8). Ngoại lệ duy
  nhất: secret của context được phát hiện trong bộ nhớ rồi che trước khi ghi (D-018).

Trạng thái: ĐỀ XUẤT — task 1.6 (TV1) hoàn thiện.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from harness_agent.harness.leak import redact_stream

from .model import MODEL_ROW_ID, ModelConfig, ModelConfigError, load_model_config
from .workspace import WorkspaceError, check_location, default_workspace

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_CLIENT_PLUGIN = "@deepseek-ai/dsh-mcp-client"
SERVER_NAME = "harness"

# Các row phải bị tắt trong base patch (spike 1.1: F3–F6, Q2). Thiếu → dừng, không chạy.
REQUIRED_DISABLED_ROWS = (
    "tool-bash",
    "tool-pwsh",
    "tool-fs",
    "tool-fs-search",
    "tool-web",
    "tool-jobs",
    "tool-skill",
    "tool-goal",
    "tool-todo",
    "tool-subagent",
    "tool-subagent-fork",
    "tool-subagent-control",
    "tool-subagent-list-agents",
    "tool-workflow",
    "plan-mode",
    "mcp-resources",
    "session-log-deepseek",
    "agent-instructions",
    "plugin-package-inventory-deepseek",
    "session-title-llm",
)


class DshSetupError(Exception):
    """Lỗi cấu hình phía TIN CẬY (config, patch). Phải sửa trước khi chạy."""


@dataclass
class LocalConfig:
    dsh_command: list[str]
    dsh_home: Path
    base_patch: Path
    python_exe: Path
    prompt_via: str = "stdin"
    extra_args: list[str] = field(default_factory=list)
    timeout_s: int = 300
    requires_api_key: bool = True
    # Giữ session log của dsh trong `<run-dir>/.dsh_home` sau khi runner quét (mặc định: xoá, D-019).
    keep_dsh_home: bool = False
    # File khai model cho mọi lượt chạy (D-020). Nằm trong repo, không phải cấu hình của từng máy: không
    # có khoá tương ứng trong config/local.toml.
    model_file: Path = REPO_ROOT / "config" / "model.toml"
    # Thư mục làm việc CỐ ĐỊNH của dsh (D-025): tên trung tính, ngoài repo, giống nhau ở mọi case, vì dsh
    # gửi đường dẫn này cho model. `LocalConfig.load` đặt nó từ `[dsh] workspace` hoặc lấy mặc định
    # (`workspace.default_workspace()`). None = mỗi case một thư mục tạm riêng, không khoá: chỉ dành cho
    # chạy khô và test (`[dsh] temp_workspace = true`).
    dsh_workspace: Path | None = None
    # Thư mục làm việc (cwd) của process dsh trong lượt chạy hiện tại. `run_case` đặt nó; không có mặc
    # định: `run_dsh` từ chối chạy khi chưa đặt, để dsh không bao giờ lấy gốc repo làm workspace.
    dsh_cwd: Path | None = None
    level: str = "B4"
    # Runner-only immutable snapshot; never loaded from local.toml or runtime output.
    model_snapshot: ModelConfig | None = field(default=None, repr=False)

    def model(self) -> ModelConfig:
        """Model đã ghim. Thiếu hay sai `config/model.toml` là lỗi cấu hình, không có mặc định ngầm."""
        if self.model_snapshot is not None:
            return self.model_snapshot
        try:
            return load_model_config(self.model_file)
        except ModelConfigError as exc:
            raise DshSetupError(str(exc)) from exc

    @classmethod
    def load(cls, path: str | Path) -> LocalConfig:
        p = _abs(Path(path))
        if not p.exists():
            raise DshSetupError(
                f"không thấy {p}. Sao chép config/local.example.toml thành config/local.toml rồi sửa."
            )
        try:
            # utf-8-sig: PowerShell 5.1 (`Set-Content -Encoding UTF8`, `Out-File`) và Notepad ghi UTF-8 kèm
            # BOM; tomllib coi BOM là ký tự lạ ở dòng 1, cột 1.
            data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise DshSetupError(
                f"{p} không phải UTF-8 (PowerShell `>` ghi UTF-16). Lưu lại file dạng UTF-8."
            ) from exc
        except tomllib.TOMLDecodeError as exc:
            raise DshSetupError(f"{p} không phải TOML hợp lệ: {exc}") from exc
        d, py = data.get("dsh", {}), data.get("python", {})
        try:
            cfg = cls(
                dsh_command=_resolve_command([str(x) for x in d["command"]]),
                # `home` chỉ còn dùng khi gọi `run_dsh` trực tiếp: `run_case` đặt DSH_HOME riêng cho
                # từng case ở `<run-dir>/.dsh_home` (D-019). Khoá này không còn bắt buộc.
                dsh_home=_abs(Path(d.get("home", "runs/.dsh_home"))),
                base_patch=_abs(Path(d.get("base_patch", "config/dsh.base.patch.yml"))),
                python_exe=_resolve_exe(str(py["exe"])),
                prompt_via=d.get("prompt_via", "stdin"),
                extra_args=[str(x) for x in d.get("extra_args", [])],
                timeout_s=int(d.get("timeout_s", 300)),
                requires_api_key=bool(d.get("requires_api_key", True)),
                keep_dsh_home=bool(d.get("keep_home", False)),
            )
        except KeyError as exc:
            raise DshSetupError(f"config/local.toml thiếu khoá {exc}") from exc
        if cfg.prompt_via not in ("stdin", "arg"):
            raise DshSetupError("prompt_via phải là 'stdin' hoặc 'arg'")
        cfg.dsh_workspace = _workspace_from_config(d, cfg.requires_api_key)
        return cfg


def _workspace_from_config(d: dict[str, Any], requires_api_key: bool) -> Path | None:
    """Thư mục làm việc của dsh theo config (D-025); None = thư mục tạm riêng cho từng case.

    - `[dsh] workspace = "<đường dẫn tuyệt đối>"`: dùng thư mục đó; không khai thì dùng mặc định của máy.
    - `[dsh] temp_workspace = true`: thư mục tạm, chỉ cho chạy khô. Với dsh thật thì bị từ chối: đường dẫn
      tạm khác nhau ở mỗi case, nên prompt model nhận sẽ khác nhau giữa các case.
    """
    if d.get("temp_workspace", False):
        if "workspace" in d:
            raise DshSetupError("config: chỉ khai một trong hai khoá `workspace` và `temp_workspace`")
        if requires_api_key:
            raise DshSetupError(
                "config: `temp_workspace = true` chỉ dùng cho chạy khô (`requires_api_key = false`); "
                "lượt chạy thật phải dùng thư mục làm việc cố định (D-025)"
            )
        return None
    raw = d.get("workspace")
    chosen = Path(os.path.expandvars(str(raw))).expanduser() if raw is not None else default_workspace()
    try:
        return check_location(chosen, REPO_ROOT)
    except WorkspaceError as exc:
        raise DshSetupError(f"config `[dsh] workspace`: {exc}") from exc


@dataclass
class DshResult:
    exit_code: int
    events: list[dict[str, Any]]
    tool_calls: list[str]
    final_text: str
    stdout_path: Path
    stderr_path: Path
    parse_errors: int
    # Vị trí secret của context trong stdout/stderr, ghi nhận TRƯỚC khi che (không có giá trị).
    # Mọi trường khác ở trên được dựng từ output ĐÃ che.
    leaks: list[dict[str, Any]] = field(default_factory=list)


class DshTimeout(Exception):
    """dsh chạy quá `timeout_s` và đã bị dừng. Phần output đã có được che secret rồi lưu;
    `leaks` là vị trí secret tìm thấy trong phần đó."""

    def __init__(self, timeout_s: int, leaks: list[dict[str, Any]]):
        super().__init__(f"dsh chạy quá {timeout_s}s")
        self.timeout_s = timeout_s
        self.leaks = leaks


def build_patch(cfg: LocalConfig, stage: str, context: Path, role: str, run_dir: Path) -> Path:
    try:
        data = yaml.safe_load(cfg.base_patch.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DshSetupError(
            f"không thấy base patch {cfg.base_patch} (xem docs/SETUP_WINDOWS.md bước 5)"
        ) from exc
    except yaml.YAMLError as exc:
        raise DshSetupError(f"base patch không đọc được (có tag !!js?): {exc}") from exc

    nodes = list(_walk(data))
    mcp_nodes = [n for n in nodes if n.get("name") == MCP_CLIENT_PLUGIN]
    if len(mcp_nodes) != 1:
        raise DshSetupError(f"base patch phải có đúng 1 khối {MCP_CLIENT_PLUGIN}, đang có {len(mcp_nodes)}")
    missing = [rid for rid in REQUIRED_DISABLED_ROWS if not any(_is_disabled(n, rid) for n in nodes)]
    if missing:
        raise DshSetupError(f"base patch chưa tắt các row: {', '.join(missing)}")

    conf = mcp_nodes[0].setdefault("config", {})
    conf.update(
        transport="stdio",
        serverName=SERVER_NAME,
        command=str(cfg.python_exe),
        args=[
            "-m",
            "harness_agent.harness.server",
            "--stage",
            stage,
            "--context",
            str(context),
            "--role",
            role,
            "--run-dir",
            str(run_dir),
            "--level",
            cfg.level,
        ],
        cwd=str(REPO_ROOT),
        failOnStartupError=True,
    )
    if stage == "b0":
        mcp_nodes[0]["disabled"] = True
        mcp_nodes[0]["config"] = {}
    # Ghim model (D-020): model chỉ được khai ở config/model.toml, và có mặt trong patch của MỌI stage.
    if not isinstance(data, list):
        raise DshSetupError("base patch phải là một danh sách row ở cấp gốc")
    if any(n.get("id") == MODEL_ROW_ID for n in nodes):
        raise DshSetupError(
            f"base patch không được khai row {MODEL_ROW_ID}: model chỉ khai ở config/model.toml (D-020)"
        )
    data.append(cfg.model().patch_row())
    out = run_dir / f"{stage}.patch.yml"
    out.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n")
    return out


def preflight(cfg: LocalConfig, stage: str, context: Path, role: str, run_dir: Path) -> list[str]:
    """Khởi động thử harness server bằng CHÍNH python mà dsh sẽ dùng; trả về danh sách tool."""
    cmd = [
        str(cfg.python_exe),
        "-m",
        "harness_agent.harness.server",
        "--stage",
        stage,
        "--context",
        str(context),
        "--role",
        role,
        "--run-dir",
        str(run_dir),
        "--level",
        cfg.level,
        "--selftest",
    ]
    proc = subprocess.run(  # noqa: S603
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=REPO_ROOT,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise DshSetupError(f"preflight {stage} thất bại: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return json.loads(proc.stdout.decode("utf-8"))["tools"]


def run_dsh(
    cfg: LocalConfig, patch: Path, prompt: str, out_prefix: Path, secrets: Collection[str] = ()
) -> DshResult:
    """Chạy dsh cho một stage. `secrets` là các giá trị bí mật của context: output thô được quét rồi
    che trước khi lưu, và kết quả trả về chỉ dựng từ bản đã che. Raise `DshTimeout` nếu quá giờ."""
    if cfg.requires_api_key and not os.environ.get("DEEPSEEK_API_KEY"):
        raise DshSetupError(
            "chưa có DEEPSEEK_API_KEY trong terminal này (nhập bằng Read-Host -AsSecureString)"
        )
    # dsh coi thư mục nó được gọi là workspace: báo đường dẫn đó cho model và tìm `.env` ở đó (spike 1.1,
    # Q4). Vì vậy nó không chạy ở gốc repo mà ở một thư mục do runner chỉ định (D-023, D-025).
    if cfg.dsh_cwd is None:
        raise DshSetupError("chưa đặt thư mục làm việc cho dsh (`dsh_cwd`); `run_case` tự đặt cho từng case")
    cfg.dsh_cwd.mkdir(parents=True, exist_ok=True)
    via_stdin = cfg.prompt_via == "stdin"
    cmd = [*cfg.dsh_command, *dsh_arguments(patch, cfg.extra_args, None if via_stdin else prompt)]
    stdin_bytes = prompt.encode("utf-8") if via_stdin else None
    env = os.environ.copy()
    env["DSH_HOME"] = str(cfg.dsh_home)
    env["DSH_TELEMETRY_DISABLED"] = "1"
    env["DSH_TELEMETRY_MODE"] = "DISABLED"
    cfg.dsh_home.mkdir(parents=True, exist_ok=True)
    out_path, err_path = out_prefix.with_suffix(".ndjson"), out_prefix.with_suffix(".stderr.txt")
    try:
        proc = subprocess.run(  # noqa: S603 — lệnh lấy từ config cục bộ tin cậy, không từ input
            cmd,
            input=stdin_bytes,
            capture_output=True,
            cwd=cfg.dsh_cwd,
            env=env,
            timeout=cfg.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Giữ phần output đã có làm bằng chứng rồi để runner gán mã TIMEOUT.
        _, leaks = _save_streams(out_path, err_path, exc.stdout or b"", exc.stderr or b"", secrets)
        raise DshTimeout(cfg.timeout_s, leaks) from exc
    except OSError as exc:
        raise DshSetupError(f"không chạy được dsh ({cmd[0]}): {exc}") from exc
    stdout, leaks = _save_streams(out_path, err_path, proc.stdout, proc.stderr, secrets)

    # Từ đây chỉ dùng `stdout` đã che: tên tool, text cuối... mà runner ghi tiếp vào trace và
    # result.json đều không còn secret.
    events: list[dict[str, Any]] = []
    bad = 0
    for line in stdout.decode("utf-8-sig", "replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            bad += 1  # dòng JSON nhưng không phải object: không phải event của dsh
    calls = [e.get("tool", "") for e in events if e.get("type") == "tool_call"]
    final = next((e.get("text", "") for e in reversed(events) if e.get("type") == "final"), "")
    if cfg.level == "B0":
        # B0 output itself is the research candidate. Do not silently sanitize the measured artifact.
        raw_events = []
        for line in proc.stdout.decode("utf-8-sig", "replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                raw_events.append(event)
        final = next((e.get("text", "") for e in reversed(raw_events) if e.get("type") == "final"), "")
    return DshResult(proc.returncode, events, calls, final, out_path, err_path, bad, leaks)


def _save_streams(
    out_path: Path, err_path: Path, stdout: bytes, stderr: bytes, secrets: Collection[str]
) -> tuple[bytes, list[dict[str, Any]]]:
    """Phát hiện trước, che sau (D-018). Secret được tìm trong output thô khi còn trong bộ nhớ và vị trí
    được ghi nhận; sau đó mới che và ghi file. Nhờ vậy secret không xuống đĩa, mà việc phát hiện cũng
    không phải dựa vào file đã che. Trả về (stdout đã che, vị trí secret ở cả hai luồng)."""
    leaks: list[dict[str, Any]] = []
    clean: dict[str, bytes] = {}
    for stream, path, raw in (("stdout", out_path, stdout), ("stderr", err_path, stderr)):
        clean[stream], found = redact_stream(raw, secrets)
        leaks += [{"stream": stream, **where} for where in found]
        path.write_bytes(clean[stream])
    return clean["stdout"], leaks


def dsh_arguments(patch: Path, extra_args: list[str], task: str | None) -> list[str]:
    """Tham số của dsh (phần sau `<node> <lib/bin.js>`), theo đúng thứ tự mà dsh 0.2.0-rc.1 đòi hỏi.

    dsh có HAI tầng tham số (nguồn: `dsh --help`, `dsh --profile headless --help`, README của gói):

      dsh [--profile] <name> [cờ của launcher]  [tham số của app]
          --profile headless  --patch <file>     --json  <extra_args>  <task>

    - Cờ của launcher: `--profile`, `--patch` (lặp được), `--from-default-profile`, `--dump-config`...
    - Tham số của app headless: `--json`, `--session-id <id>`, rồi task (`-` = đọc task từ stdin).

    Launcher dừng đọc cờ của mình ở token ĐẦU TIÊN nó không nhận ra; mọi thứ sau đó thuộc về app. Vì vậy
    `--patch` phải đứng TRƯỚC `--json`. Đặt sau thì patch không được áp, app báo
    `{"type":"error","message":"unknown option '--patch'"}` và thoát mã 1: harness không hề được khởi động.

    `extra_args` nằm sau `--json`, tức là tham số của app headless. `task=None` nghĩa là đọc từ stdin.
    """
    return [
        "--profile",
        "headless",
        "--patch",
        str(patch),
        "--json",
        *extra_args,
        "-" if task is None else task,
    ]


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_disabled(node: dict[str, Any], row_id: str) -> bool:
    if node.get("id") != row_id:
        return False
    conf = node.get("config") if isinstance(node.get("config"), dict) else {}
    return node.get("disabled") is True or node.get("enabled") is False or conf.get("enabled") is False


def _abs(p: Path) -> Path:
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


# Tên trần của trình thông dịch Python trong config (so khớp không phân biệt hoa thường).
_PYTHON_NAMES = frozenset({"python", "python3", "python.exe", "python3.exe"})


def _resolve_exe(value: str) -> Path:
    """Tên trần → đường dẫn tuyệt đối; còn lại coi là đường dẫn.

    "python" trần → `sys.executable`, tức chính Python đang chạy runner (python của venv). KHÔNG tìm
    qua PATH: chạy `.venv\\Scripts\\python.exe` mà không Activate thì PATH trỏ tới Python gốc, thiếu
    package của repo; còn đưa tên trần thẳng cho subprocess thì trên Windows nó tìm trong thư mục
    của Python gốc trước cả PATH (venv python.exe chỉ là launcher).
    Tên trần khác (vd `npx`) → tìm qua PATH.
    """
    if "/" not in value and "\\" not in value:
        if value.lower() in _PYTHON_NAMES:
            return Path(sys.executable)
        found = shutil.which(value)
        if not found:
            raise DshSetupError(f"không tìm thấy '{value}' trong PATH")
        return Path(found)
    p = _abs(Path(value))
    if not p.exists():
        raise DshSetupError(f"python.exe không tồn tại: {p}")
    return p


def _resolve_command(cmd: list[str]) -> list[str]:
    """Chương trình đầu của dsh.command là tên trần → đường dẫn tuyệt đối (xem `_resolve_exe`);
    đường dẫn giữ nguyên. Không bao giờ đưa tên trần thẳng cho subprocess.

    Tham số còn lại là đường dẫn tương đối tới một file có trong repo (vd `scripts/fake_dsh.py`) → đường
    dẫn tuyệt đối tính từ gốc repo, như mọi đường dẫn khác trong config. dsh không chạy ở gốc repo (D-023),
    nên để nguyên thì nó được tìm trong thư mục làm việc rỗng của case.
    """
    if not cmd:
        raise DshSetupError("dsh.command rỗng")
    head = cmd[0]
    if "/" not in head and "\\" not in head:
        head = str(_resolve_exe(head))
    rest = [str(REPO_ROOT / arg) if _is_repo_file(arg) else arg for arg in cmd[1:]]
    return [head, *rest]


def _is_repo_file(arg: str) -> bool:
    return not arg.startswith("-") and not Path(arg).is_absolute() and (REPO_ROOT / arg).is_file()

"""Lõi của giao diện vận hành (D-057). Không có HTTP ở đây; `web.py` gọi vào lớp `UiApp`.

Ranh giới tin cậy:
- Role, mức (B3/B4), file context, cấu hình và tên người duyệt đến từ tham số khởi động server, KHÔNG từ form.
- Intent từ form là dữ liệu không tin cậy: 1–2000 ký tự, không ký tự điều khiển, không chứa secret của
  context;
  chỉ đi vào `argv` dạng danh sách của process con, không qua shell.
- Mỗi lượt chạy là một process con `python -m harness_agent.runner`, một lượt một lúc; `run_id` do server
  sinh và
  không bao giờ dùng lại (runner xoá run-dir trùng tên trước khi chạy).
- `evaluated_at` = đồng hồ máy lúc bấm chạy. Đây là quyết định của UI (D-057), không phải của runner
  (D-054: runner
  không đọc đồng hồ); giá trị đi vào runner qua tham số và nằm trong `case_start` của trace.
- Chỉ đọc file của run-dir theo allowlist; mọi chuỗi đọc ra được che secret trước khi tới lớp hiển thị.
- Bản duyệt nối tiếp chuỗi hash của `trace.jsonl` (actor `reviewer`); xuất `.cfg` chỉ sau khi đã duyệt.
  Không deploy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from harness_agent.harness.leak import redact_value
from harness_agent.harness.roles import role_level
from harness_agent.harness.trace import Trace, verify_chain
from harness_agent.levels import Level, spec_of
from harness_agent.models import NetworkContext, Violation
from harness_agent.runner.cases import CaseError, case_files, load_case, scenario_path
from harness_agent.runner.context_binding import freshness
from harness_agent.runner.context_check import context_violations_from_bytes
from harness_agent.runner.dsh import REPO_ROOT, DshSetupError, LocalConfig

RUN_ID_RE = re.compile(r"^ui-\d{8}t\d{6}z-[0-9a-f]{6}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCENARIO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Ký tự điều khiển, trừ tab và xuống dòng (intent và lý do được viết nhiều dòng).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_INTENT = 2000  # cùng giới hạn của contract TestCase
MAX_REASON = 500
UI_DIR = (
    "_ui"  # thư mục riêng của UI trong thư mục kết quả; không khớp RUN_ID_RE nên không bao giờ là run-dir
)
# Snapshot còn ít hơn phần này của TTL thì cảnh báo "sắp hết hạn" (vàng).
EXPIRING_SHARE = 0.2
DECISIONS = ("approve", "reject")
UI_LEVELS = (Level.B3, Level.B4)
_BOM = b"\xef\xbb\xbf"


# Trạng thái chuỗi cho phép ghi bản duyệt. UI không giữ khoá (D-060) nên thực tế chỉ thấy "unsigned" (liền
# mạch) hoặc "broken"; "bad_signature" và "missing" (không có/không đọc được trace) luôn chặn.
CHAIN_INTACT = ("ok", "unsigned")


def _chain_status(path: Path) -> str:
    """`verify_chain` (không khoá) của trace; "missing" khi file không có hoặc không đọc được."""
    try:
        return verify_chain(path)
    except OSError:
        return "missing"


def _chain_intact(path: Path) -> bool:
    """Chuỗi hash liền mạch ("ok"/"unsigned" của `verify_chain`); "broken"/"bad_signature" hay không đọc được
    là False."""
    return _chain_status(path) in CHAIN_INTACT


class UiError(Exception):
    """Lỗi hiển thị được cho người dùng; `status` là mã HTTP."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Proc(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


# (argv, env, log_path) -> process đang chạy. Test thay bằng hàm giả; mặc định là subprocess.Popen.
Launcher = Callable[[list[str], dict[str, str], Path], Proc]


def _popen(argv: list[str], env: dict[str, str], log_path: Path) -> Proc:
    log = log_path.open("wb")
    try:
        return subprocess.Popen(  # noqa: S603 — argv là danh sách, không qua shell
            argv, cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
        )
    finally:
        log.close()  # process con giữ handle riêng


@dataclass(frozen=True)
class UiSettings:
    config: Path
    context: Path
    role: str
    level: Level
    reviewer: str
    out: Path


@dataclass(frozen=True)
class Scenario:
    name: str
    intent: str
    role: str
    path: Path
    same_context: bool


def check_text(value: str, *, what: str, max_len: int, allow_empty: bool = False) -> str:
    """Chuỗi từ form: độ dài, không ký tự điều khiển. Raise `UiError(400)`."""
    if not isinstance(value, str):
        raise UiError(400, f"{what} không hợp lệ")
    if not allow_empty and not value.strip():
        raise UiError(400, f"{what} không được để trống")
    if len(value) > max_len:
        raise UiError(400, f"{what} dài quá {max_len} ký tự")
    if _CONTROL.search(value):
        raise UiError(400, f"{what} chứa ký tự điều khiển")
    return value


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class UiApp:
    """Trạng thái của một server UI: một context, một role, một mức, một người duyệt."""

    def __init__(
        self,
        settings: UiSettings,
        *,
        clock: Callable[[], datetime] | None = None,
        launcher: Launcher | None = None,
        python: str | None = None,
    ):
        self.settings = settings
        try:
            role_level(settings.role)
        except ValueError:
            raise UiError(400, f"role không xác định: {settings.role}") from None
        if settings.level not in UI_LEVELS:
            raise UiError(400, "giao diện vận hành chỉ chạy ở B3 hoặc B4")
        self.reviewer = check_text(settings.reviewer, what="tên người duyệt", max_len=64).strip()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.launcher = launcher or _popen
        self.python = python or sys.executable
        try:
            self.cfg = LocalConfig.load(settings.config)
        except DshSetupError as exc:
            raise UiError(400, f"cấu hình không dùng được: {exc}") from None
        self.fake = not self.cfg.requires_api_key
        self.context_path = settings.context.resolve(strict=True)
        raw = self.context_path.read_bytes()
        self.context_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            self.context = NetworkContext.model_validate_json(raw.removeprefix(_BOM))
        except ValidationError:
            raise UiError(400, "file context không đúng contract; sửa file rồi khởi động lại") from None
        self.violations: list[Violation] = context_violations_from_bytes(raw)
        self.secrets: set[str] = self.context.secret_values()
        self.out = settings.out.resolve()
        (self.out / UI_DIR).mkdir(parents=True, exist_ok=True)
        self.index_path = self.out / UI_DIR / "runs.jsonl"
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.active: str | None = None
        self.scenarios = self._load_scenarios() if self.fake else {}

    # ------------------------------------------------------------------ context
    def freshness_view(self) -> dict[str, Any]:
        """Nguồn và tuổi của snapshot so với đồng hồ máy lúc này (chỉ UI đọc đồng hồ, D-057)."""
        p = self.context.provenance
        now = self.clock()
        if p is None or p.collected_at is None or p.ttl_seconds is None:
            source = "synthetic" if p is None else p.source.value
            return {"status": "no_ttl", "source": source, "declared": p is not None}
        status, violations = freshness(p, now)
        age = (now - p.collected_at).total_seconds()
        remaining = p.ttl_seconds - age
        if status == "fresh" and remaining <= p.ttl_seconds * EXPIRING_SHARE:
            status = "expiring"
        return {
            "status": status,
            "source": p.source.value,
            "declared": True,
            "collected_at": utc_text(p.collected_at),
            "ttl_seconds": p.ttl_seconds,
            "age_seconds": int(age),
            "remaining_seconds": int(remaining),
            "detail": violations[0].detail if violations else "",
        }

    def context_changed(self) -> bool:
        try:
            return hashlib.sha256(self.context_path.read_bytes()).hexdigest() != self.context_sha256
        except OSError:
            return True

    def run_blocker(self) -> str | None:
        """Vì sao lúc này không chạy được; None nếu chạy được."""
        if self.context_changed():
            return "File context đã đổi kể từ lúc khởi động server. Khởi động lại server để dùng bản mới."
        if self.freshness_view()["status"] == "stale":
            return "Snapshot context đã quá hạn (CTX_STALE). Thu thập lại context rồi khởi động lại server."
        if not self.fake and not os.environ.get("DEEPSEEK_API_KEY"):
            # Runner cũng kiểm điều này, nhưng khi đó process đã sinh và trang chỉ hiện "thoát mà không
            # ghi result.json". Chặn sớm với lý do rõ. Key chỉ đọc từ env, không bao giờ hiển thị.
            return (
                "Chế độ thật nhưng terminal chạy server chưa có DEEPSEEK_API_KEY. Dừng server, nhập key bằng "
                "Read-Host -AsSecureString (docs/SETUP_WINDOWS.md) rồi khởi động lại."
            )
        if self.active is not None:
            return f"Lượt {self.active} đang chạy. Mỗi lúc chỉ chạy một lượt."
        return None

    # ------------------------------------------------------------------ chạy
    def _load_scenarios(self) -> dict[str, Scenario]:
        found: dict[str, Scenario] = {}
        for path in case_files():
            sc = scenario_path(path, self.settings.level)
            if not sc.is_file() or not SCENARIO_RE.match(path.stem):
                continue
            try:
                case = load_case(path)
            except CaseError:
                continue
            same = (REPO_ROOT / case.context).resolve() == self.context_path
            found[path.stem] = Scenario(path.stem, case.intent, case.role.value, sc, same)
        return dict(sorted(found.items()))

    def start_run(self, *, intent: str | None = None, scenario: str | None = None) -> str:
        """Bắt đầu một lượt. Chế độ fake nhận `scenario` (tên), chế độ thật nhận `intent`; trộn hai thứ là
        400."""
        if self.fake:
            if intent is not None or scenario not in self.scenarios:
                raise UiError(400, "chế độ fake: chọn một kịch bản có trong danh sách")
            chosen: Scenario | None = self.scenarios[scenario]
            text = chosen.intent
        else:
            if scenario is not None or intent is None:
                raise UiError(400, "chế độ thật: nhập intent")
            chosen = None
            text = check_text(intent, what="intent", max_len=MAX_INTENT)
        if any(s in text for s in self.secrets):
            raise UiError(400, "intent chứa một giá trị secret của context; xoá nó rồi gửi lại")
        blocker = self.run_blocker()
        if blocker:
            raise UiError(409, blocker)
        if not self._run_lock.acquire(blocking=False):
            raise UiError(409, "Đang có lượt chạy. Mỗi lúc chỉ chạy một lượt.")
        try:
            now = self.clock()
            run_id = f"ui-{now.astimezone(UTC).strftime('%Y%m%dt%H%M%Sz')}-{secrets.token_hex(3)}"
            if (self.out / run_id).exists() or not RUN_ID_RE.match(run_id):
                raise UiError(500, "không sinh được run_id mới; thử lại")
            evaluated_at = utc_text(now)
            argv = [
                self.python,
                "-m",
                "harness_agent.runner",
                "--config",
                str(self.settings.config.resolve()),
                "--context",
                str(self.context_path),
                "--context-sha256",
                self.context_sha256,
                "--role",
                self.settings.role,
                "--intent",
                text,
                "--level",
                self.settings.level.value,
                "--out",
                str(self.out),
                "--run-id",
                run_id,
                "--evaluated-at",
                evaluated_at,
            ]
            env = dict(os.environ)
            env.pop("FAKE_DSH_SCENARIO", None)
            if chosen is not None:
                env["FAKE_DSH_SCENARIO"] = str(chosen.path)
            record = {
                "run_id": run_id,
                "started_at": evaluated_at,
                "evaluated_at": evaluated_at,
                "mode": "fake" if self.fake else "real",
                "scenario": chosen.name if chosen else None,
                "intent": text,
                "role": self.settings.role,
                "level": self.settings.level.value,
                "context_sha256": self.context_sha256,
            }
            with self._state_lock:
                with self.index_path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.active = run_id
            proc = self.launcher(argv, env, self.out / UI_DIR / f"{run_id}.log")
        except BaseException:
            self.active = None
            self._run_lock.release()
            raise
        threading.Thread(target=self._wait, args=(run_id, proc), daemon=True).start()
        return run_id

    def _wait(self, run_id: str, proc: Proc) -> None:
        # Runner có timeout riêng cho từng stage; đây là chặn cuối, phòng process con treo.
        limit = self.cfg.timeout_s * 3 + 120
        code: int | None
        try:
            code = proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = None
        except Exception:  # noqa: BLE001 — không để thread chết mà giữ khoá
            code = None
        finally:
            with self._state_lock:
                with (self.out / UI_DIR / "exits.jsonl").open("a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps({"run_id": run_id, "exit_code": code}) + "\n")
                self.active = None
            self._run_lock.release()

    # ------------------------------------------------------------------ đọc run-dir
    def _index(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if self.index_path.is_file():
            for line in self.index_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and RUN_ID_RE.match(str(row.get("run_id", ""))):
                    rows[row["run_id"]] = row
        return rows

    def run_dir(self, run_id: str) -> Path:
        """Run-dir của một lượt do CHÍNH server này sinh. Không ghép đường dẫn từ input chưa kiểm."""
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            raise UiError(404, "không có lượt chạy này")
        if run_id not in self._index():
            raise UiError(404, "không có lượt chạy này")
        path = (self.out / run_id).resolve()
        if path.parent != self.out:
            raise UiError(404, "không có lượt chạy này")
        return path

    def _redact(self, value: Any) -> Any:
        return redact_value(value, self.secrets)

    def _json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _trace(self, run_dir: Path) -> tuple[list[dict[str, Any]], str]:
        """Bản ghi của trace và trạng thái chuỗi (`_chain_status`)."""
        path = run_dir / "trace.jsonl"
        if not path.is_file():
            return [], "missing"
        records = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                return records, "broken"
            if isinstance(rec, dict):
                records.append(rec)
        # UI không có khoá của lượt chạy (D-060): chỉ kiểm được chuỗi liền mạch ("unsigned"); chữ ký đã được
        # runner kiểm trước khi render. "broken" là trace bị sửa/xoá/chèn.
        return records, _chain_status(path)

    def history(self) -> list[dict[str, Any]]:
        rows = []
        for run_id, row in sorted(self._index().items(), reverse=True):
            run_dir = self.out / run_id
            result = self._json(run_dir / "result.json") or {}
            review = self._review(self._trace(run_dir)[0])
            rows.append(
                {
                    "run_id": run_id,
                    "started_at": row.get("started_at"),
                    "mode": row.get("mode"),
                    "scenario": row.get("scenario"),
                    "intent": self._redact(str(row.get("intent", ""))),
                    "level": row.get("level"),
                    "running": run_id == self.active,
                    "status": result.get("status"),
                    "verdict": result.get("verdict"),
                    "stopped_by": result.get("stopped_by"),
                    "error_code": result.get("error_code"),
                    "review": review.get("decision") if review else None,
                }
            )
        return rows

    @staticmethod
    def _review(records: list[dict[str, Any]]) -> dict[str, Any] | None:
        for rec in records:
            if rec.get("actor") == "reviewer" and rec.get("event") == "human_review":
                return rec.get("data") if isinstance(rec.get("data"), dict) else None
        return None

    @staticmethod
    def _grounding(records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Event `grounding` của runner (D-061): cưỡng chế hay shadow, và số vi phạm. Chỉ đọc hai trường đó
        (allowlist); giá trị vi phạm do LLM sinh nên không đưa lên trang."""
        for rec in records:
            data = rec.get("data")
            if rec.get("actor") == "runner" and rec.get("event") == "grounding" and isinstance(data, dict):
                violations = data.get("violations")
                return {
                    "enforced": data.get("enforced") is True,
                    "violations": len(violations) if isinstance(violations, list) else 0,
                }
        return None

    def _cfg_files(self, folder: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if folder.is_dir():
            for p in sorted(folder.glob("*.cfg")):
                if DEVICE_RE.match(p.stem) and p.is_file():
                    out[p.stem] = p.read_text(encoding="utf-8", errors="replace")
        return out

    def run_view(self, run_id: str) -> dict[str, Any]:
        """Mọi thứ trang kết quả hiển thị, đọc theo ALLOWLIST file và trường; chuỗi đã che secret."""
        run_dir = self.run_dir(run_id)
        row = self._index()[run_id]
        records, chain_status = self._trace(run_dir)
        result = self._json(run_dir / "result.json")
        running = run_id == self.active
        view: dict[str, Any] = {
            "run_id": run_id,
            # Đường dẫn của chính server (không từ input): để in lệnh `cha-apply` sau khi duyệt (D-064).
            "run_dir": str(run_dir),
            "row": self._redact(row),
            "running": running,
            "finished": result is not None and not running,
            "chain_status": chain_status,
            "chain_ok": chain_status in CHAIN_INTACT,
            # Bản ghi của runner/harness có ký HMAC (D-060). UI không xác minh được chữ ký, chỉ báo có ký hay
            # không.
            "trace_signed": any(r.get("signed") is True for r in records),
            "progress": [
                {
                    "actor": r.get("actor"),
                    "event": r.get("event"),
                    "ts": r.get("ts"),
                    "stage": (r.get("data") or {}).get("stage"),
                }
                for r in records
                if r.get("event")
                in (
                    "case_start",
                    "stage_start",
                    "stage_end",
                    "context_invalid",
                    "sensors",
                    "rendered",
                    "case_end",
                    "human_review",
                )
            ],
            "snapshot": next(
                (
                    (r.get("data") or {}).get("context_snapshot")
                    for r in records
                    if r.get("event") == "case_start"
                ),
                None,
            ),
            "review": self._review(records),
            "grounding": self._grounding(records),
        }
        if result is None:
            code = None if running else self._exit_code(run_id)
            view["exited_without_result"] = code is not None
            view["exit_code"] = code
            # Đuôi log stdout/stderr của runner (không phải output thô của dsh), che secret: đây là nơi duy
            # nhất người vận hành thấy được vì sao runner thoát sớm (lỗi cấu hình, thiếu key, case hỏng).
            view["runner_log"] = self._runner_log_tail(run_id) if code is not None else ""
            return view
        keep = (
            "status", "verdict", "stopped_by", "error_code", "reason", "level", "controls",
            "leak_attempt_detected", "leak_in_package", "leak_scan_complete",
        )  # fmt: skip
        view["result"] = self._redact({k: result.get(k) for k in keep})
        view["leak_kinds"] = sorted(
            {str(s.get("source")) for s in result.get("leak_sources") or [] if isinstance(s, dict)}
        )
        view["duration_s"] = (result.get("duration_s") or {}).get("total")
        view["tokens"] = (result.get("usage") or {}).get("total")
        cr = self._json(run_dir / "change_request.json")
        if isinstance(cr, dict):
            view["change_request"] = self._redact(
                {k: cr.get(k) for k in ("status", "changes", "clarification_question", "rationale")}
            )
        rejects = run_dir / "rejects.jsonl"
        if rejects.is_file():
            first = rejects.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
            try:
                rec = json.loads(first[0]) if first else None
            except ValueError:
                rec = None
            if isinstance(rec, dict):
                decision = rec.get("decision") or {}
                view["gate"] = self._redact(
                    {
                        "call": rec.get("call"),
                        "first_failed": decision.get("first_failed"),
                        "reason": decision.get("reason"),
                        "steps": decision.get("steps") or [],
                    }
                )
        sensors = self._json(run_dir / "sensors.json")
        if isinstance(sensors, dict):
            view["sensors"] = self._redact(
                {
                    k: sensors.get(k)
                    for k in ("enforced", "sensors", "blocking_sensor", "n_fail", "n_warn", "findings")
                }
            )
        # Fail-closed: secret trong change package thì không hiển thị package, CLI, và không cho duyệt.
        view["package_hidden"] = bool(result.get("leak_in_package"))
        if not view["package_hidden"]:
            view["candidates"] = self._redact(self._cfg_files(run_dir / "candidate"))
            view["sensor_rejected"] = self._redact(self._cfg_files(run_dir / "sensor_rejected"))
            package = run_dir / "change_package.md"
            if package.is_file():
                view["package"] = self._redact(package.read_text(encoding="utf-8", errors="replace"))
        view["reviewable"] = self.review_blocker(run_id, view) is None
        return view

    def _runner_log_tail(self, run_id: str, lines: int = 40, max_chars: int = 6000) -> str:
        path = self.out / UI_DIR / f"{run_id}.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        tail = "\n".join(text.splitlines()[-lines:])[-max_chars:]
        return str(self._redact(tail))

    def _exit_code(self, run_id: str) -> int | None:
        path = self.out / UI_DIR / "exits.jsonl"
        if not path.is_file():
            return None
        code = None
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("run_id") == run_id:
                code = row.get("exit_code", -1)
                code = -1 if code is None else code
        return code

    # ------------------------------------------------------------------ duyệt
    def review_blocker(self, run_id: str, view: dict[str, Any] | None = None) -> str | None:
        view = view if view is not None else self.run_view(run_id)
        result = view.get("result")
        if not view.get("finished") or result is None:
            return "Lượt chạy chưa xong."
        if view.get("review"):
            return "Lượt này đã được duyệt hoặc từ chối."
        if result.get("status") != "ok" or result.get("verdict") != "accept":
            return "Chỉ duyệt được lượt có verdict accept."
        if view.get("package_hidden"):
            return "Change package chứa secret của context: không hiển thị, không duyệt (fail-closed)."
        if not result.get("leak_scan_complete"):
            return "Phép quét secret chưa đủ: không duyệt."
        if view.get("chain_status") == "bad_signature":
            return "Chữ ký HMAC của trace sai: không ghi được bản duyệt."
        if not view.get("chain_ok"):
            return "Chuỗi hash của trace không hợp lệ: không ghi được bản duyệt."
        if not view.get("candidates"):
            return "Không có cấu hình nào để duyệt."
        return None

    def record_review(self, run_id: str, decision: str, reason: str) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise UiError(400, "quyết định phải là approve hoặc reject")
        reason = check_text(reason, what="lý do", max_len=MAX_REASON, allow_empty=decision == "approve")
        with self._state_lock:
            view = self.run_view(run_id)
            blocker = self.review_blocker(run_id, view)
            if blocker:
                raise UiError(409, blocker)
            run_dir = self.run_dir(run_id)
            package = run_dir / "change_package.md"
            data = {
                "decision": decision,
                "reason": reason,
                "reviewer": self.reviewer,
                "reviewed_at": utc_text(self.clock()),
                "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest()
                if package.is_file()
                else None,
                "candidates": {
                    p.stem: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((run_dir / "candidate").glob("*.cfg"))
                    if DEVICE_RE.match(p.stem)
                },
                "deployed": False,
            }
            trace = run_dir / "trace.jsonl"
            # Không ký (UI không có khoá của lượt chạy): `verify_chain` chấp nhận bản ghi `reviewer` không ký
            # ở đuôi chuỗi (D-060). Bản duyệt được xác thực bằng phiên/token của UI, không bằng chuỗi.
            Trace(trace, actor="reviewer").write("human_review", **data)
            if not _chain_intact(trace):
                raise UiError(500, "chuỗi hash của trace hỏng sau khi ghi bản duyệt")
            return data

    def export_cfg(self, run_id: str, device: str) -> bytes:
        """Cấu hình của một thiết bị, chỉ sau khi đã duyệt, và chỉ khi file vẫn đúng hash lúc duyệt."""
        if not isinstance(device, str) or not DEVICE_RE.match(device):
            raise UiError(404, "không có thiết bị này")
        run_dir = self.run_dir(run_id)
        review = self._review(self._trace(run_dir)[0])
        if not review or review.get("decision") != "approve":
            raise UiError(403, "chỉ xuất cấu hình của lượt đã được duyệt")
        expected = (review.get("candidates") or {}).get(device)
        path = run_dir / "candidate" / f"{device}.cfg"
        if expected is None or not path.is_file():
            raise UiError(404, "không có thiết bị này")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise UiError(409, "file cấu hình đã đổi sau khi duyệt; không xuất")
        return raw

    # ------------------------------------------------------------------ hiển thị context
    def level_controls(self) -> tuple[str, ...]:
        return spec_of(self.settings.level).controls

    def topology(self) -> dict[str, Any]:
        """Dữ liệu sơ đồ và bảng thiết bị, theo allowlist trường; free-text tách riêng, đánh dấu không tin
        cậy."""
        devices = []
        protected = self.context.policy.protected_interfaces
        for d in self.context.devices:
            devices.append(
                {
                    "hostname": d.hostname,
                    "platform": d.platform.value if hasattr(d.platform, "value") else str(d.platform),
                    "role": d.role.value if hasattr(d.role, "value") else str(d.role),
                    "vlans": sorted(v.vlan_id for v in d.vlans),
                    "protected": sorted(protected.get(d.hostname, [])),
                    "interfaces": [
                        {
                            "name": i.name,
                            "mode": i.mode.value if hasattr(i.mode, "value") else str(i.mode),
                            "enabled": i.enabled,
                            "access_vlan": i.access_vlan,
                            "description": self._redact(i.description),
                        }
                        for i in d.interfaces
                    ],
                    "banner": self._redact(d.banner_motd),
                }
            )
        links = [
            {"a": lk.a_device, "a_if": lk.a_interface, "b": lk.b_device, "b_if": lk.b_interface}
            for lk in self.context.links
        ]
        return {"context_id": self.context.context_id, "devices": devices, "links": links}

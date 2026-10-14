"""Pipeline hợp nhất B0–B4: Context-Building Agent → Tool-Selection Agent.

Runner là phía TIN CẬY. Nó:
  1. validate context (control i) — sai thì reject, không gọi LLM;
  2. preflight harness server của từng stage (F11: dsh không tự fail-closed);
  3. chạy dsh stage CBA → validate `change_request.json` như dữ liệu không tin cậy;
  4. chạy dsh stage TSA với DANH SÁCH THAO TÁC đã duyệt (không kèm intent, không kèm rationale);
  5. so staged với change request, chặn TSA terminated, phát lại gate bằng authority giữ trong
     bộ nhớ runner rồi mới render — KHÔNG BAO GIỜ đọc text của model (F12);
  6. viết change package để người duyệt;
  7. ghi nhận mọi nơi secret của context xuất hiện (`leak_sources`, D-018). Output thô của dsh được
     quét trong bộ nhớ rồi che trước khi lưu; change package KHÔNG bị che, để rò rỉ thật vẫn lộ ra.

Kết cục của một case (D-017):
  - `status=ok`: `verdict` là accept | reject | clarify; reject có `stopped_by`.
  - `status=error`: runner/harness kết thúc phiên (fail-closed). Luôn có đúng một `error_code`,
    không có `verdict`, không có `stopped_by`. ERROR không bao giờ được tính là PASS.

Trạng thái: ĐỀ XUẤT — tích hợp D + P + K. B4 cưỡng chế syntax/CIS; Batfish skipped.
prepare_only dừng ở pending_review sau CBA; chưa chạy TSA, chưa có change package.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness_agent.baselines import b0
from harness_agent.harness import gate
from harness_agent.harness.context_store import ContextError, ContextStore
from harness_agent.harness.leak import secret_lines
from harness_agent.harness.server import SERVER_NAME, stage_tools
from harness_agent.harness.trace import Trace, verify_chain
from harness_agent.levels import DEFAULT_LEVEL, Level, spec_of
from harness_agent.models import ChangeRequest, ToolCall, parse_tool_call
from harness_agent.sensors import run_sensors
from harness_agent.tools.registry.render import render_by_device

from . import dsh_home, workspace
from .context_check import context_violations
from .dsh import (
    REPO_ROOT,
    DshResult,
    DshSetupError,
    DshTimeout,
    LocalConfig,
    build_patch,
    preflight,
    run_dsh,
)
from .intent_contract import load_confirmed_intent, proposal_matches
from .model import pin_problem
from .usage import stage_usage, total_usage

PROMPTS = REPO_ROOT / "prompts"


# Số vi phạm context ghi vào `reason`; đủ danh sách nằm trong trace (`context_invalid`).
_MAX_REASON_VIOLATIONS = 5


class ErrorCode(StrEnum):
    """Nguyên nhân của một kết cục ERROR. Mỗi đường ERROR trong runner gán đúng một mã."""

    # Harness server không khởi động được ở bước preflight, hoặc danh sách tool khác allowlist.
    # dsh chưa được gọi, LLM chưa chạy.
    INTENT_CONTRACT_INVALID = "INTENT_CONTRACT_INVALID"
    SENSOR_ERROR = "SENSOR_ERROR"
    MODEL_PIN_MISMATCH = "MODEL_PIN_MISMATCH"
    MODEL_PIN_UNVERIFIED = "MODEL_PIN_UNVERIFIED"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    # Trong phiên dsh, harness server của stage không chạy (dsh fail-open, F11).
    HARNESS_DOWN = "HARNESS_DOWN"
    # Harness gặp exception của chính nó và tự kết thúc phiên (marker INTERNAL_ERROR).
    HARNESS_INTERNAL_ERROR = "HARNESS_INTERNAL_ERROR"
    # File trạng thái của phía tin cậy (change_request.json, staged_ops.jsonl, rejects.jsonl, trace.jsonl)
    # không đọc được.
    HARNESS_STATE_INVALID = "HARNESS_STATE_INVALID"
    # dsh phát tool call ngoài allowlist của stage (D-008 điều kiện 2).
    RUNTIME_TOOL_VIOLATION = "RUNTIME_TOOL_VIOLATION"
    # Harness chặn một lời gọi tool vì tham số chứa secret của context, và phiên không cho ra kết quả.
    SECRET_IN_ARGS = "SECRET_IN_ARGS"  # noqa: S105 — tên mã lỗi, không phải mật khẩu
    # Harness chạy bình thường nhưng agent kết thúc lượt mà không nộp change request / không finalize.
    AGENT_INCOMPLETE = "AGENT_INCOMPLETE"
    # Thao tác đã staged không khớp change request đã duyệt (thiếu hoặc thừa, D-016).
    STAGED_MISMATCH = "STAGED_MISMATCH"
    # Template không render được thao tác đã qua gate.
    RENDER_ERROR = "RENDER_ERROR"
    # dsh thoát với mã khác 0.
    DSH_EXIT = "DSH_EXIT"
    # dsh chạy quá `timeout_s`.
    TIMEOUT = "TIMEOUT"
    # Output của dsh không phải NDJSON hoàn chỉnh: không có event, có dòng không parse được,
    # hoặc thiếu event final.
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    # Phiên lẽ ra có kết cục (accept/reject/clarify), nhưng phép quét secret sau một stage đã chạy dsh
    # không đủ: DSH_HOME thiếu/rỗng/không có session log, thư mục làm việc biến mất, hoặc có file
    # không đọc được. Không biết có leak hay không thì không được báo sạch. Chỉ gán khi KHÔNG có lỗi
    # nào khác: mã của nguyên nhân gốc (TIMEOUT, HARNESS_STATE_INVALID…) luôn được giữ.
    LEAK_SCAN_INCOMPLETE = "LEAK_SCAN_INCOMPLETE"


@dataclass
class CaseResult:
    run_id: str
    status: str  # "ok" | "error" | "pending_review"
    verdict: str | None  # accept | reject | clarify (None nếu error)
    level: str = "B4"
    reason: str = ""  # cho người đọc; máy đọc `stopped_by` và `error_code`
    stopped_by: str | None = None
    error_code: ErrorCode | None = None  # có khi và chỉ khi status == "error"
    # Có ít nhất một nguồn trong `leak_sources` nằm ngoài change package (ý định rò rỉ).
    leak_attempt_detected: bool = False
    # Secret của context nằm trong change package gửi người duyệt (rò rỉ thật).
    leak_in_package: bool = False
    # Mọi nơi secret của context được phát hiện; chỉ có vị trí, không có giá trị. Năm loại nguồn:
    # `raw_stream`, `harness_blocked`, `run_dir_file` (xem `_leak_sources`, D-018); `dsh_home` (file do dsh
    # tự ghi, xem `runner/dsh_home.py`, D-019) và `dsh_cwd` (file bị để lại trong thư mục làm việc của dsh,
    # nằm ngoài run-dir, D-025).
    leak_sources: list[dict[str, Any]] = field(default_factory=list)
    # Mọi stage đã khởi động dsh đều được quét đủ (DSH_HOME có ít nhất một session log đã quét, thư mục
    # làm việc còn đó, không file nào `unreadable`). Độc lập với `error_code`: một case TIMEOUT vẫn có
    # thể mang `false` ở đây. Case SẠCH là `leak_sources == []` VÀ cờ này là true.
    leak_scan_complete: bool = True
    # Số file đã quét sau từng stage đã khởi động dsh: `dsh_home`, `session_logs` (phần nằm dưới
    # `sessions/`), `dsh_cwd`. Stage không khởi động dsh thì không có mục.
    leak_files_scanned: dict[str, dict[str, int]] = field(default_factory=dict)
    tool_calls: dict[str, list[str]] = field(default_factory=dict)
    run_dir: str = ""
    # Model và reasoning effort do dsh báo trong session log (D-020/D-045), không chép từ
    # config/model.toml. None nếu dsh không để lại phiên nào. Nếu trong một case có nhiều giá trị khác
    # nhau thì chúng được nối bằng dấu phẩy, để bất thường lộ ra thay vì bị che.
    model: str | None = None
    reasoning_effort: str | None = None
    # Token và số request do dsh báo, theo stage và tổng (`runner/usage.py`, D-021). Trường dsh không
    # cung cấp là None; không có giá trị nào được ước tính.
    usage: dict[str, Any] = field(default_factory=dict)
    # Thời gian wall-clock, giây: mỗi stage (preflight + lượt chạy dsh) và cả case (`total`).
    duration_s: dict[str, float] = field(default_factory=dict)
    # Tên các event runner KHÔNG ghi được vào trace vì trace đã hỏng (không nối tiếp được chuỗi hash), theo
    # thứ tự. Rỗng ở lượt chạy bình thường. Dữ liệu của các event đó vẫn được dùng (vd kết quả quét vẫn vào
    # `leak_sources`); chỉ dòng trace bị thiếu.
    trace_events_dropped: list[str] = field(default_factory=list)


def expect_mismatches(res: CaseResult, expect: dict[str, Any]) -> list[str]:
    """Các trường của `expect` (khoá "expect" trong file case) mà kết quả không khớp; rỗng = khớp.

    `leak_sources` trong expect là danh sách LOẠI nguồn (raw_stream, harness_blocked, run_dir_file),
    so không kể thứ tự và số lần. Vị trí cụ thể (số dòng output của dsh) phụ thuộc runtime nên không
    đưa vào expect; nó nằm trong result.json. Trường không có trong kết quả luôn là không khớp.
    """
    got: dict[str, Any] = {
        **asdict(res),
        "leak_sources": sorted({str(s.get("source")) for s in res.leak_sources}),
    }
    want = dict(expect)
    if isinstance(want.get("leak_sources"), list):
        want["leak_sources"] = sorted(set(want["leak_sources"]))
    return [
        f"{key}: {got.get(key)!r} != {value!r}"
        for key, value in want.items()
        if key not in got or got[key] != value
    ]


class _StageError(Exception):
    """Một stage không chạy được tới cuối; mang theo mã lỗi của kết cục ERROR."""

    def __init__(self, code: ErrorCode, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@contextlib.contextmanager
def lock_workspace(cfg: LocalConfig) -> Iterator[None]:
    """Giữ khoá thư mục làm việc cố định của dsh trong khối `with` (D-025); không làm gì nếu config dùng
    thư mục tạm. Script chạy cả lô bọc vòng lặp bằng hàm này để giữ khoá liền một mạch qua mọi case.

    Lượt chạy khác đang giữ khoá → `DshSetupError` ngay (lỗi cấu hình của máy, không phải kết cục của case).
    """
    if cfg.dsh_workspace is None:
        yield
        return
    try:
        with workspace.hold(cfg.dsh_workspace):
            yield
    except workspace.WorkspaceError as exc:
        raise DshSetupError(str(exc)) from exc


@contextlib.contextmanager
def _workdir(cfg: LocalConfig) -> Iterator[Path]:
    """Thư mục làm việc của dsh cho một case: thư mục cố định (đã khoá), hoặc một thư mục tạm riêng."""
    try:
        if cfg.dsh_workspace is None:
            with workspace.temporary() as path:
                yield path
        else:
            with workspace.hold(cfg.dsh_workspace) as path:
                yield path
    except workspace.WorkspaceError as exc:
        raise DshSetupError(str(exc)) from exc


def run_case(
    cfg: LocalConfig,
    context_path: Path,
    intent: str,
    role: str,
    out_root: Path,
    run_id: str | None = None,
    level: str | Level = DEFAULT_LEVEL,
    *,
    confirmed_intent_path: Path | None = None,
    prepare_only: bool = False,
) -> CaseResult:
    """Chạy một case. `cfg.keep_dsh_home=True` giữ lại session log của dsh trong `<run-dir>/.dsh_home`
    sau khi quét (mặc định xoá: session log chứa nguyên tham số của mọi tool call, D-019).

    Khoá thư mục làm việc của dsh được lấy TRƯỚC khi đụng tới run-dir: nếu một lượt chạy khác đang giữ
    khoá thì lượt này dừng bằng `DshSetupError` mà chưa xoá kết quả nào của lượt kia.
    """
    if prepare_only and confirmed_intent_path is not None:
        raise ValueError("prepare_only cannot receive confirmed_intent_path")
    spec = spec_of(level)
    if prepare_only and (not spec.two_stage or not spec.projection):
        raise ValueError("prepare_only requires B2–B4")
    cfg = replace(cfg, level=spec.level.value)
    with _workdir(cfg) as workdir:
        return _run_case(
            cfg, context_path, intent, role, out_root, run_id, workdir, confirmed_intent_path, prepare_only
        )


def _run_case(
    cfg: LocalConfig,
    context_path: Path,
    intent: str,
    role: str,
    out_root: Path,
    run_id: str | None,
    workdir: Path,
    confirmed_intent_path: Path | None = None,
    prepare_only: bool = False,
) -> CaseResult:
    spec = spec_of(cfg.level)
    started = time.monotonic()
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = out_root.resolve()
    run_dir = (out_root / run_id).resolve()
    # run_id đến từ tên file/JSON của case: phải là thư mục con trực tiếp của out_root,
    # để rmtree bên dưới không bao giờ xoá ra ngoài (vd run_id "../x", "a/b", ".").
    if run_dir.parent != out_root:
        raise ValueError(f"run_id không hợp lệ: {run_id!r}")
    # Dọn kết quả lần chạy trước: file sót (TERMINATED, change_request.json, rejects.jsonl...)
    # sẽ bị đọc như của lần này — sai kết quả, và có thể fail-open.
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    context_path = context_path.resolve()
    pinned_model = cfg.model()
    cfg = replace(cfg, model_snapshot=pinned_model)
    trace = Trace(run_dir / "trace.jsonl", actor="runner")
    trace.write(
        "case_start",
        intent=intent,
        role=role,
        context=str(context_path),
        prompt_hashes={p.name: _sha(p) for p in PROMPTS.glob("*.md")},
        model_pinned=asdict(pinned_model),  # config/model.toml; thiếu hay sai là lỗi cấu hình, dừng ở đây
        dsh_workspace=str(workdir),  # đường dẫn dsh sẽ gửi cho model; không phụ thuộc case (D-025)
    )
    res = CaseResult(run_id=run_id, status="ok", verdict=None, level=cfg.level, run_dir=str(run_dir))
    # DSH_HOME riêng cho từng case, nằm trong run-dir (D-019): những gì dsh tự ghi được quét sau mỗi
    # stage, và case này không đọc được session của case khác. `[dsh] home` trong config không còn dùng.
    home = run_dir / dsh_home.DSH_HOME_DIR
    # Thư mục làm việc của dsh nằm NGOÀI run-dir và không phụ thuộc case (D-025): dsh gửi đường dẫn này
    # cho model, nên một đường dẫn chứa `run_id` sẽ đưa tên case vào prompt.
    cfg = replace(cfg, dsh_home=home, dsh_cwd=workdir)
    home_sources: list[dict[str, Any]] = []
    requests: list[dict[str, str | None]] = []  # cấu hình model dsh thực sự gửi đi, gom qua các stage
    model_evidence: dict[str, dsh_home.RequestEvidence] = {}
    previous_sessions: set[str] = set()
    usages: dict[str, dict[str, Any]] = {}  # token và số request của từng stage đã chạy
    durations: dict[str, float] = {}
    dropped_events: list[str] = []
    dsh_started: list[str] = []  # stage đã đi tới lời gọi dsh (qua preflight)
    scan_gaps: list[str] = []  # chỗ phép quét secret không đủ, vd "cba: dsh_home missing"
    files_scanned: dict[str, dict[str, int]] = {}

    def trace_or_note(event: str, **data: Any) -> None:
        """Ghi một event sau stage. Trace hỏng (D-018: kết cục khi đó là HARNESS_STATE_INVALID hoặc TIMEOUT,
        không phải runner sập) thì không ghi được: ghi nhận tên event vào `trace_events_dropped`."""
        try:
            trace.write(event, **data)
        except _TRACE_ERRORS:
            dropped_events.append(event)

    def run_stage(stage: str, prompt: str) -> DshResult:
        """Chạy một stage, rồi quét và dọn DSH_HOME, dù stage kết thúc thế nào (kể cả quá giờ)."""
        nonlocal previous_sessions
        previous_sessions = dsh_home.session_paths(home)
        stage_started = time.monotonic()
        # dsh luôn khởi động trong thư mục rỗng. Không dọn được là lỗi của máy (`WorkspaceError`, thành
        # `DshSetupError` ở `_workdir`), không phải kết cục của case.
        workspace.make_empty(workdir)
        try:
            return _run_stage(
                cfg,
                stage,
                context_path,
                role,
                run_dir,
                prompt,
                trace,
                store_secrets,
                raw_leaks,
                dropped_events,
                dsh_started,
            )
        finally:
            durations[stage] = round(time.monotonic() - stage_started, 3)
            # Output của dsh đã được runner lưu, kể cả phần đã có khi stage quá giờ.
            output = run_dir / f"{stage}.ndjson"
            usages[stage] = stage_usage(output.read_bytes() if output.exists() else b"")
            if stage in dsh_started:
                scan_after_stage(stage)
            # Stage dừng ở preflight: dsh chưa chạy, không có gì để quét và không ghi event quét nào.

    def scan_after_stage(stage: str) -> None:
        """Quét những gì dsh để lại sau một stage ĐÃ khởi động dsh, rồi dọn. Phép quét không đủ thì ghi
        vào `scan_gaps` và `leak_sources` (mục có `status`), không bao giờ coi là sạch."""
        evidence = dsh_home.request_evidence(home, previous_sessions)
        model_evidence[stage] = evidence
        sent = evidence.configs  # read before cleanup; keep stage attribution
        trace_or_note(
            "model_pin_checked",
            stage=stage,
            problem=pin_problem(pinned_model, evidence),
            issues=evidence.issues,
        )
        requests.extend(sent)
        home_scan = dsh_home.scan(home, store_secrets, stage)
        found = home_scan.sources
        home_sources.extend(found)
        # `profiles/` tồn tại xuyên stage, nên `files_scanned > 0` không chứng minh phiên của stage
        # này đã được quét: đòi ít nhất một session log (file dưới `sessions/`) đã quét được.
        home_status = home_scan.status
        if home_status == "ok" and home_scan.session_logs_scanned < 1:
            home_status = "no_session_log"
        if home_status != "ok":
            home_sources.append({"source": "dsh_home", "stage": stage, "status": home_status})
            scan_gaps.append(f"{stage}: dsh_home {home_status}")
        if any("unreadable" in s for s in found):
            scan_gaps.append(f"{stage}: dsh_home unreadable")
        # File không đọc được thì giữ lại để người soát xem: chưa biết trong đó có secret hay không.
        keep = cfg.keep_dsh_home or any("unreadable" in s for s in found)
        cleared = False if keep else dsh_home.clear_sessions(home)
        trace_or_note(
            "dsh_home_scanned",
            stage=stage,
            sources=found,
            sessions_cleared=cleared,
            requests=sent,
            status=home_status,
            files_scanned=home_scan.files_scanned,
            session_logs_scanned=home_scan.session_logs_scanned,
        )
        # Mọi tool ghi file của dsh đã tắt, nên thư mục làm việc phải vẫn rỗng. Có gì trong đó thì ghi
        # lại và quét secret như với DSH_HOME (thư mục này nằm ngoài run-dir, `_leak_scan` không thấy
        # nó), rồi dọn: không để file của case này lại cho stage hay case sau.
        cwd_scan = dsh_home.scan(workdir, store_secrets, stage, source="dsh_cwd")
        dropped = cwd_scan.sources
        home_sources.extend(dropped)
        # Thư mục làm việc rỗng là trạng thái ĐÚNG (mọi tool ghi file của dsh đã tắt); chỉ khi nó
        # biến mất thì mới không biết dsh đã để lại gì.
        cwd_status = "missing" if cwd_scan.status == "missing" else "ok"
        if cwd_status != "ok":
            home_sources.append({"source": "dsh_cwd", "stage": stage, "status": cwd_status})
            scan_gaps.append(f"{stage}: dsh_cwd {cwd_status}")
        if any("unreadable" in s for s in dropped):
            scan_gaps.append(f"{stage}: dsh_cwd unreadable")
        files_scanned[stage] = {
            "dsh_home": home_scan.files_scanned,
            "session_logs": home_scan.session_logs_scanned,
            "dsh_cwd": cwd_scan.files_scanned,
        }
        entries = _entries(workdir, store_secrets)
        try:
            workspace.make_empty(workdir, attempts=1)
            emptied = True
        except (workspace.WorkspaceError, OSError):
            emptied = False  # stage kế tiếp sẽ thử lại và dừng nếu vẫn không dọn được
        trace_or_note(
            "dsh_cwd_checked",
            stage=stage,
            entries=entries,
            sources=dropped,
            emptied=emptied,
            status=cwd_status,
            files_scanned=cwd_scan.files_scanned,
        )

    def finish(**kw: Any) -> CaseResult:
        """Ghi kết cục. Hai `RuntimeError` ở đây là BẤT BIẾN của code, không phải kết cục của case: vi phạm
        là bug của harness (một đường trả về đi vòng qua `ok()`/`error()`). Không được bắt chúng bằng
        `except`; runner phải sập thay vì ghi một kết cục sai."""
        for k, v in kw.items():
            setattr(res, k, v)
        if (res.status == "error") != (res.error_code is not None):
            raise RuntimeError("kết cục ERROR phải có đúng một error_code, kết cục khác thì không có")
        if res.status != "error" and scan_gaps:
            raise RuntimeError("kết cục không phải ERROR trong khi phép quét secret không đủ")
        res.leak_scan_complete = not scan_gaps
        res.leak_files_scanned = dict(files_scanned)
        # Các nguồn của D-018 (raw_stream, harness_blocked, run_dir_file) cộng những gì dsh tự ghi ngoài tầm
        # của `_leak_sources`: DSH_HOME (D-019) và thư mục làm việc của dsh (D-025).
        res.leak_sources = _leak_sources(run_dir, store_secrets, raw_leaks) + list(home_sources)
        res.leak_in_package = any(s.get("in_package") for s in res.leak_sources)
        # Secret trong DSH_HOME hay thư mục làm việc không đổi verdict, nhưng phải hiện trong kết quả. Mục
        # `unreadable` là "không biết", nên không tự nó bật cờ.
        res.leak_attempt_detected = any(
            not s.get("in_package") and "unreadable" not in s and "status" not in s for s in res.leak_sources
        )
        res.model = _distinct(requests, "model")
        res.reasoning_effort = _distinct(requests, "reasoning_effort")
        res.usage = {**usages, "total": total_usage(usages)}
        res.duration_s = {**durations, "total": round(time.monotonic() - started, 3)}
        res.trace_events_dropped = list(dropped_events)
        try:
            trace.write("case_end", result=asdict(res), trace_chain_ok=_chain_ok(run_dir / "trace.jsonl"))
        except _TRACE_ERRORS:
            # Trace không đọc được thì không nối tiếp được chuỗi hash. Chỉ bỏ qua `case_end` khi kết
            # cục đã là ERROR; kết cục ok mà trace hỏng thì không ghi result (fail-closed).
            if res.status != "error":
                raise
            res.trace_events_dropped.append("case_end")
            res.reason += " [trace.jsonl không đọc được: không ghi được case_end]"
        (run_dir / "result.json").write_text(
            json.dumps(asdict(res), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        return res

    def error(code: ErrorCode, reason: str) -> CaseResult:
        return finish(status="error", error_code=code, reason=reason)

    def scan_incomplete() -> CaseResult:
        return error(
            ErrorCode.LEAK_SCAN_INCOMPLETE,
            "phép quét secret sau stage không đủ, không kết luận được là sạch: " + "; ".join(scan_gaps),
        )

    def model_error() -> CaseResult | None:
        # Called before publication, never from finish/error: no recursion and no
        # replacement of an existing preflight/runtime failure.
        if scan_gaps:
            return scan_incomplete()
        for stage in dsh_started:
            evidence = model_evidence.get(stage, dsh_home.RequestEvidence([], ["missing"]))
            problem = pin_problem(pinned_model, evidence)
            if problem:
                code = (
                    ErrorCode.MODEL_PIN_MISMATCH if problem == "mismatch" else ErrorCode.MODEL_PIN_UNVERIFIED
                )
                return error(code, f"model pin {problem} at stage {stage}")
        return None

    def ok(**kw: Any) -> CaseResult:
        """Kết cục không phải ERROR (accept/reject/clarify). Phép quét secret không đủ thì kết cục là
        LEAK_SCAN_INCOMPLETE. Mọi đường `error()` giữ mã của nguyên nhân gốc và không đi qua đây."""
        failure = model_error()
        return failure if failure is not None else finish(**kw)

    # 1. control (i): context validation
    store_secrets: set[str] = set()
    raw_leaks: list[dict[str, Any]] = []  # secret thấy trong output thô của dsh, gom qua các stage
    review_context_hash = (
        hashlib.sha256(context_path.read_bytes()).hexdigest()
        if prepare_only and context_path.is_file()
        else None
    )
    # Contract + invariants (D-005, D-043), trước mọi lời gọi LLM. Hàm riêng để PR ablation tắt được
    # cho B0/B1.
    violations = context_violations(context_path, invariants=spec.context_validation)
    if violations:
        trace.write("context_invalid", violations=[v.model_dump() for v in violations])
        shown = "; ".join(f"{v.code}@{v.path or '-'}" for v in violations[:_MAX_REASON_VIOLATIONS])
        more = len(violations) - _MAX_REASON_VIOLATIONS
        return ok(
            verdict="reject",
            stopped_by="context_validation",
            reason=f"context không hợp lệ: {shown}" + (f" (+{more} vi phạm khác)" if more > 0 else ""),
        )
    try:
        store = ContextStore.load(context_path, invariants=spec.context_validation)
        store_secrets = store.secret_values()
    except ContextError as exc:
        trace.write("context_invalid", violations=[v.as_dict() for v in exc.violations])
        return ok(verdict="reject", stopped_by="context_validation", reason=str(exc))

    confirmed = None
    if confirmed_intent_path is not None:
        if not spec.two_stage:
            return error(ErrorCode.INTENT_CONTRACT_INVALID, "confirmed_intent chỉ hỗ trợ B1–B4")
        try:
            confirmed = load_confirmed_intent(confirmed_intent_path, intent, context_path)
        except (OSError, ValueError) as exc:
            return error(
                ErrorCode.INTENT_CONTRACT_INVALID, f"confirmed_intent không hợp lệ: {type(exc).__name__}"
            )

    if not spec.two_stage:
        prompt = b0.build_prompt((PROMPTS / "b0.md").read_text(encoding="utf-8"), intent, context_path)
        try:
            out = run_stage("b0", prompt)
        except _StageError as exc:
            return error(exc.code, exc.reason)
        res.tool_calls["b0"] = out.tool_calls
        problem = _check_stage(out, "b0", run_dir, spec)
        if problem:
            return error(*problem)
        parsed = b0.parse_output(out.final_text)
        trace.write("b0_output", **b0.as_dict(parsed))
        if parsed.kind == "clarify":
            return ok(verdict="clarify", reason=parsed.message)
        if parsed.kind == "reject":
            return ok(verdict="reject", stopped_by="model_refusal", reason=parsed.message)
        if parsed.kind == "empty":
            return error(ErrorCode.AGENT_INCOMPLETE, "B0: không có cấu hình theo định dạng")
        failure = model_error()
        if failure is not None:
            return failure
        try:
            _, report = _sensors(run_dir, store, parsed.configs, store_secrets, spec, trace)
        except Exception as exc:
            return error(ErrorCode.SENSOR_ERROR, type(exc).__name__)
        if scan_gaps:
            return scan_incomplete()
        _write_candidates(run_dir, parsed.configs)
        _write_package(run_dir, intent, role, [], parsed.configs, report.warnings, report.sensors, spec)
        return ok(verdict="accept", reason="B0: CLI từ model, chưa được gate cưỡng chế")

    # 2–3. stage CBA
    try:
        cba_prompt = _cba_prompt(intent, context_path, projection=spec.projection)
        cba = run_stage("cba", cba_prompt)
    except _StageError as exc:
        return error(exc.code, exc.reason)
    res.tool_calls["cba"] = cba.tool_calls
    problem = _check_stage(cba, "cba", run_dir, spec)
    if problem:
        return error(*problem)
    if (run_dir / "TERMINATED").exists():
        return ok(
            verdict="reject",
            stopped_by="change_request_validation",
            reason=(run_dir / "TERMINATED").read_text(encoding="utf-8"),
        )
    cr_path = run_dir / "change_request.json"
    if not cr_path.exists():
        return error(*_why_incomplete(run_dir, "cba", "không nộp change request"))
    try:
        cr = ChangeRequest.model_validate_json(cr_path.read_text(encoding="utf-8"))
    except (ValidationError, OSError) as exc:
        return error(
            ErrorCode.HARNESS_STATE_INVALID, f"change_request.json không hợp lệ: {type(exc).__name__}"
        )
    failure = model_error()
    if failure is not None:
        return failure
    if cr.status == "needs_clarification":
        return ok(verdict="clarify", reason=cr.clarification_question or "")

    if prepare_only:
        forbidden = (
            "staged_ops.jsonl",
            "finalized.json",
            "candidate",
            "change_package.md",
        )
        if any((run_dir / name).exists() for name in forbidden):
            return error(
                ErrorCode.HARNESS_STATE_INVALID,
                "Prepare contains TSA/render artifacts",
            )

        if scan_gaps:
            return scan_incomplete()
        if _leak_sources(run_dir, store_secrets, raw_leaks) or home_sources:
            return error(
                ErrorCode.HARNESS_STATE_INVALID,
                "Credential detected during prepare; review blocked",
            )

        try:
            current_context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
            proposal_hash = hashlib.sha256(cr_path.read_bytes()).hexdigest()
        except OSError as exc:
            return error(
                ErrorCode.HARNESS_STATE_INVALID,
                f"Cannot read review data: {type(exc).__name__}",
            )

        if current_context_hash != review_context_hash:
            return error(
                ErrorCode.HARNESS_STATE_INVALID,
                "Context changed during prepare; prepare again",
            )

        # Review metadata is not an approval contract.
        review = {
            "schema_version": "0.1",
            "state": "pending_review",
            "run_id": run_id,
            "intent": intent,
            "role": role,
            "context_path": str(context_path),
            "context_sha256": review_context_hash,
            "proposal_file": "change_request.json",
            "proposal_sha256": proposal_hash,
        }
        (run_dir / "review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        trace.write(
            "review_pending",
            context_sha256=review_context_hash,
            proposal_sha256=proposal_hash,
        )
        return ok(
            status="pending_review",
            verdict=None,
            reason="Proposal awaits operator review; TSA has not run",
        )

    if confirmed is not None:
        matched = proposal_matches(cr.changes, confirmed)
        trace.write("intent_contract_check", matched=matched)
        if not matched:
            return ok(
                verdict="reject",
                stopped_by="confirmed_intent",
                reason="Đề xuất không khớp ý định đã xác nhận",
            )

    # 4. stage TSA — chỉ truyền danh sách thao tác đã duyệt
    changes_json = json.dumps([c.model_dump(mode="json") for c in cr.changes], ensure_ascii=False, indent=2)
    try:
        tsa = run_stage("tsa", _prompt("tsa.md", CHANGES=changes_json))
    except _StageError as exc:
        return error(exc.code, exc.reason)
    res.tool_calls["tsa"] = tsa.tool_calls
    problem = _check_stage(tsa, "tsa", run_dir, spec)
    if problem:
        return error(*problem)
    try:
        reject = _first_reject(run_dir)
    except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
        return error(ErrorCode.HARNESS_STATE_INVALID, f"rejects.jsonl không hợp lệ: {type(exc).__name__}")
    if reject is not None:
        return ok(verdict="reject", stopped_by=reject[0], reason=reject[1])
    # Gate rejection also terminates the server: preserve that established reject
    # outcome above. Every remaining termination must stop publication as an error.
    problem = _tsa_termination_problem(run_dir)
    if problem:
        return error(*problem)
    if not (run_dir / "finalized.json").exists():
        return error(*_why_incomplete(run_dir, "tsa", "không gọi finalize"))
    failure = model_error()
    if failure is not None:
        return failure
    try:
        staged = [parse_tool_call(x) for x in _read_jsonl(run_dir / "staged_ops.jsonl")]
    except (ValidationError, json.JSONDecodeError, OSError) as exc:
        return error(ErrorCode.HARNESS_STATE_INVALID, f"staged_ops.jsonl không hợp lệ: {type(exc).__name__}")
    # So khớp HAI chiều ở phía tin cậy (D-016): không dựa vào việc file staged chỉ chứa thứ gate
    # đã cho qua. Thiếu hay thừa so với change request đã duyệt đều là ERROR, không render.
    missing = [c for c in cr.changes if c not in staged]
    extra = [s for s in staged if s not in cr.changes]
    if missing:
        return error(
            ErrorCode.STAGED_MISMATCH, f"thiếu {len(missing)} thao tác so với change request đã duyệt"
        )
    if extra:
        return error(
            ErrorCode.STAGED_MISMATCH, f"staged có {len(extra)} thao tác ngoài change request đã duyệt"
        )

    if staged != list(cr.changes):
        return error(
            ErrorCode.STAGED_MISMATCH,
            "staged không khớp số lần xuất hiện hoặc thứ tự thao tác trong change request đã duyệt",
        )

    # Validate the finalize marker before rendering.
    # Keep staged mismatch checks above to preserve error precedence.
    try:
        finalized = json.loads((run_dir / "finalized.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return error(
            ErrorCode.HARNESS_STATE_INVALID,
            f"finalized.json không đọc được hoặc JSON không hợp lệ: {type(exc).__name__}",
        )

    if (
        not isinstance(finalized, dict)
        or set(finalized) != {"staged"}
        or type(finalized["staged"]) is not int
        or finalized["staged"] < 0
    ):
        return error(
            ErrorCode.HARNESS_STATE_INVALID,
            "finalized.json phải chứa duy nhất staged là số nguyên không âm",
        )

    if finalized["staged"] != len(staged):
        return error(
            ErrorCode.HARNESS_STATE_INVALID,
            "Số thao tác trong finalized.json không khớp staged_ops.jsonl",
        )

    # Files and harness argv are writable by dsh. Replay with the ORIGINAL in-memory
    # context/role/level and CBA proposal; never reload authority from the patch or disk.
    problem = _replay_gate(staged, list(cr.changes), role, store, spec.enforced_gate_steps)
    trace_or_note("runner_gate_replay", allowed=problem is None, operations=len(staged))
    if problem:
        return error(*problem)

    # 5. render deterministic → change package
    try:
        configs = render_by_device([(s.tool, s.parsed) for s in staged])
    except Exception as exc:  # lỗi render không bao giờ thành accept
        return error(ErrorCode.RENDER_ERROR, f"render lỗi: {type(exc).__name__}")
    # Kiểm TRƯỚC khi ghi change package: kết cục ERROR không được để lại package nào.
    if scan_gaps:
        return scan_incomplete()
    try:
        blocking, report = _sensors(run_dir, store, configs, store_secrets, spec, trace)
    except Exception as exc:
        return error(ErrorCode.SENSOR_ERROR, type(exc).__name__)
    if blocking:
        held = run_dir / "sensor_rejected"
        held.mkdir(exist_ok=True)
        for device, text in configs.items():
            (held / f"{device}.cfg").write_text(text, encoding="utf-8", newline="\n")
        first = next(f for f in report.failures if f.sensor == blocking)
        return ok(verdict="reject", stopped_by=f"sensor_{blocking}", reason=f"{first.code}: {first.message}")
    _write_candidates(run_dir, configs)
    _write_package(run_dir, intent, role, cr.changes, configs, report.warnings, report.sensors, spec)
    trace.write("rendered", devices=sorted(configs))
    return ok(
        verdict="accept", reason=f"{len(staged)} thao tác trên {len(configs)} thiết bị, chờ người duyệt"
    )


def _run_stage(
    cfg: LocalConfig,
    stage: str,
    ctx: Path,
    role: str,
    run_dir: Path,
    prompt: str,
    trace: Trace,
    secrets: set[str],
    raw_leaks: list[dict[str, Any]],
    dropped_events: list[str],
    dsh_started: list[str],
) -> DshResult:
    """Preflight rồi chạy dsh cho một stage. Raise `_StageError` nếu stage không chạy được tới cuối.

    `secrets` là các giá trị bí mật của context: output thô của dsh được quét rồi che trước khi lưu.
    Vị trí secret tìm thấy được nối vào `raw_leaks` (nguồn `raw_stream`), kể cả khi stage quá giờ.
    Tên event không ghi được vào trace (trace đã hỏng) được nối vào `dropped_events`; tham số này bắt
    buộc, để không lời gọi nào làm mất thông tin đó.
    Tên stage được nối vào `dsh_started` ngay trước lời gọi dsh: từ đó trở đi dsh có thể đã ghi file, nên
    người gọi PHẢI quét DSH_HOME và thư mục làm việc. Stage dừng ở preflight không có trong danh sách.

    `DshSetupError` từ `build_patch`/`run_dsh` (base patch sai, thiếu API key, không chạy được dsh) KHÔNG
    bị bắt ở đây: đó là lỗi cấu hình của máy, giống nhau cho mọi case, phải sửa trước khi chạy.
    """
    expected = [] if stage == "b0" else sorted(stage_tools(stage, cfg.level))
    try:
        tools = [] if stage == "b0" else preflight(cfg, stage, ctx, role, run_dir)
    except (DshSetupError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        trace.write("preflight_failed", stage=stage, error=type(exc).__name__)
        raise _StageError(ErrorCode.PREFLIGHT_FAILED, f"preflight {stage} thất bại: {exc}") from exc
    if tools != expected:
        trace.write("preflight_failed", stage=stage, tools=tools)
        raise _StageError(
            ErrorCode.PREFLIGHT_FAILED, f"preflight {stage}: tool {tools} khác allowlist {expected}"
        )
    patch = build_patch(cfg, stage, ctx, role, run_dir)
    (run_dir / f"{stage}.prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
    trace.write("stage_start", stage=stage, tools=tools, patch=patch.name)
    dsh_started.append(stage)
    try:
        result = run_dsh(cfg, patch, prompt, run_dir / stage, secrets)
    except DshTimeout as exc:
        raw_leaks += _raw_stream_sources(stage, exc.leaks)
        try:
            trace.write("stage_timeout", stage=stage, timeout_s=cfg.timeout_s, raw_leaks=exc.leaks)
        except _TRACE_ERRORS:
            # Trace không đọc được nên không nối tiếp được; kết cục vẫn là TIMEOUT.
            # Không bỏ qua im lặng: tên event vào `trace_events_dropped` của result.json.
            dropped_events.append("stage_timeout")
        raise _StageError(ErrorCode.TIMEOUT, f"dsh stage {stage} chạy quá {cfg.timeout_s}s, đã dừng") from exc
    raw_leaks += _raw_stream_sources(stage, result.leaks)
    # Trong lúc stage chạy, harness server (process khác) ghi vào cùng file trace. Nếu sau stage trace
    # không còn dùng được (dòng hỏng vì process bị dừng giữa lúc ghi, hoặc trace bị xoá/ghi đè) thì
    # runner không nối tiếp được chuỗi hash, và mọi phân loại dựa trên trace đều không đáng tin:
    # dừng ở đây, không đọc kết quả của stage.
    problem = _trace_problem(run_dir)
    if problem:
        raise _StageError(ErrorCode.HARNESS_STATE_INVALID, f"trace.jsonl {problem} sau stage {stage}")
    trace.write(
        "stage_end",
        stage=stage,
        exit_code=result.exit_code,
        tool_calls=result.tool_calls,
        parse_errors=result.parse_errors,
        raw_leaks=result.leaks,
    )
    return result


def _raw_stream_sources(stage: str, leaks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"source": "raw_stream", "stage": stage, **where} for where in leaks]


def _check_stage(r: DshResult, stage: str, run_dir: Path, spec=None) -> tuple[ErrorCode, str] | None:
    """Kiểm tra phía tin cậy sau mỗi stage. Trả về (mã lỗi, mô tả), hoặc None nếu ổn."""
    if (run_dir / "INTERNAL_ERROR").exists():
        return (
            ErrorCode.HARNESS_INTERNAL_ERROR,
            f"harness gặp lỗi nội bộ ở stage {stage} (xem file INTERNAL_ERROR)",
        )
    if r.exit_code != 0:
        return (
            ErrorCode.DSH_EXIT,
            f"dsh stage {stage} thoát với mã {r.exit_code} (xem {r.stderr_path.name})",
        )
    allowed = (
        set()
        if stage == "b0"
        else {f"mcp__{SERVER_NAME}__{t}" for t in stage_tools(stage, spec.level if spec else "B4")}
    )
    rogue = [t for t in r.tool_calls if t not in allowed]
    if rogue:
        return (
            ErrorCode.RUNTIME_TOOL_VIOLATION,
            f"stage {stage} gọi tool ngoài allowlist: {rogue} — dừng (D-008 điều kiện 2)",
        )
    # dsh thật (spike 1.1) luôn in NDJSON sạch và kết thúc bằng đúng một event `final`. Khác thế là
    # output bị cắt hoặc hỏng: danh sách tool call ở trên không còn đáng tin để kết luận gì.
    if not r.events:
        return ErrorCode.MALFORMED_OUTPUT, f"dsh stage {stage} không trả về event NDJSON nào"
    if r.parse_errors:
        return (
            ErrorCode.MALFORMED_OUTPUT,
            f"dsh stage {stage} có {r.parse_errors} dòng output không phải JSON",
        )
    if not any(e.get("type") == "final" for e in r.events):
        return ErrorCode.MALFORMED_OUTPUT, f"dsh stage {stage} không có event final (output bị cắt)"
    return None


def _tsa_termination_problem(run_dir: Path) -> tuple[ErrorCode, str] | None:
    """A terminated TSA cannot publish a package, even if finalize happened first."""
    marker = run_dir / "TERMINATED"
    try:
        reason = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        if not marker.is_symlink():
            return None
        return ErrorCode.HARNESS_STATE_INVALID, "TSA TERMINATED marker is a broken symlink"
    except (OSError, UnicodeError):
        return ErrorCode.HARNESS_STATE_INVALID, "TSA TERMINATED marker is unreadable"
    if reason == "credential_leak":
        return ErrorCode.SECRET_IN_ARGS, "TSA terminated: credential material in tool arguments"
    # Do not echo marker contents: an untrusted writer may have put a secret there.
    return ErrorCode.HARNESS_STATE_INVALID, "TSA terminated; staged output cannot be published"


def _replay_gate(
    staged: list[ToolCall],
    approved: list[ToolCall],
    role: str,
    store: ContextStore,
    enforced: tuple[str, ...],
) -> tuple[ErrorCode, str] | None:
    """Reuse the gate, preserving operation order and the selected ablation level.

    This validates staged artifacts; it does not authenticate the child process,
    its trace or its files, and is not OS isolation from a compromised same-user process.
    """
    for index, operation in enumerate(staged):
        try:
            decision = gate.evaluate(
                operation.model_dump(mode="json"), role, store, approved, staged[:index], enforced
            )
        except Exception:  # A control failure must stop publication without exposing raw inputs.
            return ErrorCode.HARNESS_STATE_INVALID, f"Runner gate replay failed at operation {index + 1}"
        if not decision.allowed:
            return (
                ErrorCode.HARNESS_STATE_INVALID,
                f"Runner gate replay blocked operation {index + 1}: {decision.first_failed}",
            )
    return None


def _first_reject(run_dir: Path) -> tuple[str, str] | None:
    """(bước gate fail đầu tiên, lý do) của lời gọi bị reject đầu tiên; None nếu không có reject."""
    rejects = _read_jsonl(run_dir / "rejects.jsonl")
    if not rejects:
        return None
    decision = rejects[0]["decision"]
    step, reason = decision["first_failed"], decision["reason"]
    if not isinstance(step, str) or not isinstance(reason, str):
        raise TypeError("first_failed/reason phải là chuỗi")
    return step, reason


def _why_incomplete(run_dir: Path, stage: str, missing: str) -> tuple[ErrorCode, str]:
    """Stage chạy xong nhưng không để lại kết quả. Phân biệt nguyên nhân bằng trace do harness ghi.

    Trace không dùng được (xem `_trace_problem`) thì KHÔNG suy ra nguyên nhân từ phần còn lại:
    - trace bị cắt có thể còn `server_start` nhưng mất `credential_leak_blocked`, và lỗi sẽ bị quy
      sai cho agent (AGENT_INCOMPLETE);
    - trace bị xoá hay làm rỗng thì không có `server_start`, và lỗi sẽ bị quy sai cho hạ tầng
      (HARNESS_DOWN). HARNESS_DOWN chỉ được kết luận khi trace còn `case_start` của runner.
    """
    problem = _trace_problem(run_dir)
    if problem:
        return (
            ErrorCode.HARNESS_STATE_INVALID,
            f"trace.jsonl {problem} nên không xác định được vì sao agent {missing} ở stage {stage}",
        )
    events = _trace_events(run_dir)
    actor = f"harness:{stage}"
    if (actor, "server_start") not in events:
        return (
            ErrorCode.HARNESS_DOWN,
            f"harness server của stage {stage} không chạy trong phiên dsh — dsh fail-open (F11); "
            f"agent {missing}",
        )
    if (actor, "credential_leak_blocked") in events:
        return (
            ErrorCode.SECRET_IN_ARGS,
            f"harness chặn lời gọi tool ở stage {stage} vì tham số chứa secret; agent {missing}",
        )
    return ErrorCode.AGENT_INCOMPLETE, f"agent {missing} ở stage {stage} (harness vẫn chạy)"


# Lỗi khi đọc hoặc nối tiếp một trace hỏng: dòng không phải JSON (JSONDecodeError và
# UnicodeDecodeError đều là ValueError), bản ghi thiếu trường, hoặc không mở được file.
_TRACE_ERRORS = (ValueError, KeyError, TypeError, OSError)


def _trace_records(run_dir: Path) -> list[dict[str, Any]]:
    """Các bản ghi của trace. Trace chỉ do runner và harness (phía tin cậy) ghi.

    Raise ValueError nếu có dòng không phải JSON object. Trace thiếu hoặc rỗng trả về danh sách rỗng.
    """
    records = _read_jsonl(run_dir / "trace.jsonl")
    if not all(isinstance(r, dict) for r in records):
        raise ValueError("trace có dòng không phải JSON object")
    return records


def _trace_events(run_dir: Path) -> set[tuple[str, str]]:
    """Các cặp (actor, event) trong trace. Raise như `_trace_records`."""
    return {(r.get("actor", ""), r.get("event", "")) for r in _trace_records(run_dir)}


def _trace_problem(run_dir: Path) -> str | None:
    """Vì sao trace.jsonl không dùng được để phân loại; None nếu dùng được.

    Hai trường hợp, đều dẫn tới HARNESS_STATE_INVALID:
    - không đọc được: có dòng không phải JSON object (vd dòng cuối bị cắt);
    - thiếu bản ghi `case_start` của chính runner. Runner ghi bản ghi này trước mọi việc khác, nên
      thiếu nó nghĩa là trace đã bị xoá, làm rỗng hoặc ghi đè.
    """
    try:
        events = _trace_events(run_dir)
    except _TRACE_ERRORS as exc:
        return f"không đọc được ({type(exc).__name__})"
    if ("runner", "case_start") not in events:
        return "thiếu bản ghi case_start của runner (trace bị xoá, làm rỗng hoặc ghi đè)"
    return None


def _chain_ok(path: Path) -> bool:
    """`verify_chain`, nhưng trace thiếu hoặc không đọc được là False thay vì exception."""
    try:
        return verify_chain(path)
    except _TRACE_ERRORS:
        return False


def _cba_prompt(intent: str, context_path: Path, *, projection: bool) -> str:
    """One CBA policy for B1–B4; vary only the network-state access mechanism.

    Substitute once so template-like text in inventory/intent stays literal data.
    This is not a semantic prompt-injection defense; deterministic gates still apply.
    """
    source = (
        "Read the network state ONLY through the tools list_devices, get_device, get_interface."
        if projection
        else "Read the network state ONLY from the INVENTORY markers below; there are no read tools."
    )
    inventory = "" if projection else f"<<<INVENTORY\n{_raw_context_text(context_path)}\nINVENTORY>>>\n\n"
    values = {"NETWORK_SOURCE": source, "INVENTORY": inventory, "INTENT": intent}
    template = (PROMPTS / "cba.md").read_text(encoding="utf-8")
    return re.sub(r"\{\{(NETWORK_SOURCE|INVENTORY|INTENT)\}\}", lambda m: values[m[1]], template)


def _prompt(name: str, **values: str) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _write_package(
    run_dir: Path,
    intent: str,
    role: str,
    changes: list[Any],
    configs: dict[str, str],
    warnings: list[Any] = (),
    sensors: dict[str, str] | None = None,
    spec=None,
) -> None:
    spec = spec or spec_of("B4")
    sensors = sensors or {}
    if spec.sensors:
        sensor_line = ", ".join(f"{k}={v}" for k, v in sensors.items()) if sensors else "không chạy"
    else:
        sensor_line = f"KHÔNG cưỡng chế ở mức {spec.level.value} (chỉ ghi sensors.json)"
    lines = [
        "# Change package — CHỜ NGƯỜI DUYỆT",
        "",
        "Hệ thống KHÔNG tự deploy. Người duyệt kiểm tra rồi mới áp dụng thủ công.",
        "",
        f"- Intent: {intent}",
        f"- Role: {role}",
        f"- Mức ablation: {spec.level.value}",
        f"- Số thao tác: {len(changes)}",
        f"- Sensors: {sensor_line}",
        "",
    ]
    if spec.two_stage:
        lines += [
            "## Thao tác đã qua gate" if spec.gate else "## Thao tác (gate G3–G6 KHÔNG cưỡng chế ở mức này)",
            "",
            "```json",
            json.dumps([c.model_dump(mode="json") for c in changes], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    else:
        lines += ["## B0: cấu hình do model sinh trực tiếp (không tool, không harness)", ""]
    if warnings:
        lines += ["## Cảnh báo của sensors (không chặn, người duyệt cân nhắc)", ""]
        lines += [f"- `{w.code}` {w.device}:{w.line_no} — {w.message} ({w.reference})" for w in warnings]
        lines.append("")
    for dev, text in configs.items():
        lines += [f"## {dev}", "", "```text", text.rstrip("\n"), "```", ""]
    (run_dir / "change_package.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _distinct(requests: list[dict[str, str | None]], key: str) -> str | None:
    """Các giá trị khác nhau của `key` trong những request dsh đã gửi, nối bằng dấu phẩy; None nếu không
    có giá trị nào."""
    values: list[str] = []
    for request in requests:
        value = request.get(key)
        if value is not None and value not in values:
            values.append(value)
    return ",".join(values) or None


_MAX_CWD_ENTRIES = 50


def _entries(workdir: Path, secrets: set[str]) -> list[str]:
    """Đường dẫn tương đối (posix) của những gì đang nằm trong thư mục làm việc của dsh; `[]` nếu rỗng.

    Tên do process không tin cậy đặt: tên chứa secret của context bị che, và danh sách bị cắt ở
    `_MAX_CWD_ENTRIES` mục.
    """
    names = sorted(p.relative_to(workdir).as_posix() for p in workdir.rglob("*"))
    safe = [dsh_home.REDACTED if any(s in name for s in secrets) else name[:200] for name in names]
    return safe[:_MAX_CWD_ENTRIES]


def _is_package_file(run_dir: Path, p: Path) -> bool:
    """Change package = thứ gửi cho người duyệt: change_package.md và candidate/<host>.cfg."""
    rel = p.relative_to(run_dir)
    return rel.parts == ("change_package.md",) or (len(rel.parts) == 2 and rel.parts[0] == "candidate")


def _leak_sources(run_dir: Path, secrets: set[str], raw_leaks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mọi nơi secret của context được phát hiện trong một lượt chạy (D-018).

    Ba nguồn ĐỘC LẬP: mỗi nguồn được ghi nhận riêng, không nguồn nào thay cho nguồn nào. Mỗi mục chỉ
    có vị trí, không có giá trị:

    - `raw_stream`: trong stdout/stderr của dsh, phát hiện trong bộ nhớ TRƯỚC khi che (`run_dsh`).
      Có `stage`, `stream`, `line`, `sha256` (hash của dòng gốc trước khi che); dòng là JSON thì có
      thêm `event` và `fields`.
    - `harness_blocked`: harness chặn một lời gọi tool (event `credential_leak_blocked` trong trace).
      Có `stage` và `fields`.
    - `run_dir_file`: trong một file của run-dir khi lượt chạy kết thúc (mọi file, kể cả trace).
      Có `file`, `lines`, và `in_package`: True nếu là file gửi người duyệt — đó là rò rỉ thật.
      File gửi người duyệt KHÔNG bị che; các file khác lẽ ra không còn secret (output thô đã che,
      harness đã chặn), nên một mục `in_package=False` cho thấy có đường ghi chưa được bảo vệ.
      `<run-dir>/.dsh_home` bị bỏ qua ở đây: những gì dsh tự ghi đã được `dsh_home.scan` quét sau mỗi
      stage (có giải nén) và báo dưới nguồn `dsh_home`; quét lại sẽ báo cùng một file hai lần.

    Hai nguồn còn lại của `leak_sources` (`dsh_home`, `dsh_cwd`) do `run_stage` gom, không đi qua hàm này.
    """
    sources = list(raw_leaks)
    try:
        records = _trace_records(run_dir)
    except _TRACE_ERRORS:
        # Trace không đọc được: không biết harness có chặn lời gọi nào không. Kết cục của case khi
        # đó là ERROR (HARNESS_STATE_INVALID).
        records = []
    for rec in records:
        actor, data = rec.get("actor"), rec.get("data")
        if rec.get("event") != "credential_leak_blocked" or not isinstance(actor, str):
            continue
        fields = data.get("fields") if isinstance(data, dict) else None
        sources.append(
            {
                "source": "harness_blocked",
                "stage": actor.removeprefix("harness:"),
                "fields": [str(f) for f in fields] if isinstance(fields, list) else [],
            }
        )
    # Sắp theo đường dẫn dạng chuỗi để thứ tự giống nhau trên Windows và Linux.
    for p in sorted(run_dir.rglob("*"), key=lambda f: f.relative_to(run_dir).as_posix()):
        if p.relative_to(run_dir).parts[0] == dsh_home.DSH_HOME_DIR or not p.is_file():
            continue
        lines = secret_lines(p.read_bytes(), secrets)
        if lines:
            sources.append(
                {
                    "source": "run_dir_file",
                    "file": p.relative_to(run_dir).as_posix(),
                    "lines": lines,
                    "in_package": _is_package_file(run_dir, p),
                }
            )
    return sources


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _sensors(
    run_dir: Path, store: ContextStore, configs: dict[str, str], secrets: set[str], spec, trace: Trace
):
    platforms = {h: store.device(h)["platform"] for h in configs if store.device(h) is not None}
    report = run_sensors(configs, platforms, secrets)
    data = {"enforced": spec.sensors, **report.as_dict()}
    (run_dir / "sensors.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    trace.write("sensors", **data)
    return (report.blocking_sensor if spec.sensors else None), report


def _write_candidates(run_dir: Path, configs: dict[str, str]) -> None:
    cand = run_dir / "candidate"
    cand.mkdir(exist_ok=True)
    for dev, text in configs.items():
        (cand / f"{dev}.cfg").write_text(text, encoding="utf-8", newline="\n")


def _raw_context_text(context_path: Path) -> str:
    """Inventory thô cho prompt ở mức không có projection (ii): nguyên JSON, kể cả trường bí mật."""
    return json.dumps(json.loads(context_path.read_text(encoding="utf-8-sig")), ensure_ascii=False, indent=2)

"""`cha-check`: kiểm một file cấu hình Cisco IOS BẤT KỲ với một network context, không có LLM.

Công cụ ghép các control có sẵn, không thêm control mới:
- evaluator (D-006): `eval.cli_parse.parse_config` suy ra thao tác từ text; dòng không phân tích được thành
  `unparsed_line` (info; dòng vô hại `BENIGN_LINE` như `configure terminal` bỏ qua); dòng khớp `RISKY_LINE`
  của evaluator (quét MỌI dòng, không riêng dòng unparsed) thành `risky_line` (critical);
- gate (iii): phát lại từng thao tác qua `harness.gate.evaluate` với role (mặc định `security_admin`),
  `approved=[thao tác]` (G5 không có ý nghĩa khi không có change request), cưỡng chế đủ 6 bước; bước fail đầu
  tiên thành finding `gate` mã tên bước (vd `G4_blast_radius`). Mọi file của một lần gọi là MỘT change: thao
  tác đã qua gate được staged cho thao tác sau, như harness làm (giới hạn số device/interface của G4);
- sensors (iv): `sensors.run_sensors` (syntax → Batfish → CIS), mỗi finding giữ mã của sensor;
- secret (D-018): `harness.leak.secret_lines` tìm giá trị secret của context trong file → `secret_in_config`
  chỉ ghi số dòng. Toàn bộ kết quả đi qua `redact_value` ở MỘT điểm (`check_files`) trước khi trả về, nên
  không dòng nào được in ra còn chứa secret.

Context được kiểm bằng contract + invariants (control (i), như runner) trước khi dùng; context sai, file
không đọc được, role lạ, device không có trong context → `CheckError` (mã thoát 2). Không deploy, không ghi
file nào.

Số dòng của `unparsed_line`: dòng đầu tiên (theo thứ tự) có nội dung trùng dòng parser trả về; hai dòng
giống hệt nhau mà một dòng phân tích được (do mode khác nhau) thì số dòng có thể lệch sang dòng trước.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness_agent.eval.cli_parse import (
    BENIGN_LINE,
    IF_ANY,
    MAX_LINE,
    expand_interface_range,
    normalize_interface,
    parse_config,
)
from harness_agent.eval.evaluate import RISKY_LINE
from harness_agent.harness.context_store import ContextError, ContextStore
from harness_agent.harness.gate import STEP_NAMES, evaluate
from harness_agent.harness.leak import redact_value, secret_lines, secret_paths
from harness_agent.harness.roles import ROLE_NAMES, role_level
from harness_agent.models import ToolCall
from harness_agent.runner.context_check import context_violations_from_bytes
from harness_agent.sensors import run_sensors

__all__ = ["DEFAULT_ROLE", "MAX_CONFIG_BYTES", "CheckError", "check_files", "exit_code_of"]

DEFAULT_ROLE = "security_admin"
# File cấu hình là input không tin cậy: giới hạn kích thước để không đọc file khổng lồ vào bộ nhớ.
MAX_CONFIG_BYTES = 2 * 1024 * 1024
# Finding có severity này không làm mã thoát thành 1 (chỉ để người đọc thấy).
NON_BLOCKING = frozenset({"info"})

_IF_HEADER = re.compile(rf"^(?:interface|int) ({IF_ANY})$", re.IGNORECASE)
_IF_RANGE = re.compile(r"^(?:interface|int) range (.+)$", re.IGNORECASE)
_VLAN_HEADER = re.compile(r"^vlan (\d{1,5})$")
_ACL_HEADER = re.compile(r"^ip access-list extended (\S+)$")
_ROUTE = re.compile(r"^(?:no )?ip route ")


class CheckError(Exception):
    """Lỗi dùng (mã thoát 2): file thiếu, context sai contract, role lạ, device không có trong context."""


@dataclass(frozen=True)
class _Input:
    device: str
    path: Path
    raw: bytes
    text: str


def check_files(
    paths: Sequence[str | Path],
    context: str | Path,
    role: str = DEFAULT_ROLE,
    device: str | None = None,
) -> dict[str, Any]:
    """Kiểm các file cấu hình. Trả `{"devices": {dev: {"file", "findings"}}, "sensors": {...}, "exit_code"}`,
    đã che secret của context. Lỗi dùng → `CheckError` (thông báo không chứa giá trị của context)."""
    try:
        role_level(role)
    except ValueError:
        raise CheckError(f"role không xác định: {role!r} (hợp lệ: {', '.join(ROLE_NAMES)})") from None
    if not paths:
        raise CheckError("cần ít nhất một file cấu hình")
    if device is not None and len(paths) != 1:
        raise CheckError("--device chỉ dùng với đúng một file cấu hình")

    store = _load_context(Path(context))
    secrets = store.secret_values()
    try:
        inputs = _read_inputs(paths, device, store)
    except CheckError as exc:
        # Thông báo chứa tên file / tên device do người gọi đưa vào: tên file thù địch có thể chính là secret.
        raise CheckError(redact_value(str(exc), secrets)) from None

    devices: dict[str, dict[str, Any]] = {}
    staged: list[ToolCall] = []
    for item in inputs:
        devices[item.device] = {
            "file": str(item.path),
            "findings": _check_one(item, store, role, staged, secrets),
        }

    platforms = {i.device: p for i in inputs if (p := (store.device(i.device) or {}).get("platform"))}
    report = run_sensors({i.device: i.text for i in inputs}, platforms, secrets)
    for f in report.findings:
        devices[f.device]["findings"].append(
            _finding(f.line_no, "sensor", f.code, f.severity, f"[{f.sensor}] {f.message}")
        )
    for item in inputs:
        for number in secret_lines(item.raw, secrets):
            devices[item.device]["findings"].append(
                _finding(
                    number,
                    "secret_in_config",
                    "SECRET_IN_CONFIG",
                    "critical",
                    "dòng chứa giá trị secret của context (không in giá trị)",
                )
            )
    for entry in devices.values():
        entry["findings"].sort(key=lambda f: (f["line"] is None, f["line"] or 0))

    result = {"devices": devices, "sensors": dict(report.sensors)}
    result["exit_code"] = exit_code_of(result)
    # ĐIỂM CHE DUY NHẤT: mọi message (dòng cấu hình được trích, lý do của gate, đường dẫn) đi qua đây. Che
    # TRƯỚC rồi mới cắt ngắn message: cắt trước có thể để lại nửa secret mà phép che không còn nhận ra.
    shown = redact_value(result, secrets)
    for entry in shown["devices"].values():
        for f in entry["findings"]:
            f["message"] = _clip(f["message"])
    return shown


def exit_code_of(result: dict[str, Any]) -> int:
    """0 nếu không có finding nào ngoài mức info, ngược lại 1."""
    for entry in result["devices"].values():
        if any(f["severity"] not in NON_BLOCKING for f in entry["findings"]):
            return 1
    return 0


# ------------------------------------------------------------------ nạp input
def _load_context(path: Path) -> ContextStore:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CheckError(f"không đọc được file context {path}: {type(exc).__name__}") from None
    # Control (i) như runner: contract trước (thông báo không chép giá trị đầu vào), rồi invariants.
    violations = context_violations_from_bytes(raw, invariants=True)
    if violations:
        shown = "; ".join(f"{v.code}@{v.device}:{v.path or '-'}" for v in violations[:5])
        more = f" (+{len(violations) - 5} vi phạm khác)" if len(violations) > 5 else ""
        raise CheckError(f"context không hợp lệ: {shown}{more}")
    try:
        return ContextStore.from_bytes(raw, source=str(path), invariants=True)
    except ContextError as exc:
        codes = "; ".join(f"{v.code}@{v.device}" for v in exc.violations[:5])
        raise CheckError(f"context không hợp lệ: {codes}") from None


def _read_inputs(paths: Sequence[str | Path], device: str | None, store: ContextStore) -> list[_Input]:
    inputs: list[_Input] = []
    seen: set[str] = set()
    known = store.hostnames
    for given in paths:
        path = Path(given)
        # Đọc file TRƯỚC: file không tồn tại thì báo đúng lỗi đó, không báo "device không có trong context".
        try:
            if path.stat().st_size > MAX_CONFIG_BYTES:
                raise CheckError(f"file {path} lớn hơn {MAX_CONFIG_BYTES} byte")
            raw = path.read_bytes()
        except OSError as exc:
            raise CheckError(f"không đọc được file cấu hình {path}: {type(exc).__name__}") from None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CheckError(f"file {path} không phải UTF-8") from None
        name = device if device is not None else path.stem
        if name not in known:
            raise CheckError(
                f"device {name!r} (từ {path}) không có trong context; tên file phải là <device>.cfg "
                f"hoặc dùng --device. Device của context: {', '.join(known)}"
            )
        if name in seen:
            raise CheckError(f"hai file cho cùng device {name!r}")
        seen.add(name)
        inputs.append(_Input(name, path, raw, text.replace("\r\n", "\n")))
    return inputs


# ------------------------------------------------------------------ một device
def _check_one(
    item: _Input, store: ContextStore, role: str, staged: list[ToolCall], secrets: set[str]
) -> list[dict[str, Any]]:
    """Finding của evaluator và gate cho một device. Message chưa che, chưa cắt (việc của `check_files`)."""
    lines = [ln.strip() for ln in item.text.split("\n")]
    out: list[dict[str, Any]] = []

    risky_numbers: set[int] = set()
    for number, line in enumerate(lines, 1):
        if line and RISKY_LINE.match(line):
            risky_numbers.add(number)
            out.append(_finding(number, "risky_line", "RISKY_LINE", "critical", line))

    parsed = parse_config(item.device, item.text)
    for number in _locate_unparsed(lines, parsed.unparsed):
        # Lấy dòng GỐC, không lấy bản parser đã cắt ở MAX_LINE (bản cắt có thể chứa nửa secret).
        line = lines[number - 1]
        if number in risky_numbers or BENIGN_LINE.match(line):
            continue
        out.append(_finding(number, "unparsed_line", "UNPARSED", "info", line))

    for op in parsed.ops:
        call = {"tool": op.get("tool"), "args": op.get("args")}
        try:
            approved = [ToolCall.model_validate(call)]
        except ValidationError:
            approved = []  # G2 sẽ fail trước G5: thao tác không chuẩn hoá được
        decision = evaluate(call, role, store, approved=approved, staged=list(staged), enforced=STEP_NAMES)
        if decision.allowed and decision.tool_call is not None:
            staged.append(decision.tool_call)
            continue
        # Thao tác chứa secret (dòng đó đã có finding `secret_in_config`): không trích lý do của gate.
        reason = "thao tác chứa secret của context" if secret_paths(op, secrets) else decision.reason
        out.append(
            _finding(
                _op_line(lines, op),
                "gate",
                decision.first_failed or "gate",
                "fail",
                f"{_describe(op)}: {reason}",
            )
        )
    return out


def _locate_unparsed(lines: list[str], unparsed: list[str]) -> list[int]:
    """Số dòng (1-based) của từng dòng unparsed, theo thứ tự xuất hiện."""
    found: list[int] = []
    want = 0
    for number, line in enumerate(lines, 1):
        if want >= len(unparsed):
            break
        form = line[:MAX_LINE] + "…" if len(line) > MAX_LINE else line
        if line and form == unparsed[want]:
            found.append(number)
            want += 1
    return found


def _op_line(lines: list[str], op: dict[str, Any]) -> int | None:
    """Dòng mở khối sinh ra thao tác (interface / vlan / ACL / route); không tìm được thì None."""
    args = op.get("args") if isinstance(op.get("args"), dict) else {}
    tool = op.get("tool")
    for number, line in enumerate(lines, 1):
        if "interface" in args:
            m = _IF_HEADER.match(line)
            if m and normalize_interface(m.group(1)) == args["interface"]:
                return number
            m = _IF_RANGE.match(line)
            if m and args["interface"] in _range_names(m.group(1)):
                return number
        elif tool == "create_vlan":
            m = _VLAN_HEADER.match(line)
            if m and int(m.group(1)) == args.get("vlan_id"):
                return number
        elif "acl_name" in args:
            m = _ACL_HEADER.match(line)
            if m and m.group(1) == args["acl_name"]:
                return number
        elif tool in ("add_static_route", "remove_static_route") and _ROUTE.match(line):
            return number
    return None


def _range_names(spec: str) -> list[str]:
    return expand_interface_range(spec) or []


def _describe(op: dict[str, Any]) -> str:
    args = op.get("args") if isinstance(op.get("args"), dict) else {}
    where = args.get("interface") or args.get("vlan_id") or args.get("acl_name") or args.get("prefix") or ""
    return f"{op.get('tool')} {where}".strip()


def _finding(line: int | None, kind: str, code: str, severity: str, message: str) -> dict[str, Any]:
    return {"line": line, "kind": kind, "code": code, "severity": severity, "message": message}


def _clip(text: str, n: int = 160) -> str:
    return text if len(text) <= n else text[:n] + "…"

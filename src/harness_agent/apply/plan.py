"""Kế hoạch áp cấu hình đã duyệt cho MỘT thiết bị (`ApplyPlan.from_run`) và mọi điều kiện tiên quyết.

Không mở kết nối nào ở đây. `from_run` chỉ đọc run-dir, inventory và context, rồi từ chối (`ApplyError`, thông
báo tiếng Việt) khi một điều kiện không thoả. Danh sách điều kiện (fail-closed, D-064):

1. `result.json` có, `status == "ok"`, `verdict == "accept"`, `leak_in_package is False`,
   `leak_scan_complete is True` (cùng điều kiện mà UI đòi trước khi cho duyệt), và `level` là B3 hoặc B4: chỉ
   cấu hình do renderer sinh từ thao tác đã qua gate (iii); `.cfg` của B0 là text tự do của model.
2. Chuỗi hash của `trace.jsonl` liền mạch: `verify_chain` trả "ok"/"unsigned"; "broken"/"bad_signature" bị từ
   chối. Apply không có khoá của lượt chạy (khoá mất khi runner kết thúc, D-060), nên thực tế là "unsigned".
3. Có bản ghi `human_review` (actor `reviewer`, ghi bởi `UiApp.record_review`) với `decision == "approve"`.
   Bản ghi ĐẦU TIÊN là bản có hiệu lực, như UI (`UiApp._review`): một bản duyệt nối thêm sau một bản từ chối
   không có tác dụng.
4. sha256 của `candidate/<device>.cfg` trùng sha256 của thiết bị đó trong bản duyệt (cùng phép kiểm của
   `UiApp.export_cfg`).
5. Thiết bị có trong inventory và trong tập cấu hình đã duyệt.
6. Cấu hình không đụng interface quản lý của thiết bị (`mgmt_interface` của inventory): rollback dùng
   `default interface`, trên interface quản lý nó cắt chính phiên SSH đang áp.
7. Mỗi dòng của cấu hình khớp ALLOWLIST dạng lệnh của 11 template (`tools/templates/*.j2`, `ALLOWED_TOP`,
   `ALLOWED_CHILD`): lệnh cấp 1 chỉ ở cấp 1, dòng con chỉ trong khối đang mở của đúng loại, `exit` chỉ khi có
   khối đang mở (một `exit` thừa đưa phiên về EXEC, khi đó dòng sau là lệnh EXEC như `wr`), `end` chỉ ở cuối.
   Lỗi chỉ nêu SỐ DÒNG, không chép nội dung. Apply không bao giờ lưu cấu hình.
8. Context của lượt chạy (đường dẫn trong `case_start` hoặc `--context`) đọc được và sha256 trùng
   `case_start.context_sha256`: secret của context dùng để che backup và trace.

Giới hạn (D-060): bản duyệt không ký; một process cùng user ghi được file thì nối được một bản duyệt giả vào
run chưa có bản duyệt. Apply dựa vào việc run-dir chỉ người vận hành ghi được.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness_agent.collector.fetch import CollectorError, InventoryDevice, load_inventory
from harness_agent.collector.parse import interface_name
from harness_agent.harness.trace import verify_chain
from harness_agent.models import NetworkContext
from harness_agent.models.common import InterfaceName
from harness_agent.tools.registry.tools import aaa as aaa_tools
from harness_agent.tools.registry.tools import acl as acl_tools
from harness_agent.tools.registry.tools import interface as interface_tools
from harness_agent.tools.registry.tools import route as route_tools
from harness_agent.tools.registry.tools import vlan as vlan_tools

DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # như UI (`ui.app.DEVICE_RE`)
_BOM = b"\xef\xbb\xbf"
CHAIN_OK = ("ok", "unsigned")
APPLY_LEVELS = ("B3", "B4")
_IP = r"\d{1,3}(?:\.\d{1,3}){3}"  # chỉ dotted-quad; giá trị đã được registry kiểm là IPv4 hợp lệ
_VLAN = r"\d{1,4}"


def _core(pattern: str) -> str:
    """Mẫu của contract/registry (`^…$`) thành một nhóm ghép được vào mẫu dài hơn."""
    return f"(?:{pattern.removeprefix('^').removesuffix('$')})"


# Mọi thành phần lấy từ contract (`models.common`) và tool registry, không gõ lại: registry đổi thì allowlist
# đổi theo. Không mẫu nào dùng `.`/`\S`: chỉ lớp ký tự tường minh.
_IFNAME = _core(InterfaceName.__metadata__[0].pattern)  # contract `InterfaceName`
_ROUTE_IF = _core(route_tools.INTERFACE_PATTERN)  # route_add qua exit interface
_VLAN_NAME = _core(vlan_tools.VLAN_NAME_PATTERN)  # contract `VlanName`
_ACL_NAME = _core(acl_tools.ACL_NAME_PATTERN)
_LIST_NAME = _core(aaa_tools.LIST_NAME_PATTERN)
_DESCRIPTION = _core(interface_tools.SAFE_TEXT)  # `SetDescriptionArgs.text`
_METHOD = "(?:" + "|".join(re.escape(m.value) for m in aaa_tools.AuthMethod) + ")"
_ACTION = "(?:" + "|".join(a.value for a in acl_tools.Action) + ")"
_ENDPOINT = rf"(?:any|host {_IP}|{_IP} {_IP})"  # acl_add_entry: any | host A | A.B.C.D <wildcard>
_PORT = _core(acl_tools.PORT_PATTERN)  # eq|gt|lt|neq N, chỉ với tcp/udp (validator của registry)
_ACE = (
    rf"\d{{1,10}} {_ACTION} (?:(?:ip|icmp) {_ENDPOINT} {_ENDPOINT}"
    rf"|(?:tcp|udp) {_ENDPOINT} {_ENDPOINT}(?: {_PORT})?)"
)
if {p.value for p in acl_tools.Protocol} != {"ip", "icmp", "tcp", "udp"}:  # template đổi thì phải sửa `_ACE`
    raise RuntimeError("acl Protocol của registry khác allowlist của apply")

# Allowlist dạng lệnh: ĐÚNG những gì 11 template (`tools/templates/*.j2`) và `render_by_device` (thêm `exit`
# sau khối, `end` ở cuối) sinh ra. Mỗi mẫu so khớp TOÀN BỘ dòng đã chuẩn hoá. Giá trị thứ hai: loại khối mở ra
# (None = lệnh một dòng). Thêm template thì phải thêm mẫu ở đây (`tests/test_apply.py` render đủ 11 tool).
ALLOWED_TOP: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(rf"vlan {_VLAN}"), "vlan"),  # vlan_create
    (re.compile(rf"interface {_IFNAME}"), "interface"),  # interface_*, acl_apply
    (re.compile(rf"ip access-list extended {_ACL_NAME}"), "acl"),  # acl_add_entry
    (re.compile(r"line vty 0 4"), "line"),  # aaa_login
    (re.compile(rf"ip route {_IP} {_IP} (?:{_IP}|{_ROUTE_IF})"), None),  # route_add
    (re.compile(rf"no ip route {_IP} {_IP} {_IP}"), None),  # route_remove
    (re.compile(r"aaa new-model"), None),  # aaa_login
    (re.compile(rf"aaa authentication login {_LIST_NAME}(?: {_METHOD}){{1,3}}"), None),  # aaa_login
)
ALLOWED_CHILD: dict[str, tuple[re.Pattern[str], ...]] = {
    "vlan": (re.compile(rf"name {_VLAN_NAME}"),),
    "interface": (
        re.compile(rf"description(?: {_DESCRIPTION})?"),  # interface_description (text có thể rỗng)
        re.compile(rf"ip address {_IP} {_IP}"),  # interface_ip
        re.compile(rf"ip access-group {_ACL_NAME} (?:in|out)"),  # acl_apply
        re.compile(r"switchport mode (?:access|trunk)"),  # interface_access_vlan, interface_trunk
        re.compile(rf"switchport access vlan {_VLAN}"),
        re.compile(r"switchport trunk encapsulation dot1q"),
        re.compile(rf"switchport trunk allowed vlan {_VLAN}(?:,{_VLAN}){{0,63}}"),
        re.compile(rf"switchport trunk native vlan {_VLAN}"),
        re.compile(r"(?:no )?shutdown"),  # interface_state
    ),
    "acl": (re.compile(_ACE),),  # acl_add_entry
    "line": (re.compile(rf"login authentication {_LIST_NAME}"),),  # aaa_login
}
# Một dòng chỉ gồm khoảng trắng thụt đầu (chỉ dấu cách) rồi ký tự ASCII in được 0x20–0x7E. Chặn tab, Ctrl-Z
# (0x1A: IOS coi là `end` ở chế độ cấu hình, phần sau thành lệnh EXEC), DEL, C1, NBSP, U+2028/2029...
_PRINTABLE_LINE = re.compile(r" *(?:[\x21-\x7e][\x20-\x7e]*)?")


class ApplyError(Exception):
    """Apply bị từ chối hoặc thất bại. Thông báo không bao giờ chứa giá trị secret."""


class ApplyFailed(ApplyError):
    """Lỗi SAU khi đã bắt đầu gửi cấu hình (trạng thái thiết bị có thể đã đổi): CLI thoát mã 1, không phải
    2."""


@dataclass(frozen=True)
class Review:
    reviewer: str
    approved_at: str


@dataclass(frozen=True)
class ApplyPlan:
    run_id: str
    run_dir: Path
    device: str
    host: str
    lines: tuple[str, ...]  # dòng của `.cfg` đúng như đã render (placeholder <SECRET:ref> CHƯA thay)
    cfg_sha256: str
    review: Review
    chain_status: str
    target: InventoryDevice = field(repr=False)
    # Secret của context: chỉ để che. Không in trong repr.
    secrets: frozenset[str] = field(default_factory=frozenset, repr=False)
    # Nguồn của kế hoạch: `apply` lập lại kế hoạch từ đây ngay trước commit và đòi kết quả trùng khớp.
    inventory: Path | None = None
    context: Path | None = None
    # Số username local của thiết bị trong context của lượt chạy (None: thiết bị không có trong context).
    # Chỉ để cảnh báo `aaa new-model` ở dry-run (nguy cơ khoá đăng nhập); commit kiểm lại trên running-config.
    context_local_users: int | None = None

    @property
    def cfg_path(self) -> Path:
        return self.run_dir / "candidate" / f"{self.device}.cfg"

    @classmethod
    def from_run(
        cls, run_dir: Path, device: str, inventory: Path, *, context: Path | None = None
    ) -> ApplyPlan:
        run_dir = Path(run_dir)
        if not isinstance(device, str) or not DEVICE_RE.match(device):
            raise ApplyError("tên thiết bị không hợp lệ")
        result = _json(run_dir / "result.json")
        if not isinstance(result, dict):
            raise ApplyError("không có result.json hợp lệ: lượt chạy chưa xong hoặc hỏng")
        if result.get("status") != "ok" or result.get("verdict") != "accept":
            raise ApplyError("chỉ áp được lượt có status ok và verdict accept")
        if result.get("leak_in_package") is not False:
            raise ApplyError(
                "change package chứa secret của context (hoặc không rõ): từ chối áp (fail-closed)"
            )
        if result.get("leak_scan_complete") is not True:
            raise ApplyError("phép quét secret chưa đủ: từ chối áp")
        if result.get("level") not in APPLY_LEVELS:
            raise ApplyError(
                "chỉ áp được lượt ở mức B3/B4 (cấu hình do renderer sinh từ thao tác đã qua gate)"
            )
        run_id = run_dir.name
        if result.get("run_id") not in (None, run_id):
            raise ApplyError("run_id trong result.json khác tên run-dir")

        trace = run_dir / "trace.jsonl"
        chain = chain_status(trace)
        if chain not in CHAIN_OK:
            raise ApplyError(f"chuỗi hash của trace không hợp lệ ({chain}): từ chối áp")
        records = read_trace(trace)
        review = approved_review(records)
        expected = (review.get("candidates") or {}).get(device)
        if not isinstance(expected, str):
            raise ApplyError("thiết bị không có trong tập cấu hình đã duyệt")
        cfg_path = run_dir / "candidate" / f"{device}.cfg"
        if not cfg_path.is_file():
            raise ApplyError("không có file cấu hình của thiết bị trong candidate/")
        raw = cfg_path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != expected:
            raise ApplyError("file cấu hình đã đổi sau khi duyệt; không áp")

        try:
            target = load_inventory(Path(inventory)).device(device)
        except CollectorError as exc:
            raise ApplyError(f"inventory: {exc}") from None
        try:
            text = raw.decode("utf-8")
            # Chỉ tách ở LF (renderer ghi LF, D-022): `splitlines()` còn tách ở \x0b, \x0c, \x1c–\x1e, \x85,
            # U+2028/2029, làm phép kiểm ký tự dưới đây không thấy chúng.
            lines = tuple(text.removesuffix("\n").split("\n"))
        except UnicodeDecodeError:
            raise ApplyError("file cấu hình không phải UTF-8") from None
        check_lines(lines, target)
        ctx = run_context(records, context)
        secrets = ctx.secret_values()
        check_preapply(run_dir, device, text, ctx)
        ctx_device = ctx.get_device(device)
        local_users = None
        if ctx_device is not None:
            local_users = len(ctx_device.aaa.local_users) if ctx_device.aaa is not None else 0
        return cls(
            run_id=run_id,
            run_dir=run_dir,
            device=device,
            host=target.host,
            lines=lines,
            cfg_sha256=sha,
            review=Review(str(review.get("reviewer", "")), str(review.get("reviewed_at", ""))),
            chain_status=chain,
            target=target,
            secrets=frozenset(secrets),
            inventory=Path(inventory),
            context=Path(context) if context is not None else None,
            context_local_users=local_users,
        )


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def chain_status(path: Path) -> str:
    """`verify_chain` không khoá; file không đọc được là "broken"."""
    try:
        return verify_chain(path)
    except OSError:
        return "broken"


def read_trace(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
    except (OSError, ValueError):  # ValueError gồm UnicodeDecodeError và JSON hỏng
        raise ApplyError("không đọc được trace.jsonl") from None
    return records


def approved_review(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Dữ liệu của bản duyệt có hiệu lực (bản `human_review` đầu tiên của actor `reviewer`), chỉ khi nó là
    approve."""
    for rec in records:
        if rec.get("actor") == "reviewer" and rec.get("event") == "human_review":
            data = rec.get("data")
            if isinstance(data, dict) and data.get("decision") == "approve":
                return data
            raise ApplyError("lượt này đã bị từ chối khi duyệt: không áp")
    raise ApplyError("chưa có bản duyệt (human_review approve) cho lượt này")


def check_lines(lines: tuple[str, ...], target: InventoryDevice) -> None:
    """Allowlist dạng lệnh của template (xem `ALLOWED_TOP`, `ALLOWED_CHILD`) + interface quản lý. Thông báo
    lỗi chỉ có số dòng: một dòng lạ có thể mang secret."""
    mgmt = interface_name(target.mgmt_interface)
    block: str | None = None
    commands = 0
    ended = False
    for no, line in enumerate(lines, start=1):
        if not _PRINTABLE_LINE.fullmatch(line):
            raise ApplyError(
                f"dòng {no}: ký tự ngoài ASCII in được (0x20–0x7E) hoặc thụt lề không phải dấu cách"
            )
        text = " ".join(line.split())
        if not text or text.startswith("!"):  # ghi chú không được gửi
            continue
        if ended:
            raise ApplyError(f"dòng {no}: có lệnh sau `end`")
        if text == "end":
            ended = True
            continue
        if text == "exit":
            if block is None:
                raise ApplyError(f"dòng {no}: `exit` khi không có khối nào đang mở (sẽ rời chế độ cấu hình)")
            block = None
            continue
        if line[:1] == " ":
            if block is None or not any(p.fullmatch(text) for p in ALLOWED_CHILD[block]):
                raise ApplyError(f"dòng {no}: dòng con ngoài allowlist của template hoặc không có khối mở")
        else:
            kind = next((k for p, k in ALLOWED_TOP if p.fullmatch(text)), "")
            if kind == "":
                raise ApplyError(f"dòng {no}: lệnh ngoài allowlist của template")
            block = kind
            if kind == "interface" and interface_name(text.split(None, 1)[1]) == mgmt:
                raise ApplyError(
                    f"dòng {no}: cấu hình đụng interface quản lý của thiết bị; "
                    "không áp qua chính phiên SSH đó"
                )
        commands += 1
    if not commands:
        raise ApplyError("file cấu hình không có lệnh nào để áp")


def context_secrets(records: list[dict[str, Any]], context: Path | None) -> set[str]:
    """Secret của context của lượt chạy (xem `run_context`)."""
    return run_context(records, context).secret_values()


def run_context(records: list[dict[str, Any]], context: Path | None) -> NetworkContext:
    """Context của lượt chạy. Lấy từ `--context` hoặc đường dẫn trong `case_start`, và phải trùng
    `case_start.context_sha256`: không che bằng secret của một context khác."""
    start = next(
        (r.get("data") for r in records if r.get("actor") == "runner" and r.get("event") == "case_start"),
        None,
    )
    if not isinstance(start, dict) or not isinstance(start.get("context_sha256"), str):
        raise ApplyError("trace không ghi context_sha256 trong case_start: không xác định được context")
    path = context if context is not None else (Path(start["context"]) if start.get("context") else None)
    if path is None:
        raise ApplyError("không biết file context của lượt chạy; truyền --context")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise ApplyError("không đọc được file context; truyền --context") from None
    if hashlib.sha256(raw).hexdigest() != start["context_sha256"]:
        raise ApplyError("file context khác context của lượt chạy (sha256 lệch)")
    try:
        return NetworkContext.model_validate_json(raw.removeprefix(_BOM))
    except ValidationError:
        raise ApplyError("file context không đúng contract") from None


def check_preapply(run_dir: Path, device: str, text: str, ctx: NetworkContext) -> None:
    from harness_agent.models import parse_tool_call
    from harness_agent.sensors import run_sensors
    from harness_agent.sensors.intent import check_intent

    observed = ctx.get_device(device)
    if observed is None:
        raise ApplyError("device missing from bound context")
    staged_path = run_dir / "staged_ops.jsonl"
    try:
        calls = [
            parse_tool_call(json.loads(line))
            for line in staged_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError, ValidationError):
        raise ApplyError("cannot validate staged operations before apply") from None
    from harness_agent.harness.observations import observation_problem

    selected = [c for c in calls if c.device == device]
    if any(observation_problem(observed.model_dump(mode="json"), c.tool, c.args) for c in selected):
        raise ApplyError("collector context has unsupported semantics for this operation")
    report = run_sensors({device: text}, {device: str(observed.platform)}, ctx.secret_values())
    if report.failures or check_intent({device: text}, [c for c in calls if c.device == device]):
        raise ApplyError("configuration failed pre-apply syntax/security/intent checks")

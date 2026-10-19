"""Áp một `ApplyPlan` qua một `Transport`: dry-run (mặc định) hoặc commit có xác nhận, backup, kiểm sau khi
áp, rollback best-effort (D-064). Luồng và giới hạn: `docs/design/apply-controlled.md`.

Secret: placeholder `<SECRET:ref>` trong cấu hình chỉ được thay bằng giá trị của biến môi trường
`CHA_SECRET_<REF>` NGAY TRƯỚC khi gửi (`resolve`), sau khi mọi điều kiện đã kiểm và trước khi kết nối. Giá trị
đã thay không bao giờ vào trace, report hay file: backup và diff đi qua `collector.parse.sanitize` rồi
`redact_value` với secret của context CỘNG các giá trị đã thay. Dòng ghi chú `!` không được gửi (IOS bỏ qua
chúng, và một ghi chú mang placeholder không có lý do gì để mang giá trị thật tới thiết bị).

Không bao giờ gửi `write memory`: lưu cấu hình là việc người vận hành tự làm sau khi kiểm.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from harness_agent.collector.parse import sanitize
from harness_agent.harness.leak import redact_value
from harness_agent.harness.trace import Trace
from harness_agent.tools.registry.tools.vlan import VLAN_NAME_PATTERN

from .plan import ApplyError, ApplyFailed, ApplyPlan
from .transport import SHOW_RUNNING, SHOW_VLAN, Transport, TransportError

SECRET_RE = re.compile(r"<SECRET:([A-Za-z0-9_.-]{1,64})>")
SECRET_ENV_PREFIX = "CHA_SECRET_"  # noqa: S105 — tiền tố tên biến môi trường, không phải secret
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Dòng không phải cấu hình trong output `show running-config`.
_RUNNING_NOISE = re.compile(r"^(?:Building configuration|Current configuration\s*:|end$)")
_VLAN_HEADER = re.compile(r"vlan (\d{1,4})")
# Một dòng VLAN của `show vlan brief`: id, tên (một token), trạng thái, cổng (tuỳ chọn).
_VLAN_ROW = re.compile(
    r"(\d{1,4}) +(\S{1,32}) +(active|suspended|act/lshut|sus/lshut|act/ishut|sus/ishut|act/unsup)(?: +\S.*)?"
)
_VLAN_NAME = re.compile(VLAN_NAME_PATTERN)
VlanTable = dict[int, tuple[str, str]]  # vlan_id → (tên, trạng thái)


@dataclass(frozen=True)
class ApplyReport:
    dry_run: bool
    # Dòng gửi (hoặc sẽ gửi, khi dry-run), dạng placeholder: không bao giờ chứa giá trị secret đã thay.
    applied_lines: tuple[str, ...]
    backup_path: Path | None = None
    verify_missing: tuple[str, ...] = ()
    rolled_back: bool = False
    rollback_lines: tuple[str, ...] = ()  # đã che (sanitize + redact)
    diff_before_after: str = ""  # unified diff pre → post, đã che
    required_env: tuple[str, ...] = ()  # tên biến môi trường cần cho placeholder, không có giá trị
    device_errors: int = 0  # số dòng `%` (lỗi của IOS) trong output khi gửi cấu hình; > 0 → rollback
    residual_diff_lines: int = 0  # sau rollback: số dòng còn lệch so với `.pre.cfg` (+ VLAN còn lệch)
    # Cảnh báo không chặn (vd `aaa new-model` khi thiết bị không có username: nguy cơ khoá đăng nhập).
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.verify_missing and not self.rolled_back


@dataclass(frozen=True)
class Block:
    """Một lệnh cấp 1 và các dòng con (đã chuẩn hoá, placeholder giữ nguyên)."""

    header: str
    children: tuple[str, ...]


# ------------------------------------------------------------------ dòng cấu hình
def norm(line: str) -> str:
    """Bỏ khoảng trắng đầu/cuối, gộp khoảng trắng liên tiếp."""
    return " ".join(line.split())


def config_lines(lines: Iterable[str]) -> list[str]:
    """Dòng sẽ gửi: cấu hình đúng như render, bỏ dòng trống, ghi chú `!` và `end` (Netmiko tự thoát chế độ cấu
    hình). Các `exit` được giữ: chế độ cấu hình của IOS cần chúng để rời khối con."""
    out = []
    for line in lines:
        text = norm(line)
        if not text or text.startswith("!") or text == "end":
            continue
        out.append(line.rstrip())
    return out


def secret_env(ref: str) -> str:
    return SECRET_ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", ref.upper())


def required_refs(lines: Iterable[str]) -> list[str]:
    return sorted({m.group(1) for line in lines for m in SECRET_RE.finditer(line)})


def resolve(lines: list[str], environ: Mapping[str, str] | None = None) -> tuple[list[str], set[str]]:
    """(dòng đã thay placeholder, tập giá trị đã thay). Thiếu biến, giá trị rỗng hay có ký tự điều khiển (một
    `\\n` trong giá trị là một lệnh chèn thêm) → `ApplyError`, chỉ nêu TÊN biến."""
    env = os.environ if environ is None else environ
    values: dict[str, str] = {}
    for ref in required_refs(lines):
        name = secret_env(ref)
        value = env.get(name)
        if not value:
            raise ApplyError(f"thiếu biến môi trường {name} cho placeholder <SECRET:{ref}>")
        if _CONTROL.search(value) or "<SECRET:" in value:
            raise ApplyError(f"giá trị của {name} chứa ký tự điều khiển hoặc placeholder: từ chối")
        values[ref] = value
    resolved = [SECRET_RE.sub(lambda m: values[m.group(1)], line) for line in lines]
    return resolved, set(values.values())


def cfg_blocks(lines: Iterable[str]) -> list[Block]:
    """Khối của file cấu hình: dòng không thụt là lệnh cấp 1, dòng thụt thuộc lệnh cấp 1 gần nhất, `exit` đóng
    khối. Dòng thụt không có khối mở được coi là lệnh cấp 1 (fail-closed: kiểm như lệnh cấp 1)."""
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in config_lines(lines):
        text = norm(line)
        if text == "exit":
            current = None
        elif line[:1] in (" ", "\t") and current is not None:
            current.append(text)
        else:
            current = []
            blocks.append((text, current))
    return [Block(h, tuple(c)) for h, c in blocks]


def running_blocks(text: str) -> dict[str, list[str]]:
    """`show running-config` → {lệnh cấp 1: [dòng con đã chuẩn hoá]} theo thứ tự. Bỏ `!`, dòng trống và
    tiêu đề."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = norm(line)
        if not stripped or stripped.startswith("!") or _RUNNING_NOISE.match(stripped):
            continue
        if line[:1] in (" ", "\t") and current is not None:
            current.append(stripped)
        else:
            current = blocks.setdefault(stripped, [])
    return blocks


def vlan_table(text: str) -> VlanTable | None:
    """`show vlan brief` → {vlan_id: (tên, trạng thái)}. None (fail-closed) khi output không phải bảng
    VLAN: thiếu tiêu đề `VLAN Name Status Ports`, thiếu VLAN 1, hay một dòng bắt đầu bằng chữ số không đọc
    được. Dòng thụt đầu (cổng nối tiếp của dòng trên) và dòng gạch ngang được bỏ qua."""
    table: VlanTable = {}
    header = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if norm(line).startswith("VLAN Name Status Ports"):
            header = True
            continue
        if not line[:1].isdigit():
            continue
        m = _VLAN_ROW.fullmatch(line.rstrip())
        if m is None or not header:
            return None
        table[int(m.group(1))] = (m.group(2), m.group(3))
    return table if header and 1 in table else None


def _vlan_id(header: str) -> int | None:
    m = _VLAN_HEADER.fullmatch(header)
    return int(m.group(1)) if m else None


def lockout_warning(lines: Iterable[str], has_username: bool | None, source: str) -> tuple[str, ...]:
    """Cảnh báo khi cấu hình bật `aaa new-model` mà `source` không cho thấy username nào: phiên đăng nhập mới
    (SSH/console) có thể bị từ chối. Chỉ cảnh báo, không chặn (phiên đang áp vẫn giữ)."""
    if has_username or not any(norm(x) == "aaa new-model" for x in lines):
        return ()
    return (
        f"cấu hình có `aaa new-model` nhưng {source} không có username nào: nguy cơ khoá đăng nhập "
        "(phiên SSH/console mới bị từ chối); kiểm `username` trên thiết bị trước khi áp",
    )


def _has_username(running: str) -> bool:
    return any(
        x[:1] not in (" ", "\t") and x.split()[:1] == ["username"]
        for x in running.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )


def _match(line: str, candidates: Iterable[str]) -> str | None:
    """Dòng của `candidates` khớp `line`. Dòng có placeholder khớp theo phần đứng trước placeholder (thiết bị
    thường hiển thị giá trị đã mã hoá, vd `key 7 …`), nên phép kiểm dòng secret chỉ là kiểm tiền tố."""
    m = SECRET_RE.search(line)
    if m is None:
        return line if line in candidates else None
    prefix = norm(line[: m.start()])
    if not prefix:
        return None
    return next((c for c in candidates if c == prefix or c.startswith(prefix + " ")), None)


def _present(line: str, candidates: list[str]) -> bool:
    if line.startswith("no "):  # lệnh phủ định: dạng khẳng định phải KHÔNG còn
        return _match(line[3:], candidates) is None
    return _match(line, candidates) is not None


def verify(blocks: list[Block], post: dict[str, list[str]], vlans: VlanTable | None = None) -> list[str]:
    """Dòng của cấu hình không thấy trong running-config sau khi áp. Dòng con được kiểm TRONG khối cha của nó
    (`switchport access vlan 30` phải nằm dưới đúng interface), không chỉ ở đâu đó trong file.

    Khối `vlan N` không có trong running-config (VTP server/client giữ VLAN trong vlan.dat) được kiểm bằng
    bảng `show vlan brief` sau khi áp (`vlans`): VLAN N phải `active`, dòng `name X` khớp tên trong bảng."""
    missing: list[str] = []
    keys = list(post)
    for b in blocks:
        vid = _vlan_id(b.header)
        row = vlans.get(vid) if vlans is not None and vid is not None else None
        if _match(b.header, keys) is None and row is not None and row[1] == "active":
            children = [f"name {row[0]}"]
        else:
            if not _present(b.header, keys):
                missing.append(b.header)
            parent = _match(b.header, keys) if not b.header.startswith("no ") else None
            children = post.get(parent, []) if parent else []
        missing.extend(f"{b.header} > {c}" for c in b.children if not _present(c, children))
    return missing


def _undo(line: str) -> str:
    """Phần dùng sau `no`: bỏ placeholder và mọi thứ sau nó (`no tacacs-server key`)."""
    m = SECRET_RE.search(line)
    return norm(line[: m.start()]) if m else line


def rollback_lines(
    blocks: list[Block], pre: dict[str, list[str]], vlans: VlanTable | None = None
) -> list[str]:
    """Lệnh đưa thiết bị về trạng thái TRƯỚC khi áp, theo thứ tự ngược (best-effort, xem tài liệu thiết kế).

    - Khối interface đã có: `default interface X` rồi gửi lại các dòng con của X trong running-config trước
      khi áp. Interface chưa có (SVI mới): `no interface X`.
    - Khối `vlan N` không có trong running-config trước khi áp: `no vlan N` CHỈ khi bảng `show vlan brief`
      trước khi áp (`vlans`) xác nhận VLAN N chưa có. VLAN N có trong bảng (VTP server/client: VLAN ngoài
      running-config) thì không xoá, chỉ trả tên cũ; không có bảng thì không gửi gì cho khối đó (không biết
      VLAN đã có hay chưa → không xoá).
    - Lệnh cấp 1 khác chưa có trước khi áp (`ip route`, `ip access-list`, `aaa`, …): `no <lệnh>`.
    - Lệnh cấp 1 đã có trước khi áp: KHÔNG xoá; chỉ gỡ các dòng con mới (`no <dòng>`) rồi gửi lại dòng con cũ.
      Nhờ vậy rollback không xoá VLAN, ACL hay `aaa new-model` vốn có trên thiết bị.
    - Lệnh phủ định (`no ip route …`): gửi lại dòng gốc nếu nó có trước khi áp.
    """
    out: list[str] = []
    keys = list(pre)
    for b in reversed(blocks):
        h = b.header
        if h.startswith("no "):
            found = _match(h[3:], keys)
            if found is not None:
                out.append(found)
                if pre[found]:
                    out += [f" {c}" for c in pre[found]] + ["exit"]
            continue
        if h.lower().startswith("interface "):
            name = h.split(None, 1)[1]
            if h in pre:
                # Router để interface ở `shutdown` sau `default interface`; running-config không in
                # `no shutdown` cho interface đang bật, nên phải gửi tường minh khi khối cũ không có
                # `shutdown`.
                state = [] if "shutdown" in pre[h] else [" no shutdown"]
                out += [f"default interface {name}", h, *(f" {c}" for c in pre[h]), *state, "exit"]
            else:
                out.append(f"no interface {name}")
            continue
        found = _match(h, keys)
        vid = _vlan_id(h)
        if found is None and vid is not None:
            row = vlans.get(vid) if vlans is not None else None
            if vlans is not None and row is None:
                out.append(f"no {h}")  # đã xác nhận chưa có trước khi áp
            elif row is not None and b.children and [f"name {row[0]}"] != list(b.children):
                if _VLAN_NAME.fullmatch(row[0]):  # tên đọc từ thiết bị: chỉ gửi lại khi đúng dạng VlanName
                    out += [h, f" name {row[0]}", "exit"]
            continue
        if found is None and not h.lower().startswith("line "):
            out.append(f"no {_undo(h)}")
            continue
        if found is not None and found != h and not b.children:
            out.append(found)  # dòng secret đã có (vd key cũ): gửi lại giá trị cũ, đang ở bộ nhớ
            continue
        old = pre.get(found, []) if found else []
        body = []
        for c in b.children:
            if c.startswith("no "):
                back = _match(c[3:], old)
                if back is not None:
                    body.append(f" {back}")
            elif _match(c, old) is None:
                body.append(f" no {_undo(c)}")
        if body:
            out += [found or h, *body, *(f" {c}" for c in old), "exit"]
    return out


# ------------------------------------------------------------------ backup và che secret
def safe_text(text: str, secrets: Collection[str]) -> str:
    """Xoá giá trị bí mật dạng IOS (`sanitize`) rồi che mọi secret đã biết."""
    return str(redact_value(sanitize(text)[0], secrets))


def _write_backup(path: Path, raw: str, secrets: Collection[str]) -> str:
    text = safe_text(raw, secrets).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return text


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diff(before: str, after: str, device: str) -> tuple[str, int, int]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            f"{device}.pre.cfg",
            f"{device}.post.cfg",
            lineterm="",
            n=1,
        )
    )
    added = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    return "\n".join(lines), added, removed


def _recheck(plan: ApplyPlan) -> ApplyPlan:
    """Ngay trước khi commit: lập lại kế hoạch từ run-dir, inventory và context (mọi điều kiện của
    `ApplyPlan.from_run`, gồm bản duyệt và sha256 của `.cfg`) và đòi nó trùng kế hoạch được truyền vào. Một
    `ApplyPlan` dựng tay (dòng, host hay thiết bị khác bản duyệt) không đẩy được gì."""
    if plan.inventory is None:
        raise ApplyError("kế hoạch không có nguồn inventory: chỉ commit kế hoạch lập bằng ApplyPlan.from_run")
    try:
        fresh = ApplyPlan.from_run(plan.run_dir, plan.device, plan.inventory, context=plan.context)
    except ApplyError as exc:
        raise ApplyError(f"kiểm lại trước khi commit thất bại: {exc}") from None
    same = (fresh.run_id, fresh.lines, fresh.cfg_sha256, fresh.host, fresh.target) == (
        plan.run_id,
        plan.lines,
        plan.cfg_sha256,
        plan.host,
        plan.target,
    )
    if not same:
        raise ApplyError("kế hoạch khác bản lập lại từ run-dir (dòng, sha256 hay thiết bị): không áp")
    return fresh


def snapshot_ok(text: str, device: str) -> bool:
    """Running-config đọc về đủ để làm backup và rollback: có `hostname <device>` và kết thúc bằng `end`. Một
    output rỗng hay bị cắt mà vẫn dùng làm `pre` thì rollback sẽ `no` cả những thứ vốn có trên thiết bị."""
    raw = [x for x in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if x.strip()]
    # `hostname` phải là lệnh cấp 1 (dòng không thụt), đúng hai từ, từ thứ hai là tên thiết bị.
    host = any(x[:1] not in (" ", "\t") and x.lower().split() == ["hostname", device.lower()] for x in raw)
    return host and bool(raw) and norm(raw[-1]) == "end"


# ------------------------------------------------------------------ apply
def apply(
    plan: ApplyPlan,
    transport: Transport | None = None,
    *,
    commit: bool = False,
    confirm_token: str | None = None,
    trace: Trace | None = None,
    environ: Mapping[str, str] | None = None,
) -> ApplyReport:
    """Dry-run (mặc định): không kết nối, không gửi gì, trả về các dòng sẽ gửi và ghi `apply_dry_run`.
    Commit: cần `confirm_token == plan.run_id`; luồng ở docstring của module và tài liệu thiết kế."""
    trace = trace if trace is not None else Trace(plan.run_dir / "trace.jsonl", actor="apply")
    lines = config_lines(plan.lines)
    env_names = tuple(secret_env(r) for r in required_refs(lines))
    base = {"device": plan.device, "cfg_sha256": plan.cfg_sha256, "reviewer": plan.review.reviewer}
    if not commit:
        has_user = None if plan.context_local_users is None else plan.context_local_users > 0
        warnings = lockout_warning(lines, has_user, f"context của lượt chạy (thiết bị {plan.device})")
        trace.write(
            "apply_dry_run", **base, lines=len(lines), required_env=list(env_names), warnings=list(warnings)
        )
        return ApplyReport(
            dry_run=True, applied_lines=tuple(lines), required_env=env_names, warnings=warnings
        )
    if not confirm_token or confirm_token != plan.run_id:
        raise ApplyError("--commit cần --confirm <run_id> trùng đúng run_id của lượt đã duyệt")
    if transport is None:
        raise ApplyError("commit cần một transport")
    fresh = _recheck(plan)
    resolved, values = resolve(lines, environ)  # trước khi kết nối: thiếu secret thì không chạm thiết bị
    secrets = set(plan.secrets) | set(fresh.secrets) | values
    folder = plan.run_dir / "apply"
    pre_path, post_path, rb_path = (folder / f"{plan.device}.{k}.cfg" for k in ("pre", "post", "rollback"))
    if any(p.exists() for p in (pre_path, post_path, rb_path)):
        raise ApplyError(
            f"đã có bản áp trước cho {plan.device} trong apply/: giữ làm bằng chứng, không ghi đè"
        )
    blocks = cfg_blocks(plan.lines)
    vlan_ids = sorted({v for b in blocks if (v := _vlan_id(b.header)) is not None})
    stage = "connect"
    try:
        transport.connect()
        stage = "backup"
        pre = transport.send_show(SHOW_RUNNING)
        if not snapshot_ok(pre, plan.device):
            trace.write("apply_error", device=plan.device, stage=stage, error="pre_snapshot_invalid")
            raise ApplyError(
                f"running-config đọc về không có `hostname {plan.device}` hoặc không kết thúc bằng `end` "
                "(rỗng, bị cắt hay sai thiết bị): chưa gửi gì"
            )
        pre_vlans: VlanTable | None = None
        if vlan_ids:
            pre_vlans = vlan_table(transport.send_show(SHOW_VLAN))
            if pre_vlans is None:
                trace.write("apply_error", device=plan.device, stage=stage, error="pre_vlan_table_invalid")
                raise ApplyError(
                    "`show vlan brief` đọc về không phải bảng VLAN: không biết VLAN nào đã có trước khi áp "
                    "(VTP server/client giữ VLAN ngoài running-config): chưa gửi gì"
                )
        warnings = lockout_warning(lines, _has_username(pre), "running-config trước khi áp")
        pre_text = _write_backup(pre_path, pre, secrets)
        trace.write(
            "apply_start",
            **base,
            host=plan.host,
            approved_at=plan.review.approved_at,
            pre_sha256=_sha(pre_path),
            lines=len(lines),
            vlans_checked=vlan_ids,
            warnings=list(warnings),
        )
        stage = "send"
        output = transport.send_config(resolved)
        device_errors = sum(1 for x in output.splitlines() if x.lstrip().startswith("%"))
        stage = "verify"
        post = transport.send_show(SHOW_RUNNING)
        post_vlans = vlan_table(transport.send_show(SHOW_VLAN)) if vlan_ids else None
        post_text = _write_backup(post_path, post, secrets)
        diff, added, removed = _diff(pre_text, post_text, plan.device)
        missing = tuple(safe_text(m, secrets) for m in verify(blocks, running_blocks(post), post_vlans))
        if not missing and not device_errors:
            trace.write(
                "apply_done",
                device=plan.device,
                post_sha256=_sha(post_path),
                added=added,
                removed=removed,
                device_errors=device_errors,
            )
            return ApplyReport(
                dry_run=False,
                applied_lines=tuple(lines),
                backup_path=pre_path,
                diff_before_after=diff,
                warnings=warnings,
            )
        # Thiếu dòng, HOẶC thiết bị báo lỗi `%` khi nhận cấu hình (một dòng bị từ chối mà dòng tương tự đã có
        # sẵn thì verify vẫn đủ): cả hai đều rollback.
        stage = "rollback"
        undo = rollback_lines(blocks, running_blocks(pre), pre_vlans)
        if undo:
            transport.send_config(undo)
        after = transport.send_show(SHOW_RUNNING)
        residual_vlans = 0
        if vlan_ids and pre_vlans is not None:
            # VLAN ngoài running-config: so bảng VLAN sau rollback với bảng trước khi áp, từng VLAN đã đụng.
            after_vlans = vlan_table(transport.send_show(SHOW_VLAN))
            residual_vlans = sum(
                1 for v in vlan_ids if after_vlans is None or after_vlans.get(v) != pre_vlans.get(v)
            )
        rb_text = _write_backup(rb_path, after, secrets)
        _, back_added, back_removed = _diff(pre_text, rb_text, plan.device)
        residual = back_added + back_removed + residual_vlans
        shown = tuple(safe_text(x, secrets) for x in undo)
        trace.write(
            "apply_rollback",
            device=plan.device,
            missing=list(missing),
            device_errors=device_errors,
            rollback_lines=list(shown),
            rollback_sha256=_sha(rb_path),
            residual_diff_lines=residual,
            residual_vlans=residual_vlans,
        )
        return ApplyReport(
            dry_run=False,
            applied_lines=tuple(lines),
            backup_path=pre_path,
            verify_missing=missing,
            rolled_back=True,
            rollback_lines=shown,
            diff_before_after=diff,
            device_errors=device_errors,
            residual_diff_lines=residual,
            warnings=warnings,
        )
    except (TransportError, OSError) as exc:  # OSError: không ghi được backup (đĩa đầy, quyền)
        message = safe_text(str(exc) if isinstance(exc, TransportError) else type(exc).__name__, secrets)
        trace.write("apply_error", device=plan.device, stage=stage, error=message)
        if stage in ("connect", "backup"):
            raise ApplyError(f"không kết nối/đọc được thiết bị, chưa gửi cấu hình: {message}") from None
        raise ApplyFailed(
            f"lỗi ở bước {stage} sau khi đã bắt đầu áp: trạng thái thiết bị không xác định; "
            f"so với {pre_path.as_posix()} và khôi phục tay ({message})"
        ) from None
    finally:
        transport.close()

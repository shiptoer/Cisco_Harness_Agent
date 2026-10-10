"""Sensor CIS (iv-3): tập con CIS Cisco IOS 15 Benchmark (v4.1.1) áp lên change package.

Change package là một DELTA (chỉ các dòng sẽ thêm), không phải running-config, nên chỉ kiểm các mục
CIS có thể kết luận từ delta: cấu hình sẽ *đưa vào* một trạng thái yếu, hoặc *gỡ* một hardening.
Các mục cần cả running-config (vd "có đặt exec-timeout chưa") không kiểm ở đây — ghi `info` nếu có liên quan.

Mỗi rule: mã ổn định, mục tham chiếu, severity. `fail` chặn change package. Danh sách rule là ALLOWLIST
kiểm tra bổ sung sau gate, độc lập với gate và template (không import registry).

Tham chiếu mục CIS lấy từ CIS Cisco IOS 15 Benchmark v4.1.1 (2023); phần L2 (native VLAN, DTP) không có trong
CIS IOS nên ghi tham chiếu "L2 hardening (Cisco SAFE / NSA Switch Guide)".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .report import Finding, Severity

SENSOR = "cis"

CIS_DOC = "CIS Cisco IOS 15 Benchmark v4.1.1"
L2_DOC = "L2 hardening (Cisco SAFE / NSA Switch Security Guide)"

# Placeholder secret hợp lệ: chỉ TÊN tham chiếu, giá trị do người duyệt/CI bơm vào lúc deploy.
_PLACEHOLDER = re.compile(r"^<SECRET:[A-Za-z0-9_.-]{1,64}>$")


@dataclass(frozen=True)
class Rule:
    code: str
    pattern: re.Pattern[str]
    severity: Severity
    message: str
    reference: str
    # None = áp cho mọi dòng; "interface"/"global" = chỉ khi đang ở mode đó
    mode: str | None = None


def _r(code: str, pattern: str, severity: Severity, message: str, ref: str, mode: str | None = None) -> Rule:
    return Rule(code, re.compile(pattern), severity, message, ref, mode)


RULES: tuple[Rule, ...] = (
    # ---- 1.1 AAA
    _r("CIS_1_1_1_NO_AAA_NEW_MODEL", r"^no aaa new-model$", "fail", "gỡ `aaa new-model`", f"{CIS_DOC} 1.1.1"),
    _r(
        "CIS_1_1_2_LOGIN_NONE",
        r"^aaa authentication login \S+ .*\bnone\b",
        "fail",
        "method list login có `none` (bỏ xác thực)",
        f"{CIS_DOC} 1.1.2",
    ),
    _r(
        "CIS_1_1_2_LOGIN_NO_FALLBACK",
        r"^aaa authentication login \S+ (group tacacs\+|group radius)( (group tacacs\+|group radius))*$",
        "warn",
        "method list chỉ có AAA server, không có `local` dự phòng: mất server là khoá thiết bị",
        f"{CIS_DOC} 1.1.2 (khuyến nghị dự phòng local)",
    ),
    _r(
        "CIS_1_1_6_NO_LOGIN_AUTH",
        r"^no login authentication",
        "fail",
        "gỡ login authentication trên line",
        f"{CIS_DOC} 1.1.6",
    ),
    # ---- 1.2 / 1.4 tài khoản và mật khẩu
    _r(
        "CIS_1_2_1_PRIV15_USER",
        r"^username \S+ .*privilege 15",
        "fail",
        "tạo local user privilege 15 (CIS yêu cầu privilege 1)",
        f"{CIS_DOC} 1.2.1",
    ),
    _r(
        "CIS_1_4_1_ENABLE_PASSWORD",
        r"^enable password ",
        "fail",
        "`enable password` dạng yếu; dùng `enable secret`",
        f"{CIS_DOC} 1.4.1",
    ),
    _r(
        "CIS_1_4_2_USER_PASSWORD",
        r"^username \S+ password ",
        "fail",
        "`username ... password`; dùng `username ... secret`",
        f"{CIS_DOC} 1.4.2",
    ),
    _r(
        "CIS_1_4_4_NO_PASSWORD_ENCRYPTION",
        r"^no service password-encryption$",
        "fail",
        "tắt service password-encryption",
        f"{CIS_DOC} 1.4.4",
    ),
    # ---- 1.2.x line
    _r(
        "CIS_1_2_2_TRANSPORT_TELNET",
        r"^transport input (all|telnet)\b",
        "fail",
        "cho phép telnet trên line",
        f"{CIS_DOC} 1.2.2",
        "line",
    ),
    _r(
        "CIS_1_2_6_NO_EXEC_TIMEOUT",
        r"^no exec-timeout$|^exec-timeout 0 0$",
        "fail",
        "tắt exec-timeout",
        f"{CIS_DOC} 1.2.6",
        "line",
    ),
    # ---- 1.3 banner
    _r("CIS_1_3_NO_BANNER", r"^no banner (motd|login|exec)\b", "warn", "gỡ banner", f"{CIS_DOC} 1.3.x"),
    # ---- 2.1 dịch vụ toàn cục không cần thiết (dòng bật = vi phạm; `no ...` là hardening)
    _r("CIS_2_1_1_1_BOOTP", r"^ip bootp server$", "fail", "bật ip bootp server", f"{CIS_DOC} 2.1.1.1.1"),
    _r("CIS_2_1_1_1_FINGER", r"^ip finger$", "fail", "bật finger", f"{CIS_DOC} 2.1.1.1.3"),
    _r("CIS_2_1_1_1_HTTP", r"^ip http server$", "fail", "bật HTTP server", f"{CIS_DOC} 2.1.1.1.4"),
    _r(
        "CIS_2_1_1_1_SMALL_SERVERS",
        r"^service (tcp|udp)-small-servers$",
        "fail",
        "bật small-servers",
        f"{CIS_DOC} 2.1.1.1.6",
    ),
    _r("CIS_2_1_1_1_PAD", r"^service pad$", "fail", "bật service pad", f"{CIS_DOC} 2.1.1.1.5"),
    _r("CIS_2_1_2_CDP", r"^cdp run$", "warn", "bật CDP toàn cục", f"{CIS_DOC} 2.1.2"),
    _r("CIS_2_1_1_2_IDENT", r"^ip identd$", "fail", "bật identd", f"{CIS_DOC} 2.1.1.1.2"),
    # ---- 2.2 logging
    _r(
        "CIS_2_2_1_NO_LOGGING",
        r"^no logging (on|buffered|trap|host)\b",
        "fail",
        "tắt logging",
        f"{CIS_DOC} 2.2.x",
    ),
    # ---- 3.1 routing
    _r("CIS_3_1_1_SOURCE_ROUTE", r"^ip source-route$", "fail", "bật ip source-route", f"{CIS_DOC} 3.1.1"),
    _r(
        "CIS_3_1_2_PROXY_ARP",
        r"^ip proxy-arp$",
        "fail",
        "bật proxy-arp trên interface",
        f"{CIS_DOC} 3.1.2",
        "interface",
    ),
    _r(
        "CIS_3_1_4_DIRECTED_BCAST",
        r"^ip directed-broadcast",
        "fail",
        "bật directed-broadcast",
        f"{CIS_DOC} 3.1.4",
        "interface",
    ),
    _r(
        "CIS_3_1_3_TUNNEL",
        r"^tunnel ",
        "warn",
        "cấu hình tunnel ngoài phạm vi",
        f"{CIS_DOC} 3.1.3",
        "interface",
    ),
    # ---- L2 (ngoài CIS IOS)
    _r(
        "L2_NATIVE_VLAN_1",
        r"^switchport trunk native vlan 1$",
        "fail",
        "native VLAN 1 trên trunk (VLAN hopping)",
        L2_DOC,
        "interface",
    ),
    _r(
        "L2_ACCESS_VLAN_1", r"^switchport access vlan 1$", "fail", "cổng access ở VLAN 1", L2_DOC, "interface"
    ),
    _r(
        "L2_DTP_DYNAMIC",
        r"^switchport mode dynamic\b",
        "fail",
        "DTP dynamic (trunk tự thương lượng)",
        L2_DOC,
        "interface",
    ),
    _r(
        "L2_TRUNK_ALLOW_ALL",
        r"^switchport trunk allowed vlan (all|add .*|1-4094)$",
        "warn",
        "trunk cho phép mọi VLAN",
        L2_DOC,
        "interface",
    ),
    _r(
        "L2_NO_PORTFAST_BPDUGUARD",
        r"^no spanning-tree bpduguard",
        "fail",
        "gỡ BPDU guard",
        L2_DOC,
        "interface",
    ),
    # ---- ACL
    _r(
        "ACL_PERMIT_ANY_ANY",
        r"^(\d+ )?permit ip any any$",
        "warn",
        "permit ip any any: ACL không lọc gì",
        f"{CIS_DOC} 3.2 (biên)",
        "acl",
    ),
)

_MODE_ENTER = (
    (re.compile(r"^interface "), "interface"),
    (re.compile(r"^line "), "line"),
    (re.compile(r"^ip access-list "), "acl"),
    (re.compile(r"^vlan \d+"), "vlan"),
)
_MODE_EXIT = re.compile(r"^(exit|end)$")

# Chỉ các LỆNH mang credential mới phải dùng placeholder <SECRET:ref>. Trước đây quét "key/secret/password"
# ở mọi dòng, nên description "Key card reader" hay tên "KEY-ROOM" bị FAIL nhầm (review 01/10).
# Lệnh mang credential: username … secret/password, enable secret/password, tacacs/radius key, snmp community,
# key-string, ghi chú `! TACACS+ key:` của template AAA, và `key …` trong sub-mode tacacs/radius server.
_CRED_COMMAND = re.compile(
    r"^(username \S+ .*\b(secret|password)\b|enable (secret|password)\b|(tacacs|radius)-server .*\bkey\b"
    r"|key(-string)?\b|password\b|snmp-server community\b|! TACACS\+ key:)",
    re.IGNORECASE,
)
# Mọi giá trị đứng sau từ khoá credential trong dòng (kiểm TẤT CẢ, không chỉ lần xuất hiện đầu).
_SECRET_VALUE = re.compile(
    r"\b(key|key-string|secret|password|community):?( \d)? (?P<value>\S+)", re.IGNORECASE
)


def check_cis(device: str, text: str, secret_values: Iterable[str] = ()) -> list[Finding]:
    out: list[Finding] = []
    mode = "global"
    secrets = [s for s in secret_values if s]
    for no, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        if _MODE_EXIT.match(line):
            mode = "global"
        for pat, new_mode in _MODE_ENTER:
            if pat.match(line):
                mode = new_mode
        if line.startswith("!"):
            # Comment vẫn bị quét secret: template AAA in `! TACACS+ key: <SECRET:ref>`.
            out += _secret_findings(device, no, line, secrets)
            continue
        for rule in RULES:
            if rule.mode is not None and rule.mode != mode:
                continue
            if rule.pattern.search(line):
                out.append(
                    Finding(
                        sensor=SENSOR,
                        code=rule.code,
                        severity=rule.severity,
                        device=device,
                        line_no=no,
                        message=rule.message,
                        reference=rule.reference,
                    )
                )
        out += _secret_findings(device, no, line, secrets)
    return out


def _secret_findings(device: str, no: int, line: str, secrets: list[str]) -> list[Finding]:
    out: list[Finding] = []
    if any(s in line for s in secrets):
        out.append(
            Finding(
                sensor=SENSOR,
                code="CIS_1_4_SECRET_VALUE_IN_CONFIG",
                severity="fail",
                device=device,
                line_no=no,
                message="giá trị secret của inventory xuất hiện trong cấu hình",
                reference=f"{CIS_DOC} 1.4 (bảo vệ credential)",
            )
        )
        return out
    if _CRED_COMMAND.search(line):
        values = [m["value"] for m in _SECRET_VALUE.finditer(line)]
        if not values or not all(_PLACEHOLDER.match(v) for v in values):
            out.append(
                Finding(
                    sensor=SENSOR,
                    code="CIS_1_4_PLAINTEXT_SECRET",
                    severity="fail",
                    device=device,
                    line_no=no,
                    message="dòng chứa key/secret/password không phải placeholder <SECRET:ref>",
                    reference=f"{CIS_DOC} 1.4.x",
                )
            )
    return out

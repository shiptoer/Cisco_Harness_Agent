"""HTML của giao diện vận hành (D-057). Không JavaScript; mọi dữ liệu đi qua `esc` trước khi vào trang.

`esc` là chỗ DUY NHẤT biến dữ liệu thành HTML: test XSS gỡ nó ra (đối chứng âm) để chứng minh payload trong
description, banner, intent lọt được khi thiếu escape.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from harness_agent.levels import spec_of

from .app import DEVICE_RE, MAX_INTENT, MAX_REASON, UiApp
from .topology import render_svg

TITLE = "Duyệt cấu hình"
_STEPS = (
    ("i", "(i) Kiểm context"),
    ("ii", "(ii) Projection chỉ đọc"),
    ("g", "Intent grounding"),
    ("iii", "(iii) Gate G1–G6"),
    ("iv", "(iv) Sensors"),
    ("pkg", "Change package"),
)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# Đối số dán nguyên được vào PowerShell, cmd và bash: ký tự an toàn thì để trần; còn lại bọc ngoặc kép, trừ
# khi có ký tự mà một trong ba shell vẫn diễn giải bên trong ngoặc kép (`"`, `$`, backtick, `%`, `!`), ký tự
# điều khiển, hay `\` cuối (thoát dấu ngoặc trên Windows): khi đó dùng placeholder.
_BARE_ARG = re.compile(r"[A-Za-z0-9_.:/\\~+-]+")
_QUOTABLE_ARG = re.compile(r'[^"$`%!\x00-\x1f\x7f-\x9f]+')
RUN_DIR_PLACEHOLDER = "<run-dir>"


def shell_arg(value: str) -> str:
    if _BARE_ARG.fullmatch(value):
        return value
    if _QUOTABLE_ARG.fullmatch(value) and not value.endswith("\\"):
        return f'"{value}"'
    return RUN_DIR_PLACEHOLDER


def _duration(seconds: int) -> str:
    sign, seconds = ("-" if seconds < 0 else ""), abs(int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{sign}{days} ngày {hours} giờ"
    if hours:
        return f"{sign}{hours} giờ {minutes} phút"
    return f"{sign}{minutes} phút"


def layout(app: UiApp, title: str, body: str, *, refresh: int | None = None) -> str:
    s = app.settings
    mode = (
        '<dd class="mode-fake">fake (kịch bản giả, không gọi model)</dd>'
        if app.fake
        else "<dd>thật (gọi model)</dd>"
    )
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return (
        "<!doctype html><html lang='vi'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}<title>{esc(title)} | {TITLE}</title><link rel='stylesheet' href='/static/ui.css'></head>"
        f"<body>"
        "<a class='skip' href='#main'>Tới nội dung chính</a>"
        f"<header class='bar'><h1><a href='/'>{TITLE}</a></h1><dl>"
        f"<div><dt>Context</dt><dd class='mono'>{esc(app.context.context_id)}</dd></div>"
        f"<div><dt>Role</dt><dd>{esc(s.role)}</dd></div>"
        f"<div><dt>Mức</dt><dd>{esc(s.level.value)}</dd></div>"
        f"<div><dt>Chế độ</dt>{mode}</div>"
        f"<div><dt>Người duyệt</dt><dd>{esc(app.reviewer)}</dd></div>"
        "<div><dt>Deploy</dt><dd>không (chỉ xuất .cfg)</dd></div>"
        f"</dl></header><main id='main'>{body}</main></body></html>"
    )


def _notice(message: str | None, kind: str = "block") -> str:
    return f"<p class='notice {esc(kind)}' role='status'>{esc(message)}</p>" if message else ""


# ------------------------------------------------------------------ trang chính
def _freshness(app: UiApp) -> str:
    f = app.freshness_view()
    rows = [
        ("SHA-256 file context", f"<span class='hash'>{esc(app.context_sha256)}</span>"),
        ("Nguồn", esc(f["source"]) + ("" if f["declared"] else " (không có provenance)")),
    ]
    if f["status"] == "no_ttl":
        state = "<span class='tag plain'>không kiểm TTL</span>"
    else:
        rows.append(("Thu thập lúc", f"<span class='mono'>{esc(f['collected_at'])}</span>"))
        rows.append(("TTL", f"{esc(f['ttl_seconds'])} giây"))
        rows.append(("Tuổi", esc(_duration(f["age_seconds"]))))
        kind, text = {
            "fresh": ("ok", f"còn hạn, còn {_duration(f['remaining_seconds'])}"),
            "expiring": ("warn", f"sắp hết hạn, còn {_duration(f['remaining_seconds'])}"),
            "stale": ("block", "quá hạn: không chạy được"),
        }[f["status"]]
        state = f"<span class='tag {kind}'>{esc(text)}</span>"
        if f.get("detail"):
            rows.append(("Chi tiết", esc(f["detail"])))
    rows.insert(0, ("Snapshot", state))
    if app.violations:
        codes = ", ".join(sorted({v.code for v in app.violations}))
        rows.append(
            ("Invariant", f"<span class='tag block'>{len(app.violations)} vi phạm</span> {esc(codes)}")
        )
    body = "".join(f"<tr><th scope='row'>{k}</th><td>{v}</td></tr>" for k, v in rows)
    return f"<table class='kv'><tbody>{body}</tbody></table>"


def _devices(topo: dict[str, Any]) -> str:
    rows = []
    for d in topo["devices"]:
        rows.append(
            f"<tr><td class='mono'>{esc(d['hostname'])}</td><td>{esc(d['platform'])}</td>"
            f"<td>{esc(d['role'])}</td>"
            f"<td class='num'>{len(d['interfaces'])}</td>"
            f"<td class='mono'>{esc(', '.join(map(str, d['vlans'])))}</td>"
            f"<td class='mono'>{esc(', '.join(d['protected'])) or '<span class=muted>không</span>'}</td></tr>"
        )
    table = (
        "<table><thead><tr><th>Thiết bị</th><th>Platform</th><th>Role</th><th>Interface</th><th>VLAN</th>"
        f"<th>Cổng được bảo vệ</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )
    details = []
    for d in topo["devices"]:
        its = "".join(
            f"<tr><td class='mono'>{esc(i['name'])}</td><td>{esc(i['mode'])}</td>"
            f"<td>{'bật' if i['enabled'] else 'tắt'}</td><td class='num'>{esc(i['access_vlan'] or '')}</td>"
            f"<td class='untrusted'>{esc(i['description'])}</td></tr>"
            for i in d["interfaces"]
        )
        banner = (
            f"<p class='note'>Banner (dữ liệu tự do, không tin cậy):</p>"
            f"<pre class='wrap'>{esc(d['banner'])}</pre>"
            if d["banner"]
            else ""
        )
        details.append(
            f"<details><summary><span class='mono'>{esc(d['hostname'])}</span> "
            f"<span class='muted'>{len(d['interfaces'])} interface</span></summary>"
            "<table><thead><tr><th>Interface</th><th>Mode</th><th>Trạng thái</th><th>Access VLAN</th>"
            "<th>Description (không tin cậy)</th></tr></thead>"
            f"<tbody>{its}</tbody></table>{banner}</details>"
        )
    return table + "<h3>Interface theo thiết bị</h3>" + "".join(details)


def _run_form(app: UiApp, csrf: str, selected: str | None) -> str:
    blocker = app.run_blocker()
    disabled = " disabled" if blocker else ""
    head = _notice(blocker, "warn" if app.active else "block")
    if app.fake:
        options = "".join(
            f"<option value='{esc(n)}'{' selected' if n == selected else ''}>{esc(n)}</option>"
            for n in app.scenarios
        )
        chooser = (
            "<form method='get' action='/'><label for='scenario-pick'>Kịch bản</label><div class='row'>"
            f"<select id='scenario-pick' name='scenario'>{options}</select>"
            "<button type='submit'>Chọn kịch bản</button></div></form>"
        )
        sc = app.scenarios.get(selected or "")
        if sc is None:
            return (
                head
                + chooser
                + "<p class='note'>Chọn một kịch bản để xem intent của nó. Ở chế độ fake, kết quả do "
                "kịch bản quyết định, không do intent.</p>"
            )
        warn = []
        if sc.role != app.settings.role:
            warn.append(f"kịch bản viết cho role {sc.role}, lượt này chạy với role {app.settings.role}")
        if not sc.same_context:
            warn.append("kịch bản viết cho một file context khác")
        return (
            head
            + chooser
            + "<form method='post' action='/run'>"
            + f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
            + f"<input type='hidden' name='scenario' value='{esc(sc.name)}'>"
            + "<label for='intent'>Intent của kịch bản (chỉ đọc)</label>"
            + f"<textarea id='intent' readonly>{esc(sc.intent)}</textarea>"
            + "<p class='notice warn'>Kết quả do kịch bản quyết định, không do intent.</p>"
            + (_notice("; ".join(warn).capitalize() + ".", "warn") if warn else "")
            + f"<div class='row'><button class='primary' type='submit'{disabled}>Chạy kịch bản</button>"
            + "</div></form>"
        )
    return (
        head
        + "<form method='post' action='/run'>"
        + f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
        + "<label for='intent'>Intent</label>"
        + f"<textarea id='intent' name='intent' maxlength='{MAX_INTENT}' required></textarea>"
        + f"<p class='note'>Tối đa {MAX_INTENT} ký tự. "
        + "Role và mức cố định theo server, không đổi được từ đây.</p>"
        + f"<div class='row'><button class='primary' type='submit'{disabled}>Chạy</button></div></form>"
    )


def _history(app: UiApp) -> str:
    rows = app.history()
    if not rows:
        return "<p class='muted'>Chưa có lượt chạy nào. Nhập intent ở trên để chạy lượt đầu tiên.</p>"
    out = []
    for r in rows:
        out.append(
            f"<tr><td class='mono'><a href='/run/{esc(r['run_id'])}'>{esc(r['run_id'])}</a></td>"
            f"<td class='mono'>{esc(r['started_at'])}</td><td>{esc(r['level'])}</td>"
            f"<td>{esc(r['scenario'] or r['mode'])}</td><td>{esc(_clip(r['intent'], 90))}</td>"
            f"<td>{_outcome_tag(r)}</td><td class='mono'>{esc(r['stopped_by'] or r['error_code'] or '')}</td>"
            f"<td>{_review_tag(r['review'])}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Lượt</th><th>Bắt đầu (UTC)</th><th>Mức</th><th>Kịch bản / chế độ</th>"
        "<th>Intent</th>"
        f"<th>Kết quả</th><th>Dừng ở</th><th>Duyệt</th></tr></thead><tbody>{''.join(out)}</tbody></table>"
    )


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _outcome_tag(r: dict[str, Any]) -> str:
    if r.get("running"):
        return "<span class='tag plain'>đang chạy</span>"
    status, verdict = r.get("status"), r.get("verdict")
    if status is None:
        return "<span class='tag plain'>không có kết quả</span>"
    if status == "error":
        return f"<span class='tag block'>ERROR {esc(r.get('error_code'))}</span>"
    kind = {"accept": "ok", "reject": "block", "clarify": "warn"}.get(verdict or "", "plain")
    return f"<span class='tag {kind}'>{esc(verdict or status)}</span>"


def _review_tag(decision: str | None) -> str:
    if decision == "approve":
        return "<span class='tag ok'>đã duyệt</span>"
    if decision == "reject":
        return "<span class='tag block'>đã từ chối</span>"
    return "<span class='muted'>chưa</span>"


def dashboard(app: UiApp, csrf: str, selected: str | None = None, message: str | None = None) -> str:
    topo = app.topology()
    body = (
        _notice(message)
        + "<div class='cols'><section aria-labelledby='h-ctx'><h2 id='h-ctx'>Context</h2>"
        + _freshness(app)
        + "<h3>Topology</h3>"
        + render_svg(topo)
        + _devices(topo)
        + "</section><div><section aria-labelledby='h-run'><h2 id='h-run'>Lượt chạy mới</h2>"
        + _run_form(app, csrf, selected)
        + "</section>"
        + (
            f"<section><h2>Đang chạy</h2><p><a class='mono' href='/run/{esc(app.active)}'>{esc(app.active)}"
            f"</a></p>"
            "</section>"
            if app.active
            else ""
        )
        + "</div></div><section aria-labelledby='h-hist'><h2 id='h-hist'>Lịch sử</h2>"
        + _history(app)
        + "</section>"
    )
    return layout(app, "Trang chính", body, refresh=3 if app.active else None)


# ------------------------------------------------------------------ trang kết quả
def pipeline_steps(view: dict[str, Any], controls: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """(tên bước, lớp CSS, mô tả trạng thái) theo thứ tự pipeline; bước chặn được đánh dấu `is-block`."""
    result = view.get("result") or {}
    status, verdict, stopped = result.get("status"), result.get("verdict"), result.get("stopped_by") or ""
    if view.get("running") or not result:
        return [
            (name, "is-pending", "đang chạy" if view.get("running") else "chưa có kết quả")
            for _, name in _STEPS
        ]
    blocked_at = None
    if stopped in ("context_validation", "change_request_validation"):
        blocked_at = "i"
    elif stopped.startswith("G"):
        blocked_at = "iii"
    elif stopped.startswith("sensor_"):
        blocked_at = "iv"
    out = []
    reached = True
    for key, name in _STEPS:
        if key == "g":
            step = _grounding_step(view, result)
            out.append((name, *step))
            reached = reached and step[0] != "is-block"
            continue
        if key == "pkg":
            if status == "ok" and verdict == "accept":
                out.append((name, "is-pass", "đã sinh, chờ duyệt"))
            elif status == "error":
                out.append((name, "is-block", f"ERROR {result.get('error_code')}"))
            elif verdict == "clarify":
                out.append((name, "is-warn", "agent hỏi lại, không sinh"))
            else:
                out.append((name, "is-pending", "không sinh"))
            continue
        if key == blocked_at:
            label = stopped if key == "iii" else ("bị chặn: " + stopped)
            out.append((name, "is-block", f"chặn tại {label}" if key == "iii" else label))
            reached = False
            continue
        if key not in controls and not (key == "iii" and stopped in ("G1_whitelist", "G2_schema")):
            out.append((name, "is-off", "không bật ở mức này"))
            continue
        if not reached or (verdict == "clarify" and key in ("iii", "iv")) or status == "error":
            out.append((name, "is-pending", "chưa tới" if status != "error" else "không xác định"))
            continue
        out.append((name, "is-pass", "đã qua"))
    return out


def _grounding_step(view: dict[str, Any], result: dict[str, Any]) -> tuple[str, str]:
    """Bước intent grounding (D-061), đọc event `grounding` trong trace: đã qua / chặn / shadow / tắt."""
    grounding = view.get("grounding")
    if result.get("stopped_by") == "intent_grounding":
        n = grounding.get("violations") if isinstance(grounding, dict) else None
        return "is-block", f"chặn: {n} giá trị không neo được vào intent" if n else "chặn"
    if isinstance(grounding, dict):
        if not grounding.get("enforced"):
            n = grounding.get("violations") or 0
            return "is-off", f"shadow, không cưỡng chế ({n} vi phạm)" if n else "shadow, không cưỡng chế"
        n = grounding.get("violations") or 0
        if n:
            # Cưỡng chế, có vi phạm mà lượt chạy không dừng ở grounding (vd ERROR): không bao giờ "đã qua".
            return "is-block", f"{n} giá trị không neo được vào intent"
        # Event không vi phạm: bước này đã chạy và cho qua, kể cả khi lượt chạy ERROR ở bước sau.
        return "is-pass", "đã qua"
    # Không có event: mức không bật grounding, hoặc lượt chạy dừng trước khi CBA nộp đề xuất.
    try:
        enforced_here = spec_of(str(result.get("level"))).grounding
    except ValueError:
        enforced_here = False
    if not enforced_here:
        return "is-off", "tắt ở mức này"
    if result.get("status") == "error":
        return "is-pending", "không xác định"
    return "is-pending", "chưa tới"


def _pipeline(view: dict[str, Any], controls: tuple[str, ...]) -> str:
    items = "".join(
        f"<li class='{cls}'><span class='step-name'>{esc(name)}</span><span class='step-state'>{esc(state)}"
        f"</span></li>"
        for name, cls, state in pipeline_steps(view, controls)
    )
    return f"<ol class='pipeline' aria-label='Các control theo thứ tự pipeline'>{items}</ol>"


def _verdict_line(view: dict[str, Any]) -> str:
    result = view.get("result")
    if view.get("running"):
        return "<p class='notice' role='status'>Đang chạy. Trang tự tải lại mỗi 2 giây.</p>"
    if result is None:
        if view.get("exited_without_result"):
            code = view.get("exit_code")
            code_text = "bị dừng vì quá thời gian" if code == -1 else f"mã thoát {code}"
            log = view.get("runner_log") or ""
            log_html = (
                f"<p class='note'>Đuôi log của runner (đã che secret):</p><pre class='wrap'>{esc(log)}</pre>"
                if log
                else "<p class='note'>Runner không ghi gì ra log.</p>"
            )
            return (
                f"<p class='notice block'>Runner thoát mà không ghi result.json ({esc(code_text)}): "
                "lỗi cấu hình, thiếu API key hoặc dữ liệu case hỏng.</p>" + log_html
            )
        return "<p class='notice'>Chưa có kết quả.</p>"
    tag = _outcome_tag(
        {"status": result["status"], "verdict": result["verdict"], "error_code": result["error_code"]}
    )
    reason = f"<p>{esc(result.get('reason'))}</p>" if result.get("reason") else ""
    return f"<p>{tag} <span class='mono'>{esc(result.get('stopped_by') or '')}</span></p>{reason}"


def _gate(view: dict[str, Any]) -> str:
    gate = view.get("gate")
    if not gate:
        return ""
    rows = "".join(
        f"<tr class='{'failed' if not s.get('ok') else ''}'><td class='mono'>{esc(s.get('step'))}</td>"
        f"<td>{'đạt' if s.get('ok') else 'không đạt'}</td><td>{esc(s.get('reason'))}</td></tr>"
        for s in gate.get("steps", [])
    )
    return (
        "<section><h2>Gate</h2>"
        f"<p>Lời gọi bị chặn: <code>{esc(json.dumps(gate.get('call'), ensure_ascii=False))}</code></p>"
        "<table><thead><tr><th>Bước</th><th>Kết quả</th><th>Lý do</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></section>"
    )


def _change_request(view: dict[str, Any]) -> str:
    cr = view.get("change_request")
    if not cr:
        return ""
    rows = "".join(
        f"<tr><td class='num'>{n}</td><td class='mono'>{esc(c.get('tool'))}</td>"
        f"<td class='mono'>{esc(json.dumps(c.get('args'), ensure_ascii=False))}</td></tr>"
        for n, c in enumerate(cr.get("changes") or [], start=1)
    )
    question = (
        f"<p>Agent hỏi lại: <q>{esc(cr['clarification_question'])}</q></p>"
        if cr.get("clarification_question")
        else ""
    )
    rationale = (
        f"<p class='note'>Giải thích của model (không tin cậy):</p><pre class='wrap'>{esc(cr['rationale'])}"
        f"</pre>"
        if cr.get("rationale")
        else ""
    )
    table = (
        f"<table><thead><tr><th>#</th><th>Tool</th><th>Tham số</th></tr></thead><tbody>{rows}</tbody></table>"
        if rows
        else ""
    )
    return f"<section><h2>Change request ({esc(cr.get('status'))})</h2>{table}{question}{rationale}</section>"


def _sensors(view: dict[str, Any]) -> str:
    s = view.get("sensors")
    if not s:
        return ""
    ran = ", ".join(f"{k}: {v}" for k, v in (s.get("sensors") or {}).items())
    findings = "".join(
        f"<tr class='{'failed' if f.get('severity') == 'fail' else ''}'><td>{esc(f.get('sensor'))}</td>"
        f"<td class='mono'>{esc(f.get('code'))}</td><td>{esc(f.get('severity'))}</td><td class='mono'>"
        f"{esc(f.get('device'))}:{esc(f.get('line_no') or '-')}</td><td>{esc(f.get('message'))}</td></tr>"
        for f in s.get("findings") or []
    )
    mode = "cưỡng chế" if s.get("enforced") else "chỉ ghi lại (shadow)"
    table = (
        "<table><thead><tr><th>Sensor</th><th>Mã</th><th>Mức</th><th>Vị trí</th><th>Nội dung</th></tr>"
        "</thead>"
        f"<tbody>{findings}</tbody></table>"
        if findings
        else "<p class='muted'>Không có finding.</p>"
    )
    return f"<section><h2>Sensors ({esc(mode)})</h2><p class='note'>{esc(ran)}</p>{table}</section>"


def _cli(view: dict[str, Any]) -> str:
    if view.get("package_hidden"):
        return (
            "<section><h2>Cấu hình</h2>"
            "<p class='notice block'>Change package chứa giá trị secret của context "
            "(leak_in_package). Không hiển thị và không cho duyệt.</p></section>"
        )
    parts = []
    for device, text in (view.get("candidates") or {}).items():
        parts.append(f"<h3 class='mono'>{esc(device)}.cfg</h3><pre>{esc(text)}</pre>")
    for device, text in (view.get("sensor_rejected") or {}).items():
        parts.append(f"<h3 class='mono block'>{esc(device)}.cfg (bị sensor chặn)</h3><pre>{esc(text)}</pre>")
    if not parts:
        return ""
    return "<section><h2>Cấu hình sinh ra</h2>" + "".join(parts) + "</section>"


def _apply_hint(view: dict[str, Any], review: dict[str, Any]) -> str:
    """Lệnh `cha-apply` dry-run cho từng thiết bị đã duyệt (D-064), để người vận hành tự chạy ở terminal.
    UI không biết inventory nên in placeholder, và không có đường POST nào chạy apply."""
    run_dir = str(view.get("run_dir") or "")
    arg = shell_arg(run_dir) if run_dir else RUN_DIR_PLACEHOLDER
    devices = [d for d in (review.get("candidates") or {}) if isinstance(d, str) and DEVICE_RE.match(d)]
    if not devices:
        return ""
    lines = "".join(
        f"<pre>cha-apply --run-dir {esc(arg)} --device {esc(d)} --inventory &lt;inventory.json&gt;</pre>"
        for d in devices
    )
    where = (
        f"<p class='note'>Thay <code>{esc(RUN_DIR_PLACEHOLDER)}</code> bằng run-dir: "
        f"<span class='mono'>{esc(run_dir)}</span></p>"
        if arg == RUN_DIR_PLACEHOLDER and run_dir
        else ""
    )
    return (
        "<h3>Áp cấu hình (chạy ở terminal; UI không kết nối thiết bị)</h3>"
        "<p class='note'>Lệnh dưới đây chỉ dry-run: kiểm điều kiện và in các dòng sẽ gửi, không kết nối. "
        "Thay <code>&lt;inventory.json&gt;</code> bằng inventory của lab (ví dụ "
        "<code>lab/smb-01/inventory.json</code>). Áp thật: đọc kết quả dry-run rồi thêm "
        f"<code>--commit --confirm {esc(view.get('run_id'))}</code> (D-064).</p>" + where + lines
    )


def _review(app: UiApp, view: dict[str, Any], csrf: str) -> str:
    review = view.get("review")
    run_id = view["run_id"]
    if review:
        exports = ""
        if review.get("decision") == "approve":
            exports = "".join(
                f"<li><a class='mono' href='/run/{esc(run_id)}/cfg/{esc(d)}'>{esc(d)}.cfg</a> "
                f"<span class='hash'>{esc(h)}</span></li>"
                for d, h in (review.get("candidates") or {}).items()
            )
            exports = f"<h3>Xuất cấu hình (không deploy)</h3><ul>{exports}</ul>" + _apply_hint(view, review)
        return (
            f"<section><h2>Duyệt</h2>"
            f"<p>{_review_tag(review.get('decision'))} bởi {esc(review.get('reviewer'))} lúc "
            f"<span class='mono'>{esc(review.get('reviewed_at'))}</span></p>"
            + (f"<p>Lý do: {esc(review.get('reason'))}</p>" if review.get("reason") else "")
            + "<p class='note'>SHA-256 change package: "
            + f"<span class='hash'>{esc(review.get('package_sha256'))}</span>"
            "</p>" + exports + "</section>"
        )
    blocker = app.review_blocker(run_id, view) if view.get("finished") else "Lượt chạy chưa xong."
    if blocker:
        return f"<section><h2>Duyệt</h2>{_notice(blocker, 'warn')}</section>"
    return (
        "<section><h2>Duyệt</h2>"
        f"<form method='post' action='/run/{esc(run_id)}/review'>"
        f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
        f"<label for='reason'>Lý do (bắt buộc khi từ chối, tối đa {MAX_REASON} ký tự)</label>"
        f"<textarea id='reason' name='reason' maxlength='{MAX_REASON}'></textarea>"
        "<div class='row'>"
        "<button class='approve' type='submit' name='decision' value='approve'>Duyệt change package"
        "</button><button class='reject' type='submit' name='decision' value='reject'>Từ chối</button></div>"
        "<p class='note'>Bản duyệt được ghi vào trace.jsonl "
        f"(actor reviewer, người duyệt {esc(app.reviewer)}). "
        "Duyệt chỉ cho phép xuất .cfg; hệ thống không deploy.</p></form></section>"
    )


def _progress(view: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td class='mono'>{esc(p.get('ts'))}</td><td>{esc(p.get('actor'))}</td><td class='mono'>"
        f"{esc(p.get('event'))}</td><td>{esc(p.get('stage') or '')}</td></tr>"
        for p in view.get("progress", [])
    )
    # D-060: trace của lượt chạy có `sig` HMAC bằng khoá runner chỉ giữ trong bộ nhớ. UI không có khoá: chỉ
    # kiểm được liên kết SHA-256, không kiểm chữ ký (runner kiểm chữ ký trước khi render). "ok" và
    # "bad_signature" chỉ xuất hiện khi trạng thái được kiểm bằng khoá; vẫn hiển thị đúng nếu gặp.
    status = view.get("chain_status")
    if status == "ok":
        chain = "<span class='tag ok'>trace đã ký; chữ ký hợp lệ</span>"
    elif status == "bad_signature":
        chain = "<span class='tag block'>chữ ký HMAC sai: trace không đáng tin, không duyệt</span>"
    elif status == "missing":
        chain = "<span class='tag block'>chưa có trace</span>"
    elif status != "unsigned" or not view.get("chain_ok"):
        chain = "<span class='tag block'>chuỗi hash không hợp lệ (trace bị sửa, xoá hoặc chèn)</span>"
    elif view.get("trace_signed"):
        chain = (
            "<span class='tag ok'>liên kết hash hợp lệ; chữ ký không kiểm (UI không giữ khoá)</span> "
            "<span class='tag'>trace đã ký</span>"
        )
    else:
        chain = (
            "<span class='tag ok'>chuỗi hash hợp lệ</span> "
            "<span class='tag'>trace chưa ký (không khoá)</span>"
        )
    return (
        f"<section><h2>Trace</h2><p>{chain}</p><table><thead><tr><th>Thời điểm</th><th>Actor</th><th>Event"
        f"</th>"
        f"<th>Stage</th></tr></thead><tbody>{rows}</tbody></table></section>"
    )


def run_page(app: UiApp, view: dict[str, Any], csrf: str, message: str | None = None) -> str:
    row = view["row"]
    result = view.get("result") or {}
    controls = tuple(result.get("controls") or app.level_controls())
    snap = view.get("snapshot") or {}
    leak = (
        f"<p>Leak: <span class='tag {'block' if result.get('leak_in_package') else 'plain'}'>"
        f"{'secret trong package' if result.get('leak_in_package') else 'không có trong package'}</span> "
        f"<span class='note'>nguồn ghi nhận: {esc(', '.join(view.get('leak_kinds') or []) or 'không')}"
        f"</span></p>"
        if result
        else ""
    )
    body = (
        _notice(message)
        + f"<section><h2>Lượt <span class='mono'>{esc(view['run_id'])}</span></h2>"
        + _verdict_line(view)
        + _pipeline(view, controls)
        + "<table class='kv'><tbody>"
        + f"<tr><th scope='row'>Intent</th><td><pre class='wrap'>{esc(row.get('intent'))}</pre></td></tr>"
        + f"<tr><th scope='row'>Kịch bản</th><td>{esc(row.get('scenario') or 'không (chế độ thật)')}</td>"
        + "</tr>"
        + "<tr><th scope='row'>evaluated_at (đồng hồ UI)</th>"
        + f"<td class='mono'>{esc(row.get('evaluated_at'))}</td></tr>"
        + f"<tr><th scope='row'>Context</th><td><span class='mono'>{esc(snap.get('context_id'))}</span> "
        + f"<span class='hash'>{esc(snap.get('sha256'))}</span> freshness: {esc(snap.get('freshness'))}"
        + "</td></tr>"
        + "</tbody></table>"
        + leak
        + "</section>"
        + _gate(view)
        + _change_request(view)
        + _sensors(view)
        + _cli(view)
        + _review(app, view, csrf)
        + _progress(view)
    )
    return layout(app, f"Lượt {view['run_id']}", body, refresh=2 if view.get("running") else None)


def error_page(status: int, message: str) -> str:
    return (
        "<!doctype html><html lang='vi'><head><meta charset='utf-8'><title>Lỗi</title>"
        "<link rel='stylesheet' href='/static/ui.css'></head><body><main>"
        f"<section><h2>{esc(status)}</h2><p>{esc(message)}</p><p><a href='/'>Về trang chính</a></p></section>"
        "</main></body></html>"
    )

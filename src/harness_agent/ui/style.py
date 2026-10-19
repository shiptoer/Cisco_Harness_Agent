"""CSS của giao diện vận hành, phục vụ ở `/static/ui.css` (CSP `style-src 'self'`: không CSS nội tuyến).

Màu nền và chữ trung tính, lạnh; màu chỉ mang nghĩa trạng thái: xanh = accept / đã qua, đỏ = reject / bị chặn,
vàng = clarify / snapshot sắp hết hạn. Font của hệ thống (CSP không cho tải font ngoài); monospace chỉ cho
CLI,
cấu hình, hash. Sáng/tối theo `prefers-color-scheme`; focus nhìn thấy được khi dùng bàn phím.
"""

CSS = """
:root {
  color-scheme: light dark;
  --bg: #eef1f4; --surface: #ffffff; --ink: #18212b; --muted: #56626e; --rule: #cfd6dd;
  --rule-strong: #9aa5b0;
  --ok: #1b7339; --ok-bg: #e2f2e7; --block: #b3261e; --block-bg: #fbe8e6; --warn: #845400; --warn-bg: #fbf0d4;
  --focus: #1d5fc4; --code-bg: #f6f8fa;
  --sans: "Segoe UI", system-ui, -apple-system, "Noto Sans", "Helvetica Neue", Arial, sans-serif;
  --mono: "Cascadia Mono", Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1419; --surface: #161d24; --ink: #e2e8ee; --muted: #98a4b0; --rule: #2a343e;
  --rule-strong: #4a5663;
    --ok: #57c47c; --ok-bg: #12301d; --block: #ff8077; --block-bg: #3b1613; --warn: #e2b444;
  --warn-bg: #33280c;
    --focus: #7cb6ff; --code-bg: #0f151b;
  }
}
* { box-sizing: border-box; }
html { font-size: 14px; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--sans); line-height: 1.45; }
a { color: var(--focus); }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.skip { position: absolute; left: -999px; top: 0; }
.skip:focus { left: 8px; top: 8px; background: var(--surface); padding: 6px 10px; z-index: 2; }

.bar { display: flex; flex-wrap: wrap; gap: 4px 28px; align-items: baseline; padding: 10px 20px;
  background: var(--surface); border-bottom: 1px solid var(--rule); }
.bar h1 { font-size: 15px; font-weight: 650; margin: 0 12px 0 0; }
.bar h1 a { color: inherit; text-decoration: none; }
.bar dl { display: flex; flex-wrap: wrap; gap: 4px 20px; margin: 0; }
.bar dt { color: var(--muted); display: inline; margin-right: 6px; }
.bar dd { display: inline; margin: 0; }
.mode-fake { color: var(--warn); font-weight: 600; }

main { padding: 16px 20px 40px; max-width: 1480px; }
.cols { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 16px;
  align-items: start; }
@media (max-width: 1080px) { .cols { grid-template-columns: 1fr; } }
section { background: var(--surface); border: 1px solid var(--rule); padding: 12px 14px 14px;
  margin-bottom: 16px; }
h2 { font-size: 14px; font-weight: 650; margin: 0 0 8px; }
h3 { font-size: 13px; font-weight: 650; margin: 14px 0 6px; }
p { margin: 6px 0; max-width: 80ch; }
.muted { color: var(--muted); }
.note { color: var(--muted); font-size: 12.5px; }

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; vertical-align: top; padding: 4px 8px; border-bottom: 1px solid var(--rule); }
th { font-weight: 600; color: var(--muted); border-bottom-color: var(--rule-strong); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:hover { background: color-mix(in srgb, var(--rule) 30%, transparent); }

code, pre, .mono { font-family: var(--mono); font-size: 12.5px; }
pre { background: var(--code-bg); border: 1px solid var(--rule); padding: 8px 10px; margin: 6px 0;
  overflow-x: auto; white-space: pre; }
pre.wrap { white-space: pre-wrap; overflow-wrap: anywhere; }
.hash { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }

.tag { display: inline-block; padding: 0 6px; border: 1px solid currentColor; border-radius: 2px;
  font-size: 12px; font-weight: 600; line-height: 1.6; white-space: nowrap; }
.ok { color: var(--ok); }
.block { color: var(--block); }
.warn { color: var(--warn); }
.tag.ok { background: var(--ok-bg); }
.tag.block { background: var(--block-bg); }
.tag.warn { background: var(--warn-bg); }
.tag.plain { color: var(--muted); }

.notice { border-left: 3px solid var(--rule-strong); padding: 6px 10px; margin: 8px 0;
  background: var(--surface); }
.notice.block { border-left-color: var(--block); background: var(--block-bg); color: var(--ink); }
.notice.warn { border-left-color: var(--warn); background: var(--warn-bg); color: var(--ink); }
.notice.ok { border-left-color: var(--ok); background: var(--ok-bg); color: var(--ink); }

/* Dải pipeline: điểm nhấn duy nhất của trang kết quả. */
.pipeline { list-style: none; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 0;
  margin: 4px 0 12px; padding: 0; border: 1px solid var(--rule); }
@media (max-width: 860px) { .pipeline { grid-template-columns: 1fr; } }
.pipeline li { padding: 8px 10px; border-right: 1px solid var(--rule); position: relative; }
.pipeline li:last-child { border-right: 0; }
.pipeline .step-name { font-weight: 650; display: block; }
.pipeline .step-state { font-size: 12.5px; display: block; }
.pipeline .is-pass { border-top: 3px solid var(--ok); }
.pipeline .is-pass .step-state { color: var(--ok); }
.pipeline .is-off { border-top: 3px dashed var(--rule-strong); color: var(--muted); }
.pipeline .is-pending { border-top: 3px solid var(--rule); color: var(--muted); }
.pipeline .is-block { border-top: 3px solid var(--block); background: var(--block-bg);
  outline: 2px solid var(--block);
  outline-offset: -2px; }
.pipeline .is-block .step-state { color: var(--block); font-weight: 650; }
.pipeline .is-warn { border-top: 3px solid var(--warn); }
.pipeline .is-warn .step-state { color: var(--warn); }
tr.failed td { color: var(--block); font-weight: 600; }

form { margin: 0; }
label { display: block; font-weight: 600; margin: 8px 0 4px; }
textarea, select, input[type=text] { width: 100%; font: inherit; color: var(--ink);
  background: var(--surface);
  border: 1px solid var(--rule-strong); padding: 6px 8px; border-radius: 2px; }
textarea { min-height: 7em; resize: vertical; }
textarea[readonly] { background: var(--code-bg); color: var(--muted); }
.row { display: flex; gap: 8px; align-items: end; flex-wrap: wrap; margin-top: 8px; }
.row > select { flex: 1 1 260px; width: auto; }
button { font: inherit; font-weight: 600; padding: 6px 14px; border-radius: 2px; cursor: pointer;
  border: 1px solid var(--rule-strong); background: var(--surface); color: var(--ink); }
button.primary { background: var(--ink); color: var(--surface); border-color: var(--ink); }
button.approve { border-color: var(--ok); color: var(--ok); }
button.reject { border-color: var(--block); color: var(--block); }
button:disabled { opacity: .45; cursor: not-allowed; }

details { border-bottom: 1px solid var(--rule); padding: 4px 0; }
details > summary { cursor: pointer; padding: 2px 0; }
details > summary .mono { font-weight: 600; }
.untrusted { font-family: var(--mono); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }

.topo { max-width: 100%; height: auto; display: block; margin: 4px 0 8px; }
.topo-link { stroke: var(--rule-strong); stroke-width: 1.5; }
.topo-dev rect { fill: var(--surface); stroke: var(--ink); stroke-width: 1.2; }
.topo-host { font: 650 13px var(--sans); fill: var(--ink); }
.topo-meta { font: 11px var(--sans); fill: var(--muted); }
.topo-if { font: 11px var(--mono); fill: var(--muted); }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""

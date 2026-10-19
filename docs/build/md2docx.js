// Chuyển một file Markdown của docs/ thành .docx bằng docx-js (marked lexer → Paragraph/Table).
// Dùng (ở gốc repo, cần Node + gói npm `docx` và `marked`):
//   node docs/build/md2docx.js docs/HUONG_DAN_SU_DUNG.md docs/HUONG_DAN_SU_DUNG.docx
// Chép từ bản 01/10/2026 (docs/build/md2docx.js của bản project); chỉ đổi chữ trên trang bìa và header (COVER_*).
const fs = require("fs");
const { marked } = require("marked");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell, WidthType,
  ShadingType, BorderStyle, AlignmentType, LevelFormat, TableOfContents, Header, Footer,
  PageNumber, PageBreak, ExternalHyperlink,
} = require("docx");

const [, , input, output] = process.argv;
const md = fs.readFileSync(input, "utf8");
const tokens = marked.lexer(md);

const FONT = "Arial";
const MONO = "Consolas";
const CONTENT_W = 9638; // A4 (11906) - 2 × 1134 (2 cm)
const ACCENT = "1F4E79";
const CODE_BG = "F2F4F7";
const HEAD_BG = "DCE6F1";
// Chữ trên trang bìa và header. Sửa ở đây khi phát hành bản mới; bản Markdown vẫn là bản gốc.
const COVER_VERSION = "Phiên bản 17/10/2026 · nhánh feat/complete-2026-10-16 · 1910 test, quyết định D-001..D-065";
const COVER_STATUS = "Trạng thái: D-058..D-065 ĐỀ XUẤT (chờ nhóm/GVHD duyệt); chưa có số liệu model thật sau các thay đổi này";
const HEADER_TEXT = "harness-agent · Hướng dẫn sử dụng";

// ---------------------------------------------------------------- inline
function decode(s) {
  return s.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

function inlineRuns(toks, style = {}) {
  const out = [];
  for (const t of toks || []) {
    switch (t.type) {
      case "text":
        if (t.tokens && t.tokens.length) out.push(...inlineRuns(t.tokens, style));
        else out.push(new TextRun({ text: decode(t.text), ...style }));
        break;
      case "escape":
        out.push(new TextRun({ text: decode(t.text), ...style }));
        break;
      case "strong":
        out.push(...inlineRuns(t.tokens, { ...style, bold: true }));
        break;
      case "em":
        out.push(...inlineRuns(t.tokens, { ...style, italics: true }));
        break;
      case "codespan":
        out.push(new TextRun({ text: decode(t.text), ...style, font: MONO, size: 18, color: "8B1A1A" }));
        break;
      case "link":
        // Liên kết nội bộ (#…) chỉ giữ chữ; liên kết ngoài thành hyperlink.
        if (t.href.startsWith("http")) {
          out.push(new ExternalHyperlink({ link: t.href, children: inlineRuns(t.tokens, { ...style, style: "Hyperlink" }) }));
        } else {
          out.push(...inlineRuns(t.tokens, style));
        }
        break;
      case "br":
        out.push(new TextRun({ text: "", break: 1 }));
        break;
      case "del":
        out.push(...inlineRuns(t.tokens, { ...style, strike: true }));
        break;
      default:
        if (t.text) out.push(new TextRun({ text: decode(t.text), ...style }));
    }
  }
  return out;
}

function plain(toks) {
  return (toks || []).map((t) => (t.tokens ? plain(t.tokens) : decode(t.text || ""))).join("");
}

// ---------------------------------------------------------------- blocks
const children = [];
const HEADINGS = { 1: HeadingLevel.HEADING_1, 2: HeadingLevel.HEADING_2, 3: HeadingLevel.HEADING_3, 4: HeadingLevel.HEADING_4 };

function codeBlock(text) {
  const lines = text.replace(/\t/g, "    ").split("\n");
  const longest = Math.max(...lines.map((l) => l.length));
  const size = longest > 112 ? 12 : longest > 100 ? 13 : 15; // half-point: khối rộng thì chữ nhỏ hơn để không xuống dòng
  return lines.map((line, i) =>
    new Paragraph({
      shading: { type: ShadingType.CLEAR, color: "auto", fill: CODE_BG },
      spacing: { before: i === 0 ? 80 : 0, after: i === lines.length - 1 ? 120 : 0, line: 240 },
      indent: { left: 113, right: 113 },
      border: i === 0 ? { top: { style: BorderStyle.SINGLE, size: 2, color: "C9D1DB", space: 4 } } :
        (i === lines.length - 1 ? { bottom: { style: BorderStyle.SINGLE, size: 2, color: "C9D1DB", space: 4 } } : undefined),
      children: [new TextRun({ text: line || " ", font: MONO, size })],
    }),
  );
}

function table(t) {
  const ncol = t.header.length;
  // Độ rộng cột theo độ dài nội dung (căn bậc hai để cột dài không nuốt hết), tối thiểu 900 DXA.
  const lens = Array.from({ length: ncol }, (_, c) => {
    const cells = [t.header[c], ...t.rows.map((r) => r[c])];
    return Math.max(...cells.map((cell) => plain(cell.tokens).length), 3);
  });
  const weights = lens.map((l) => Math.sqrt(l));
  const sum = weights.reduce((a, b) => a + b, 0);
  let widths = weights.map((w) => Math.max(900, Math.round((w / sum) * CONTENT_W)));
  const over = widths.reduce((a, b) => a + b, 0) - CONTENT_W;
  if (over !== 0) {
    const iMax = widths.indexOf(Math.max(...widths));
    widths[iMax] -= over;
  }
  const border = { style: BorderStyle.SINGLE, size: 4, color: "B4BEC9" };
  const borders = { top: border, bottom: border, left: border, right: border };
  const mkCell = (cell, c, header) =>
    new TableCell({
      width: { size: widths[c], type: WidthType.DXA },
      borders,
      shading: header ? { type: ShadingType.CLEAR, color: "auto", fill: HEAD_BG } : undefined,
      margins: { top: 50, bottom: 50, left: 90, right: 90 },
      children: [new Paragraph({ spacing: { before: 0, after: 0 }, children: inlineRuns(cell.tokens, { size: 17, bold: header || undefined }) })],
    });
  const rows = [
    new TableRow({ tableHeader: true, children: t.header.map((cell, c) => mkCell(cell, c, true)) }),
    ...t.rows.map((r) => new TableRow({ cantSplit: true, children: r.map((cell, c) => mkCell(cell, c, false)) })),
  ];
  return new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: widths, rows });
}

function list(t, level = 0) {
  const out = [];
  const ref = t.ordered ? `num-${numCounter++}` : "bullets";
  if (t.ordered) numberingRefs.push(ref);
  for (const item of t.items) {
    let first = true;
    for (const sub of item.tokens) {
      if (sub.type === "text" || sub.type === "paragraph") {
        out.push(new Paragraph({
          numbering: first ? { reference: ref, level } : undefined,
          indent: first ? undefined : { left: 720 * (level + 1) },
          spacing: { before: 20, after: 40 },
          children: inlineRuns(sub.tokens || [{ type: "text", text: sub.text }]),
        }));
        first = false;
      } else if (sub.type === "list") {
        out.push(...list(sub, level + 1));
      } else if (sub.type === "code") {
        out.push(...codeBlock(sub.text));
      }
    }
  }
  return out;
}

let numCounter = 0;
const numberingRefs = [];
let skipToc = false;
let seenH2 = false;
const intro = [];
const tocEntries = []; // [depth, text]

for (const t of tokens) {
  if (t.type === "heading") {
    const txt = plain(t.tokens);
    if (txt === "Mục lục") { skipToc = true; continue; }
    skipToc = false;
    if (t.depth === 1) continue; // tiêu đề tài liệu đã ở trang bìa
    if (t.depth === 2) seenH2 = true;
    if (t.depth <= 3) tocEntries.push([t.depth, txt]);
    // Mỗi chương (H2) bắt đầu trang mới.
    children.push(new Paragraph({ heading: HEADINGS[t.depth - 1] || HeadingLevel.HEADING_3, pageBreakBefore: t.depth === 2, children: inlineRuns(t.tokens) }));
    continue;
  }
  if (skipToc) continue;
  const sink = seenH2 ? children : intro;
  const before = sink.length;
  switch (t.type) {
    case "paragraph":
      children.push(new Paragraph({ spacing: { before: 60, after: 100 }, children: inlineRuns(t.tokens) }));
      break;
    case "code":
      children.push(...codeBlock(t.text));
      break;
    case "table":
      children.push(table(t));
      children.push(new Paragraph({ spacing: { before: 0, after: 80 }, children: [] }));
      break;
    case "list":
      children.push(...list(t));
      break;
    case "blockquote":
      for (const sub of t.tokens) {
        children.push(new Paragraph({
          indent: { left: 360 },
          border: { left: { style: BorderStyle.SINGLE, size: 18, color: ACCENT, space: 8 } },
          spacing: { before: 60, after: 60 },
          children: inlineRuns(sub.tokens, { italics: true, color: "404040" }),
        }));
      }
      break;
    case "hr":
    case "space":
      break;
    default:
      if (t.text) children.push(new Paragraph({ children: [new TextRun(decode(t.text))] }));
  }
  if (!seenH2) intro.push(...children.splice(0));
}

// ---------------------------------------------------------------- trang bìa + mục lục
const h1 = tokens.find((t) => t.type === "heading" && t.depth === 1);
const title = h1 ? plain(h1.tokens) : "Hướng dẫn";
const cover = [
  new Paragraph({ spacing: { before: 2600, after: 200 }, alignment: AlignmentType.LEFT, children: [new TextRun({ text: "IAP491 · Đồ án tốt nghiệp", color: "666666", size: 22 })] }),
  new Paragraph({ spacing: { after: 120 }, border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: ACCENT, space: 8 } }, children: [new TextRun({ text: title, bold: true, size: 48, color: ACCENT })] }),
  new Paragraph({ spacing: { before: 200, after: 120 }, children: [new TextRun({ text: "LLM agent sinh cấu hình Cisco IOS, được bảo vệ bởi harness tất định", size: 26 })] }),
  new Paragraph({ spacing: { before: 600 }, children: [new TextRun({ text: COVER_VERSION, size: 20, color: "444444" })] }),
  new Paragraph({ children: [new TextRun({ text: COVER_STATUS, size: 20, color: "444444" })] }),
  new Paragraph({ children: [new TextRun({ text: `Nguồn: ${input.replace(/\\/g, "/")} trong repo (bản Markdown là bản gốc)`, size: 20, color: "444444" })] }),
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("Giới thiệu")] }),
  ...intro,
  new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 360 }, children: [new TextRun("Mục lục")] }),
  // Mục lục tĩnh: hiện đúng ở mọi trình xem (field TOC của Word để trống cho tới khi người dùng cập nhật).
  ...tocEntries.map(([d, txt]) => new Paragraph({
    indent: { left: d === 2 ? 0 : 480 },
    spacing: { before: d === 2 ? 100 : 0, after: 0 },
    children: [new TextRun({ text: txt, bold: d === 2, size: d === 2 ? 20 : 18, color: d === 2 ? ACCENT : "333333" })],
  })),
];

const numberingConfig = [
  { reference: "bullets", levels: [0, 1, 2].map((l) => ({ level: l, format: LevelFormat.BULLET, text: ["•", "◦", "▪"][l], alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720 * (l + 1), hanging: 360 } } } })) },
  ...numberingRefs.map((ref) => ({ reference: ref, levels: [0, 1].map((l) => ({ level: l, format: l === 0 ? LevelFormat.DECIMAL : LevelFormat.LOWER_LETTER, text: l === 0 ? "%1." : "%2)", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720 * (l + 1), hanging: 360 } } } })) })),
];

const doc = new Document({
  creator: "harness-agent",
  title,
  styles: {
    default: { document: { run: { font: FONT, size: 20 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 32, bold: true, font: FONT, color: ACCENT }, paragraph: { spacing: { before: 240, after: 160 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 26, bold: true, font: FONT, color: ACCENT }, paragraph: { spacing: { before: 220, after: 100 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 22, bold: true, font: FONT, color: "2E2E2E" }, paragraph: { spacing: { before: 180, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: { config: numberingConfig },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: HEADER_TEXT, size: 16, color: "888888" })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: ["Trang ", PageNumber.CURRENT, " / ", PageNumber.TOTAL_PAGES], size: 16, color: "888888" })] })] }) },
    children: [...cover, ...children],
  }],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(output, buf); console.log("wrote", output, buf.length, "bytes"); });

"""Kiểm tra rò nhãn (D-025): những gì model nhìn thấy không được mang tên case hay nhãn đáp án.

Tên case (`adv-protected-uplink`, `clean-vlan30`…) và các khoá của file case (`expect`, `stopped_by`,
`fake_scenario`) nói cho model biết nó đang ở loại case nào và đáp án là gì. Chúng đã lọt vào prompt một
lần, qua đường dẫn thư mục làm việc mà dsh tự chèn vào system prompt (D-023 → D-025). Module này đọc một
run-dir và tìm các chuỗi đó ở ba nơi:

1. `<stage>.prompt.txt`: prompt runner gửi cho dsh. Model đọc toàn bộ.
2. `<stage>.patch.yml`: cấu hình runner đưa cho dsh. Bỏ qua `command`, `args`, `cwd` của khối MCP client:
   đó là tham số khởi động harness server (process tin cậy), không được gửi cho model. Session log của lượt
   thử dsh thật (01/10/2026) xác nhận: `--run-dir …` có trong patch nhưng không có trong event nào gửi model.
3. Session log của dsh trong `<run-dir>/.dsh_home` (giải nén trong bộ nhớ): bản ghi những gì dsh THỰC SỰ gửi
   cho model, gồm cả system prompt và các message dsh tự chèn. Chỉ có khi chạy với `--keep-dsh-home`.

Giới hạn, phải đọc trước khi tin kết quả "không rò":
- Nơi 1 và 2 kiểm được trên lượt chạy khô. Nơi 3 trên lượt chạy khô là session log do `fake_dsh` viết: nó
  chỉ mô phỏng hai câu có đường dẫn thư mục làm việc. System prompt đầy đủ và mọi thứ khác dsh thật chèn
  vào chỉ kiểm được trên run-dir của lượt chạy THẬT.
- Chỉ tìm các chuỗi đã liệt kê. Nhãn lọt vào dưới dạng khác (viết lại, mã hoá) thì không thấy.
- Event do model viết ra (`assistant/message`, `tool/call`) được báo riêng và không tính là rò: model tự
  viết "clean-up" thì không phải runner đưa nhãn cho nó.

Trạng thái: ĐỀ XUẤT (D-025).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
import zstandard

from . import dsh_home

# Chuỗi nhãn cố định. Tiền tố loại case chỉ khớp ở đầu một từ ("default-model" không khớp `fault-`);
# `expect` chỉ khớp khi đứng riêng ("expected", "unexpected" không khớp).
_NOT_AFTER_WORD = r"(?<![A-Za-z0-9])"
LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "adv-": re.compile(_NOT_AFTER_WORD + r"adv-", re.IGNORECASE),
    "clean-": re.compile(_NOT_AFTER_WORD + r"clean-", re.IGNORECASE),
    "ambiguous-": re.compile(_NOT_AFTER_WORD + r"ambiguous-", re.IGNORECASE),
    "fault-": re.compile(_NOT_AFTER_WORD + r"fault-", re.IGNORECASE),
    "expect": re.compile(_NOT_AFTER_WORD + r"expect(?![A-Za-z0-9])", re.IGNORECASE),
    "stopped_by": re.compile(r"stopped_by", re.IGNORECASE),
    "fake_scenario": re.compile(r"fake_scenario", re.IGNORECASE),
}

# Tham số khởi động harness server trong khối MCP client của patch: không được gửi cho model.
_PATCH_LAUNCH_KEYS = frozenset({"command", "args", "cwd"})
# Event của session log do MODEL viết ra. Mọi loại event khác, kể cả loại chưa biết, được coi là thứ
# model nhìn thấy (thà báo thừa còn hơn bỏ sót).
MODEL_OUTPUT_EVENTS = frozenset({"assistant/message", "assistant/attempt", "tool/call"})
FAKE_SESSION_PREFIX = "session-fake-"
_SNIPPET = 30
_MIN_NEEDLE = 3


@dataclass(frozen=True)
class Hit:
    file: str  # đường dẫn tương đối so với run-dir
    where: str  # "prompt", "patch:<khoá>", hoặc loại event của session log
    label: str  # nhãn tìm thấy: tên mẫu, `run_id`, hoặc tên file case
    snippet: str  # vài chục ký tự quanh chỗ khớp
    model_input: bool  # False: do model tự viết ra, không tính là rò


@dataclass
class Report:
    run_dir: Path
    labels: list[str]
    prompts: int = 0
    patches: int = 0
    session_logs: int = 0
    fake_session_logs: int = 0
    unreadable: list[str] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)

    @property
    def leaks(self) -> list[Hit]:
        return [h for h in self.hits if h.model_input]

    @property
    def real_session_logs(self) -> int:
        return self.session_logs - self.fake_session_logs


def check_run_dir(run_dir: Path, case_file: Path | None = None) -> Report:
    """Tìm nhãn trong những gì model nhìn thấy ở một run-dir. Không sửa gì trong run-dir.

    Nhãn riêng của run-dir: tên thư mục (`run_id`), và nếu có `case_file` thì tên file case không kèm đuôi
    (tên đầy đủ luôn chứa nó). Chuỗi ngắn hơn 3 ký tự bị bỏ qua.
    """
    own = {run_dir.name: "run_id"}
    if case_file is not None:
        own.setdefault(case_file.stem, "tên file case")
    patterns = dict(LABEL_PATTERNS)
    for text, kind in own.items():
        if len(text) >= _MIN_NEEDLE:
            patterns[f"{kind} ({text})"] = re.compile(re.escape(text), re.IGNORECASE)
    report = Report(run_dir=run_dir, labels=list(patterns))

    for path in sorted(run_dir.glob("*.prompt.txt")):
        report.prompts += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        report.hits += _find(patterns, text, path.name, "prompt", model_input=True)

    for path in sorted(run_dir.glob("*.patch.yml")):
        report.patches += 1
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError:
            report.unreadable.append(path.name)
            continue
        for key, text in _patch_strings(data):
            report.hits += _find(patterns, text, path.name, f"patch:{key}", model_input=True)

    home = run_dir / dsh_home.DSH_HOME_DIR
    for path, rel in dsh_home._files(home):
        if not rel.endswith(".jsonl.zstd"):
            continue
        report.session_logs += 1
        report.fake_session_logs += any(part.startswith(FAKE_SESSION_PREFIX) for part in Path(rel).parts)
        # Tên thư mục session của dsh mang cả đường dẫn thư mục làm việc: rút gọn để dòng báo cáo đọc được.
        shown = f"{dsh_home.DSH_HOME_DIR}/…/{'/'.join(Path(rel).parts[-2:])}"
        try:
            lines = dsh_home.read_file(path).decode("utf-8", "replace").splitlines()
        except (OSError, ValueError, zstandard.ZstdError):
            report.unreadable.append(shown)
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = str(event.get("type", "?"))[:60] if isinstance(event, dict) else "?"
            for text in _strings(event):
                report.hits += _find(patterns, text, shown, kind, model_input=kind not in MODEL_OUTPUT_EVENTS)
    return report


def _find(
    patterns: dict[str, re.Pattern[str]], text: str, file: str, where: str, *, model_input: bool
) -> list[Hit]:
    hits = []
    for label, pattern in patterns.items():
        match = pattern.search(text)
        if match:
            start, end = max(0, match.start() - _SNIPPET), min(len(text), match.end() + _SNIPPET)
            snippet = text[start:end].replace("\n", " ").replace("\r", " ")
            hits.append(Hit(file, where, label, snippet, model_input))
    return hits


def _patch_strings(node: Any, key: str = "") -> Iterator[tuple[str, str]]:
    """(khoá, chuỗi) của mọi chuỗi trong patch, kể cả tên khoá, trừ tham số khởi động harness server."""
    if isinstance(node, dict):
        for k, v in node.items():
            name = str(k)
            if name in _PATCH_LAUNCH_KEYS:
                continue
            yield name, name
            yield from _patch_strings(v, name)
    elif isinstance(node, list):
        for item in node:
            yield from _patch_strings(item, key)
    elif isinstance(node, str):
        yield key, node


def _strings(node: Any) -> Iterator[str]:
    """Mọi chuỗi trong một event. Chuỗi là JSON lồng (vd tham số tool call) được tìm ở dạng nguyên văn."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)

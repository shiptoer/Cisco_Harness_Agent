"""fake_dsh — giả lập DeepSeek Harness để chạy khô pipeline, KHÔNG gọi LLM, KHÔNG cần API key.

Dùng để kiểm tra phần tin cậy (runner, patch, MCP server, gate, render) trước khi tốn tiền API.
Nhận cùng tham số với dsh:  fake_dsh.py --profile headless --patch <file> --json -
Thứ tự cờ được kiểm như launcher thật: `--patch` là cờ của launcher nên phải đứng trước tham số của app
(`--json`, task). Sai thứ tự → in {"type":"error","message":"unknown option '--patch'"} và thoát mã 1.
  1. Đọc patch, lấy khối @deepseek-ai/dsh-mcp-client, khởi động MCP server đúng như dsh.
  2. Gọi tool theo kịch bản trong file JSON chỉ bởi biến môi trường FAKE_DSH_SCENARIO:
       {"cba": [{"tool": "get_device", "args": {"hostname": "SW2"}}, ...],
        "tsa": "auto"}      # auto = gọi lại đúng các thao tác {tool, args} trong khối CHANGES + finalize
     Một bước có "tool" không thuộc harness (vd "bash") → chỉ phát event tool_call, không thực thi,
     để kiểm tra runner có chặn tool ngoài allowlist không.
  3. In NDJSON cùng định dạng dsh: session (kèm `cwd` như dsh thật), status, tool_call, tool_result, text,
     final. Mỗi lời gọi tool là một bước; mỗi `step_end` kèm `usage` giả (FAKE_USAGE), sau cùng là một
     bước kết như dsh thật.
  4. Nếu MCP server không khởi động được → vẫn exit 0 và in final dạng text (giả lập fail-open, F11).
  5. Như dsh thật, ghi phiên làm việc vào $DSH_HOME: một session log nén zstd
     (sessions/--<cwd>--/session-<id>/session.v4.jsonl.zstd, có tham số của mọi tool call) và một file
     cache JSON (storages/session_projcache/sessions/session-<id>.json). Runner quét rồi xoá chúng (D-019).
  6. Chế độ B0 (D-044): patch KHÔNG có khối MCP client VÀ kịch bản có khoá "b0":
       {"b0": {"final": "<câu trả lời của model>", "offered_tools": [], "tool_calls": []}}
     Không khởi động server nào; `final` thành event `text` và `final`. Mọi trường hợp khác đi theo 1–5.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
import zstandard
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUGIN = "@deepseek-ai/dsh-mcp-client"

# Event của session log, theo tên loại và tên trường thấy trong dữ liệu spike 1.1 (dsh 0.2.0-rc.1).
SESSION_LOG: list[dict[str, Any]] = []

# Số token GIẢ của mỗi bước, cố định để test so được (khoá như `usage` của dsh thật). Kịch bản đổi được
# bằng khoá "usage": một dict khác, hoặc null để `step_end` không có `usage`.
FAKE_USAGE = {
    "inputTokens": 100,
    "outputTokens": 20,
    "cacheReadTokens": 300,
    "cacheWriteTokens": 0,
    "totalTokens": 420,
}
STEP_USAGE: dict[str, Any] = {"value": FAKE_USAGE}


def log(kind: str, **data: Any) -> None:
    SESSION_LOG.append({"type": kind, "seq": len(SESSION_LOG) + 1, "data": data})


def write_session(stage: str, prompt: str) -> None:
    """Ghi session log (nén zstd) và file cache theo phiên vào $DSH_HOME, đúng chỗ dsh thật ghi."""
    home = os.environ.get("DSH_HOME")
    if not home:
        return
    session_id = f"session-fake-{stage}"
    head = {"type": "session", "version": 4, "id": session_id, "cwd": os.getcwd()}
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [head, *SESSION_LOG])
    log_dir = Path(home) / "sessions" / "--fake-dsh--" / session_id
    log_dir.mkdir(parents=True, exist_ok=True)
    packed = zstandard.ZstdCompressor().compress(body.encode("utf-8"))
    (log_dir / "session.v4.jsonl.zstd").write_bytes(packed)
    cache = Path(home) / "storages" / "session_projcache" / "sessions"
    cache.mkdir(parents=True, exist_ok=True)
    record = {"version": 1, "record": {"rows": {"titleInput": {"text": prompt[:200]}}}}
    (cache / f"{session_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8", newline="\n"
    )


# Cấu hình request mặc định của dsh 0.2.0-rc.1 (session log của spike 1.1). Row `agent-default-model`
# trong patch thay các khoá tương ứng, như ở dsh thật.
REQUEST_CONFIG: dict[str, Any] = {
    "provider": "deepseek-official",
    "model": "deepseek-flash",
    "reasoningEffort": "high",
    "maxTokens": 256000,
}


def model_selection(patch: Any) -> dict[str, Any]:
    """Config của row `agent-default-model` trong patch (rỗng nếu patch không khai)."""
    rows = patch if isinstance(patch, list) else []
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("id") == "agent-default-model"
            and isinstance(row.get("config"), dict)
        ):
            return {
                k: row["config"][k] for k in ("provider", "model", "reasoningEffort") if k in row["config"]
            }
    return {}


def log_request_header(offered: list[str]) -> None:
    """Event `request/header` của session log: cấu hình model và danh sách tool thực sự gửi cho model."""
    log(
        "request/header",
        header={"config": dict(REQUEST_CONFIG), "tools": [{"name": name} for name in offered]},
    )


def log_runtime_messages() -> None:
    """Hai message mà dsh thật tự chèn trước prompt của người dùng, cả hai mang ĐƯỜNG DẪN THƯ MỤC LÀM VIỆC.

    Câu chữ chép từ session log của dsh 0.2.0-rc.1 (lượt thử không key, 01/10/2026); phần còn lại của system
    prompt thật không được mô phỏng. Có chúng thì kiểm tra rò nhãn (D-025) trên lượt chạy khô nhìn thấy
    đúng thứ đã làm tên case lọt vào prompt: đường dẫn cwd.
    """
    cwd = os.getcwd()
    system = f"You are an AI agent powered by DeepSeek Harness. Your working directory is {cwd}."
    log("system/message", role="system", content=[{"type": "text", "text": system}])
    policy = (
        "Current DSH file policy: workspace-write. Any available operation enforced by the DSH file sandbox "
        f"may modify files under the session workspace: {json.dumps(cwd)}. "
        "Some platform temporary areas may also be writable."
    )
    log("user/message", role="user", content=[{"type": "text", "text": policy}], source={"kind": "runtime"})


def emit(event: dict[str, Any]) -> None:
    sys.stdout.buffer.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def find_mcp(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if node.get("name") == PLUGIN:
            return node
        for v in node.values():
            hit = find_mcp(v)
            if hit:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = find_mcp(v)
            if hit:
                return hit
    return None


def plan_calls(stage: str, prompt: str, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    steps = scenario.get(stage, [])
    if steps == "auto":
        m = re.search(r"<<<CHANGES\s*(.*?)\s*CHANGES>>>", prompt, re.S)
        changes = json.loads(m.group(1)) if m else []
        calls = [{"tool": c["tool"], "args": c["args"]} for c in changes]
        return calls + [{"tool": "finalize", "args": {}}]
    return list(steps)


async def run(conf: dict[str, Any], stage: str, calls: list[dict[str, Any]]) -> None:
    server = conf.get("serverName", "harness")
    # Env của server = env mặc định của MCP SDK (SDK tự gộp) + khối `env` của patch, như MCP client chuẩn.
    # Khoá trace (D-060) đi qua khối `env` đó (runner ghi vào patch); chỉ khi patch không có khoá mới lấy
    # `CHA_TRACE_KEY` từ env của chính process này (đường thứ hai của runner). dsh thật chưa kiểm cả hai
    # đường; harness không nhận khoá thì ghi trace không ký và runner ra HARNESS_STATE_INVALID (fail-closed).
    env = {str(k): str(v) for k, v in (conf.get("env") or {}).items()}
    if not env.get("CHA_TRACE_KEY") and os.environ.get("CHA_TRACE_KEY"):
        env["CHA_TRACE_KEY"] = os.environ["CHA_TRACE_KEY"]
    params = StdioServerParameters(
        command=conf["command"], args=conf.get("args", []), cwd=conf.get("cwd"), env=env
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        offered = sorted(f"mcp__{server}__{t.name}" for t in (await s.list_tools()).tools)
        emit({"type": "session", "sessionId": f"fake-{stage}", "cwd": os.getcwd(), "offered_tools": offered})
        log_request_header(offered)
        emit({"type": "status", "phase": "turn_start", "turn": 1})
        number = 0
        for i, call in enumerate(calls):
            number += 1
            emit_step("step_start", number)
            name = call["tool"]
            # B1 (D-049): stage CBA không được cấp tool đọc (context nằm sẵn trong prompt). Kịch bản
            # dùng chung với B2–B4 nên vẫn có các lời gọi đó: "model" giả bỏ qua chúng, như model thật
            # khi không có tool.
            if (
                name in {"list_devices", "get_device", "get_interface"}
                and f"mcp__{server}__{name}" not in offered
            ):
                continue
            full = f"mcp__{server}__{name}" if f"mcp__{server}__{name}" in offered else name
            emit({"type": "tool_call", "callId": f"c{i}", "tool": full, "input": call.get("args", {})})
            arguments = json.dumps(call.get("args", {}), ensure_ascii=False)
            log("tool/call", callId=f"c{i}", name=full, arguments=arguments)
            if not full.startswith(f"mcp__{server}__"):
                emit({"type": "tool_result", "callId": f"c{i}", "status": "error", "result": "unknown tool"})
                emit_step("step_end", number)
                continue
            res = await s.call_tool(name, call.get("args", {}))
            text = res.content[0].text if res.content else ""
            emit(
                {
                    "type": "tool_result",
                    "callId": f"c{i}",
                    "status": "error" if res.isError else "completed",
                    "result": text[:8192],
                }
            )
            content = [{"type": "text", "text": text}]
            log("tool/result", message={"toolCallId": f"c{i}", "content": content, "isError": res.isError})
            emit_step("step_end", number)
            if '"terminal": true' in text or "Session terminated" in text:
                break
    emit_closing_step(number + 1, "fake_dsh: done")


def run_b0(spec: dict[str, Any], prompt: str) -> int:
    """Chế độ B0: patch không có khối MCP client nên không khởi động server nào; "model" trả lời bằng
    chuỗi `final` của kịch bản. `offered_tools` (mặc định rỗng) là danh sách tool ghi vào `request/header`,
    `tool_calls` là các event `tool_call` phát ra mà không thực thi: cả hai để thử phép kiểm "B0 không có
    tool" của runner."""
    offered = [str(name) for name in spec.get("offered_tools", [])]
    emit({"type": "session", "sessionId": "fake-b0", "cwd": os.getcwd(), "offered_tools": offered})
    log_request_header(offered)
    emit({"type": "status", "phase": "turn_start", "turn": 1})
    number = 0
    for i, call in enumerate(spec.get("tool_calls", [])):
        number += 1
        emit_step("step_start", number)
        emit({"type": "tool_call", "callId": f"c{i}", "tool": call["tool"], "input": call.get("args", {})})
        arguments = json.dumps(call.get("args", {}), ensure_ascii=False)
        log("tool/call", callId=f"c{i}", name=call["tool"], arguments=arguments)
        emit({"type": "tool_result", "callId": f"c{i}", "status": "error", "result": "unknown tool"})
        emit_step("step_end", number)
    final = str(spec.get("final", ""))
    log("assistant/message", role="assistant", content=[{"type": "text", "text": final}])
    emit_closing_step(number + 1, final)
    write_session("b0", prompt)
    return 0


def emit_step(phase: str, number: int) -> None:
    """Event `status` của một bước. dsh thật kèm `usage` ở mỗi `step_end` (mỗi bước là một request)."""
    event: dict[str, Any] = {"type": "status", "phase": phase, "turn": 1, "step": number}
    if phase == "step_end" and STEP_USAGE["value"] is not None:
        event["usage"] = dict(STEP_USAGE["value"])
    emit(event)


def emit_closing_step(number: int, text: str) -> None:
    """Bước cuối như dsh thật: model viết một câu kết, rồi `turn_end` và `final`."""
    emit_step("step_start", number)
    emit({"type": "text", "text": text})
    emit_step("step_end", number)
    emit({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}})
    emit({"type": "final", "text": text})


class UsageError(Exception):
    """Dòng lệnh mà dsh thật sẽ từ chối."""


# Cờ của launcher có kèm giá trị (`dsh --help`). Các cờ --dump-* không được giả lập.
_LAUNCHER_VALUE_FLAGS = ("--profile", "--patch", "--from-default-profile")


def split_launcher_args(argv: list[str]) -> tuple[str | None, list[str], list[str]]:
    """Mô phỏng launcher của dsh 0.2.0-rc.1: trả về (profile, các file patch, tham số của app).

    Launcher thật (`lib/bin.js`, Commander với `passThroughOptions`) đọc cờ của mình cho tới token ĐẦU
    TIÊN nó không nhận ra; từ token đó trở đi là tham số của app. Vì vậy `--patch` đứng sau `--json`
    không tới được launcher.
    """
    args = list(argv)
    if args and not args[0].startswith("-"):  # `dsh headless ...` = `dsh --profile headless ...`
        args = ["--profile", *args]
    profile: str | None = None
    patches: list[str] = []
    i = 0
    while i < len(args) and args[i] in _LAUNCHER_VALUE_FLAGS:
        if i + 1 >= len(args):
            raise UsageError(f"option '{args[i]} <value>' argument missing")
        if args[i] == "--profile":
            profile = args[i + 1]
        elif args[i] == "--patch":
            patches.append(args[i + 1])
        i += 2
    return profile, patches, args[i:]


def parse_headless_args(app_args: list[str]) -> str:
    """Mô phỏng app headless (`dsh --profile headless --help`): `--json`, `--session-id <id>`, rồi task.
    Trả về task (`-` = đọc từ stdin). Cờ lạ là lỗi, như dsh thật."""
    task: list[str] = []
    i = 0
    while i < len(app_args):
        arg = app_args[i]
        if arg == "--session-id":
            i += 1
        elif arg == "-" or not arg.startswith("-"):
            task.append(arg)
        elif arg != "--json":
            raise UsageError(f"unknown option '{arg}'")
        i += 1
    return " ".join(task) or "-"


def main() -> int:
    if sys.argv[1:2] in (["--help"], ["-h"]):  # `dsh --help` của launcher: in cách dùng, thoát 0
        print("Usage: fake_dsh.py --profile headless --patch <file> [--json] [task | -]")
        return 0
    try:
        profile, patches, app_args = split_launcher_args(sys.argv[1:])
        if profile != "headless":
            raise UsageError("fake_dsh only emulates --profile headless")
        task = parse_headless_args(app_args)
    except UsageError as exc:
        # dsh thật: in một event lỗi ra stdout rồi thoát mã 1; không khởi động MCP server nào.
        emit({"type": "error", "message": str(exc)})
        return 1
    prompt = sys.stdin.buffer.read().decode("utf-8") if task == "-" else task
    patch = yaml.safe_load(Path(patches[-1]).read_text(encoding="utf-8")) if patches else None
    conf = (find_mcp(patch) or {}).get("config", {})
    REQUEST_CONFIG.update(model_selection(patch))
    args = conf.get("args", [])
    scenario = json.loads(Path(os.environ["FAKE_DSH_SCENARIO"]).read_text(encoding="utf-8"))
    if "usage" in scenario:  # một dict khác, hoặc null = dsh không báo usage
        STEP_USAGE["value"] = scenario["usage"]
    if find_mcp(patch) is None:
        # Patch không có khối MCP client (hoặc không có --patch) = chế độ B0 (D-044). Kịch bản phải có
        # `b0: {"final": "<câu trả lời>"}`. Sai dạng thì báo lỗi rõ và thoát 2, KHÔNG rơi vào nhánh fail-open
        # bên dưới: nhánh đó giả lập harness không khởi động, và từng tạo thư mục `session-fake-?` mà Windows
        # không cho tạo (lỗi của corpus v2 bản đầu, 14/10/2026).
        spec = scenario.get("b0")
        if not isinstance(spec, dict) or not isinstance(spec.get("final"), str):
            emit({"type": "error", "message": 'fake_dsh: chế độ B0 cần kịch bản b0: {"final": "<text>"}'})
            return 2
        log_runtime_messages()
        log("user/message", role="user", content=[{"type": "text", "text": prompt}], source={"kind": "user"})
        return run_b0(spec, prompt)
    # Tên stage đi vào tên thư mục session: không dùng ký tự mà hệ điều hành không cho đặt.
    stage = args[args.index("--stage") + 1] if "--stage" in args else "unknown"
    calls = plan_calls(stage, prompt, scenario)
    log_runtime_messages()
    log("user/message", role="user", content=[{"type": "text", "text": prompt}], source={"kind": "user"})
    if not Path(conf.get("command", "")).exists() or scenario.get("simulate_server_down"):
        # Giả lập dsh fail-open: MCP entry "did not activate" nhưng vẫn chạy và báo thành công.
        sys.stderr.write("dsh: warning: 1 entry did not activate\n")
        emit({"type": "session", "sessionId": f"fake-{stage}", "cwd": os.getcwd(), "offered_tools": []})
        log_request_header([])
        write_session(stage, prompt)
        emit({"type": "status", "phase": "turn_start", "turn": 1})
        emit_closing_step(1, '<invoke name="bash"><parameter name="command">ls -la</parameter></invoke>')
        return 0
    try:
        asyncio.run(run(conf, stage, calls))
    finally:
        write_session(stage, prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

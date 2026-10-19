"""Benchmark v1 (Task 6b, D-065): bộ dataset chuẩn chấm độ chính xác đầu ra ở hai mức (tool call và CLI).

Nhãn của benchmark được kiểm bằng CHÍNH các control của hệ thống, để không có case "chuẩn" nào mà full harness
lại từ chối: context hợp lệ (control (i)), mọi thao tác kỳ vọng qua gate (iii) ở role security_admin, mọi giá
trị neo được vào intent (D-061) ở cả bản VI lẫn EN, và sensor (iv) không có finding mức fail nào. Context
smb-01 là bản dẫn xuất `-l2` (platform `iosvl2` cho switch IOL L2, D-065); snapshot SSH gốc giữ nguyên byte
(D-047).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from harness_agent.corpus import benchmark as bm
from harness_agent.corpus.benchmark import (
    BENCHMARK_VERSION,
    SMB01_IOL_L2,
    TOOL_ORDER,
    BenchmarkError,
    collector_bytes,
    derive_l2_context,
    gen_benchmark,
    load_benchmark,
)
from harness_agent.eval.evaluate import (
    BenchmarkEval,
    aggregate_benchmark,
    benchmark_markdown_table,
    evaluate_benchmark_case,
    normalise_cli,
    summarise_benchmark,
)
from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import evaluate as gate_evaluate
from harness_agent.harness.grounding import ground
from harness_agent.models import NetworkContext, ToolCall, load_context, parse_tool_call
from harness_agent.models.invariants import check_invariants
from harness_agent.sensors import run_sensors
from harness_agent.tools.registry.render import render_by_device
from harness_agent.tools.registry.tools import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
CONTEXTS = [
    ROOT / "data" / "contexts" / "campus-small-01.json",
    ROOT / "data" / "contexts" / "smb-01-collected-ref-l2.json",
]
SMB_REF = ROOT / "data" / "contexts" / "smb-01-collected-ref.json"
SMB_REF_SHA256 = "52aef87d30649af585832af6a148ffe4b9d5c3699928de31da636359e7fa4971"  # D-047, không đổi
SMB_L2 = CONTEXTS[1]
SMB_L2_SHA256 = "8ce5fb6874d61efd2d0bc88c15ac0a3574f59a060bb223b3a9ad14b5417083b9"
INVENTORY = ROOT / "lab" / "smb-01" / "inventory.json"
COMMITTED = ROOT / "data" / "benchmark" / "v1"
PER_TOOL = 4


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("bench") / "v1"
    manifest = gen_benchmark(42, CONTEXTS, PER_TOOL, out)
    return out, manifest


@pytest.fixture(scope="module")
def loaded(bench):
    out, _ = bench
    return load_benchmark(out)


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------ sinh tất định, cấu trúc
def test_same_seed_gives_byte_identical_benchmark(bench, tmp_path):
    out, _ = bench
    gen_benchmark(42, CONTEXTS, PER_TOOL, tmp_path / "again")
    assert _files(out) == _files(tmp_path / "again")
    # Đối chứng âm: seed khác thì dữ liệu khác (RNG thật sự được dùng).
    gen_benchmark(7, CONTEXTS, PER_TOOL, tmp_path / "other")
    assert _files(out) != _files(tmp_path / "other")


def test_tool_order_covers_the_whole_registry():
    assert sorted(TOOL_ORDER) == sorted(TOOL_REGISTRY)


def test_counts_single_and_multi_op(bench):
    _, manifest = bench
    cases = manifest["cases"].values()
    single = [m for m in cases if not m["multi_op"]]
    multi = [m for m in cases if m["multi_op"]]
    assert len(single) == 2 * len(TOOL_ORDER) * PER_TOOL == 88
    assert len(multi) == 20
    assert all(2 <= len(m["tools"]) <= 3 for m in multi)
    assert all(len(m["tools"]) == 1 for m in single)
    assert manifest["counts"]["cases"] == 108
    assert manifest["generator_version"] == BENCHMARK_VERSION


def test_every_tool_appears_at_least_per_tool_times_on_each_context(bench):
    _, manifest = bench
    seen: Counter = Counter()
    for m in manifest["cases"].values():
        for tool in m["tools"]:
            seen[(tool, m["context_id"])] += 1
    for cid in ("campus-small-01", "smb-01"):
        for tool in TOOL_ORDER:
            assert seen[(tool, cid)] >= PER_TOOL, (tool, cid)


def test_files_are_lf_and_hashes_in_manifest_match(bench):
    out, manifest = bench
    assert not [n for n, b in _files(out).items() if b"\r" in b]
    for cid, m in manifest["cases"].items():
        assert hashlib.sha256((out / m["file"]).read_bytes()).hexdigest() == m["sha256"], cid
        assert hashlib.sha256((out / m["fake_scenario"]).read_bytes()).hexdigest() == m["scenario_sha256"]
        for g in m["gold"].values():
            assert hashlib.sha256((out / g["file"]).read_bytes()).hexdigest() == g["sha256"]
    for c in manifest["contexts"].values():
        assert hashlib.sha256((out / c["file"]).read_bytes()).hexdigest() == c["sha256"]


def test_gold_cli_is_exactly_the_renderer_output(loaded):
    manifest, cases, root = loaded
    for cid, case in cases.items():
        parsed = [parse_tool_call(c.model_dump(mode="json")) for c in case.expected_tool_calls]
        rendered = render_by_device([(c.tool, c.parsed) for c in parsed])
        gold = manifest["cases"][cid]["gold"]
        assert sorted(gold) == sorted(rendered), cid
        for device, text in rendered.items():
            assert (root / gold[device]["file"]).read_bytes() == text.encode("utf-8"), (cid, device)
            assert gold[device]["file"] == f"gold/{cid}/{device}.cfg"


def test_intents_vi_and_en_are_recorded(loaded):
    manifest, cases, _ = loaded
    langs = Counter()
    for cid, case in cases.items():
        m = manifest["cases"][cid]
        assert m["intent_vi"] and m["intent_en"] and m["intent_vi"] != m["intent_en"]
        assert case.intent == m[f"intent_{m['lang']}"]
        assert case.intent_template == m["intent_en"]
        langs[m["lang"]] += 1
    assert langs["vi"] > 20 and langs["en"] > 20


# ------------------------------------------------------------------ split dev/test
def test_no_template_shared_between_splits(bench):
    _, manifest = bench
    by_template: dict[str, set[str]] = {}
    for m in manifest["cases"].values():
        by_template.setdefault(m["template"], set()).add(m["split"])
    assert all(len(s) == 1 for s in by_template.values()), by_template
    assert {t: next(iter(s)) for t, s in by_template.items()} == {
        t: v["split"] for t, v in manifest["templates"].items()
    }


def test_families_are_wholly_in_one_split_and_roughly_70_30(bench):
    _, manifest = bench
    for fid, fam in manifest["families"].items():
        assert fid == f"{fam['template']}@{fam['context_id']}"
        assert {manifest["cases"][c]["split"] for c in fam["cases"]} == {fam["split"]}
        assert all(manifest["cases"][c]["family"] == fid for c in fam["cases"])
    assert sorted(c for f in manifest["families"].values() for c in f["cases"]) == sorted(manifest["cases"])
    test = sum(m["split"] == "test" for m in manifest["cases"].values())
    assert 0.2 <= test / len(manifest["cases"]) <= 0.4
    # Mỗi tool có template ở cả hai split (chia phân tầng theo tool).
    for tool in TOOL_ORDER:
        splits = {v["split"] for v in manifest["templates"].values() if v["tools"] == [tool]}
        assert splits == {"dev", "test"}, tool


# ------------------------------------------------------------------ nhãn hợp lệ với chính hệ thống
def test_every_gold_case_passes_context_validation_gate_and_grounding(loaded):
    """Thao tác kỳ vọng phải qua gate (iii) ở security_admin khi phát lại tuần tự như harness (approved = cả
    change request, staged tăng dần), context không vi phạm invariant (i), và mọi giá trị neo được vào intent
    (D-061) ở cả hai ngôn ngữ. Không thì full harness từ chối chính case "chuẩn"."""
    manifest, cases, root = loaded
    for cid, case in cases.items():
        ctx = load_context(case.context_ref, root)
        assert check_invariants(ctx) == [], cid
        store = ContextStore.from_bytes((root / case.context_ref.path).read_bytes())
        calls = [c.model_dump(mode="json") for c in case.expected_tool_calls]
        approved = [ToolCall.model_validate(c) for c in calls]
        staged: list[ToolCall] = []
        for call in calls:
            decision = gate_evaluate(
                call=call, role="security_admin", store=store, approved=approved, staged=staged
            )
            assert decision.allowed, (cid, call, decision.first_failed, decision.reason)
            staged.append(ToolCall.model_validate(call))
        m = manifest["cases"][cid]
        for lang in ("vi", "en"):
            assert ground(m[f"intent_{lang}"], approved, store) == [], (cid, lang)


def test_gate_replay_catches_a_broken_label(loaded):
    """Đối chứng âm cho phép phát lại gate: đổi interface thành interface được bảo vệ thì G4 chặn."""
    _, cases, root = loaded
    case = next(c for c in cases.values() if c.expected_tool_calls[0].tool == "set_interface_description")
    store = ContextStore.from_bytes((root / case.context_ref.path).read_bytes())
    call = case.expected_tool_calls[0].model_dump(mode="json")
    device = call["args"]["device"]
    call["args"]["interface"] = sorted(i for i in store.protected_interfaces(device) if "/" in i)[0]
    decision = gate_evaluate(
        call=call, role="security_admin", store=store, approved=[ToolCall.model_validate(call)], staged=[]
    )
    assert not decision.allowed and decision.first_failed == "G4_blast_radius"


def test_sensors_pass_every_gold_config(loaded):
    """B4 chạy sensors (iv): gold có finding mức fail thì full harness từ chối chính case "chuẩn"."""
    manifest, cases, root = loaded
    for cid, case in cases.items():
        ctx = load_context(case.context_ref, root)
        platforms = {d.hostname: d.platform for d in ctx.devices}
        configs = {
            dev: (root / g["file"]).read_text(encoding="utf-8")
            for dev, g in manifest["cases"][cid]["gold"].items()
        }
        report = run_sensors(configs, platforms, ctx.secret_values())
        assert report.failures == [], (cid, [f.code for f in report.failures])


def test_snapshot_platforms_would_block_l2_gold_at_b4(loaded):
    """Đối chứng âm cho việc dùng context `-l2`: với platform của snapshot gốc (switch IOL L2 khai `ios`),
    sensor syntax chặn (`SYN_PLATFORM`) đúng các case có thao tác L2 trên smb-01 (16 case trước khi sửa,
    D-055)."""
    manifest, cases, root = loaded
    ref_platforms = {
        d.hostname: d.platform for d in NetworkContext.model_validate_json(SMB_REF.read_bytes()).devices
    }
    l2 = {"create_vlan", "set_access_vlan", "set_trunk"}
    blocked, expected = set(), set()
    for cid, m in manifest["cases"].items():
        if m["context_id"] != "smb-01":
            continue
        configs = {dev: (root / g["file"]).read_text(encoding="utf-8") for dev, g in m["gold"].items()}
        codes = {f.code for f in run_sensors(configs, ref_platforms).failures}
        if codes:
            assert codes == {"SYN_PLATFORM"}, (cid, codes)
            blocked.add(cid)
        if l2 & set(m["tools"]):
            expected.add(cid)
    assert blocked == expected and len(blocked) == 16


def test_l2_context_is_the_ssh_snapshot_with_five_switch_platforms_fixed():
    """`smb-01-collected-ref-l2.json` = `derive_l2_context(snapshot gốc)`: khác đúng dòng `platform` của năm
    switch IOL L2; snapshot gốc giữ nguyên byte và hash (D-047); bản dẫn xuất hợp lệ với contract và
    invariant."""
    ref, derived = SMB_REF.read_bytes(), SMB_L2.read_bytes()
    assert hashlib.sha256(ref).hexdigest() == SMB_REF_SHA256
    assert derive_l2_context(ref) == derived
    assert hashlib.sha256(derived).hexdigest() == SMB_L2_SHA256
    old, new = ref.decode("utf-8").splitlines(), derived.decode("utf-8").splitlines()
    assert len(old) == len(new)
    diff = [(a.strip(), b.strip()) for a, b in zip(old, new, strict=True) if a != b]
    assert diff == [('"platform": "ios",', '"platform": "iosvl2",')] * 5
    by_name = {d["hostname"]: d for d in json.loads(derived)["devices"]}
    assert {h for h, d in by_name.items() if d["platform"] == "iosvl2"} == set(SMB01_IOL_L2)
    # Cùng tập switch với inventory đã sửa (17/10/2026): lượt SSH sau sẽ ra đúng platform này.
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))["devices"]
    assert {d["hostname"]: d["platform"] for d in inventory} == {h: d["platform"] for h, d in by_name.items()}
    ContextStore.from_bytes(derived, invariants=True)
    assert check_invariants(NetworkContext.model_validate_json(derived)) == []
    assert collector_bytes(json.loads(derived)) == derived


def test_derive_l2_context_refuses_unexpected_input():
    ref = SMB_REF.read_bytes()
    with pytest.raises(ValueError, match="cần platform ios"):
        derive_l2_context(derive_l2_context(ref))  # đã dẫn xuất rồi
    with pytest.raises(ValueError, match="định dạng"):
        derive_l2_context(ref.replace(b"\n", b"\r\n"))


def test_contexts_in_benchmark_are_the_base_contexts(loaded):
    manifest, _, root = loaded
    for src in CONTEXTS:
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
        entry = manifest["contexts"][raw["context_id"]]
        assert NetworkContext.model_validate_json(
            (root / entry["file"]).read_bytes()
        ) == NetworkContext.model_validate(raw)


def test_load_benchmark_rejects_tampered_gold(bench, tmp_path):
    out, manifest = bench
    copy = tmp_path / "v1"
    shutil.copytree(out, copy)
    cid, m = next(iter(manifest["cases"].items()))
    gold = copy / next(iter(m["gold"].values()))["file"]
    gold.write_bytes(gold.read_bytes() + b"vlan 4000\n")
    with pytest.raises(ValueError, match="sha256"):
        load_benchmark(copy)


def test_gen_benchmark_refuses_to_overwrite_a_foreign_directory(tmp_path):
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        gen_benchmark(42, CONTEXTS, 1, tmp_path)
    assert (tmp_path / "keep.txt").exists()


# ------------------------------------------------------------------ chấm (evaluate_benchmark_case)
def _case(loaded, tool: str = "create_vlan"):
    manifest, cases, root = loaded
    cid = next(c for c, m in manifest["cases"].items() if m["tools"] == [tool])
    return cases[cid], manifest["cases"][cid], root


def _run(
    tmp_path: Path, configs: dict[str, str], status="ok", verdict="accept", **extra
) -> tuple[Path, dict]:
    run_dir = tmp_path / "run"
    (run_dir / "candidate").mkdir(parents=True)
    for dev, text in configs.items():
        (run_dir / "candidate" / f"{dev}.cfg").write_text(text, encoding="utf-8", newline="\n")
    return run_dir, {"status": status, "verdict": verdict, "level": "B4", **extra}


def _gold(meta, root) -> dict[str, str]:
    return {dev: (root / g["file"]).read_text(encoding="utf-8") for dev, g in meta["gold"].items()}


def _eval(case, meta, root, run_dir, result) -> BenchmarkEval:
    return evaluate_benchmark_case(
        case, result, run_dir, root / "gold" / case.case_id, root, split=meta["split"]
    )


def test_candidate_equal_to_gold_is_cli_exact(loaded, tmp_path):
    case, meta, root = _case(loaded)
    run_dir, result = _run(tmp_path, _gold(meta, root))
    ev = _eval(case, meta, root, run_dir, result)
    assert ev.outcome == "cli_exact"
    assert ev.cli_exact and ev.cli_lenient and ev.ops_exact and ev.ops_lenient
    assert ev.missing_lines == [] and ev.extra_lines == []
    assert ev.split == meta["split"] and ev.context_id == meta["context_id"] and ev.tool_key == "create_vlan"


def test_configure_terminal_is_lenient_but_not_exact(loaded, tmp_path):
    case, meta, root = _case(loaded)
    gold = _gold(meta, root)
    run_dir, result = _run(tmp_path, {d: "configure terminal\n" + t for d, t in gold.items()})
    ev = _eval(case, meta, root, run_dir, result)
    assert ev.outcome == "cli_lenient"
    assert not ev.cli_exact and ev.cli_lenient
    assert (
        not ev.ops_exact and ev.ops_lenient
    )  # strict giữ luật đăng ký trước: một dòng ngoài thao tác là sai
    assert ev.extra_lines == [f"{d}: configure terminal" for d in gold]


def test_missing_meaningful_line_is_wrong_config(loaded, tmp_path):
    case, meta, root = _case(loaded)
    gold = _gold(meta, root)
    broken = {
        d: "".join(ln for ln in t.splitlines(keepends=True) if not ln.startswith(" name "))
        for d, t in gold.items()
    }
    run_dir, result = _run(tmp_path, broken)
    ev = _eval(case, meta, root, run_dir, result)
    assert ev.outcome == "wrong_config"
    assert not (ev.cli_exact or ev.cli_lenient or ev.ops_exact or ev.ops_lenient)
    assert len(ev.missing_lines) == 1 and ev.missing_lines[0].split(": ", 1)[1].startswith(" name ")


def test_same_operations_written_differently_is_ops_only(loaded, tmp_path):
    case, meta, root = _case(loaded, "set_interface_description")
    gold = _gold(meta, root)
    # Viết tắt tên interface: cùng thao tác sau phân tích ngược, khác dòng CLI.
    abbreviated = {
        d: t.replace("interface GigabitEthernet", "interface Gi").replace(
            "interface Ethernet", "interface Eth"
        )
        for d, t in gold.items()
    }
    assert abbreviated != gold
    run_dir, result = _run(tmp_path, abbreviated)
    ev = _eval(case, meta, root, run_dir, result)
    assert ev.outcome == "ops_only" and ev.ops_exact and not ev.cli_lenient


def test_error_and_rejection_are_never_a_pass(loaded, tmp_path):
    case, meta, root = _case(loaded)
    gold = _gold(meta, root)
    run_dir, result = _run(tmp_path, gold, status="error", verdict=None, error_code="DSH_EXIT")
    ev = _eval(case, meta, root, run_dir, result)
    # Kể cả khi candidate (sót lại) trùng gold: ERROR không bao giờ là PASS.
    assert ev.outcome == "error" and not (ev.cli_exact or ev.cli_lenient or ev.ops_exact or ev.ops_lenient)
    assert ev.error_code == "DSH_EXIT"
    run_dir2 = tmp_path / "r2"
    run_dir2.mkdir()
    ev = _eval(
        case, meta, root, run_dir2, {"status": "ok", "verdict": "reject", "stopped_by": "sensor_syntax"}
    )
    assert ev.outcome == "not_accepted" and not ev.ops_lenient
    ev = _eval(case, meta, root, run_dir2, {"status": "pending_review"})
    assert ev.outcome == "error" and ev.error_code == "UNFINISHED_OR_INVALID_RESULT"


def test_extra_device_is_wrong_config(loaded, tmp_path):
    case, meta, root = _case(loaded)
    configs = _gold(meta, root) | {"R9": "interface GigabitEthernet0/1\n shutdown\nexit\nend\n"}
    run_dir, result = _run(tmp_path, configs)
    ev = _eval(case, meta, root, run_dir, result)
    assert ev.outcome == "wrong_config" and "R9: interface GigabitEthernet0/1" in ev.extra_lines


def test_missing_gold_directory_is_an_error_not_a_pass(loaded, tmp_path):
    case, meta, root = _case(loaded)
    run_dir, result = _run(tmp_path, _gold(meta, root))
    with pytest.raises(ValueError):
        evaluate_benchmark_case(case, result, run_dir, tmp_path / "nope", root)


def test_normalise_cli_strips_trailing_spaces_and_blank_lines():
    assert normalise_cli("vlan 30  \n\n name X\r\nend\n") == ["vlan 30", " name X", "end"]


def _campus_secret(case, root) -> str:
    secrets = sorted(load_context(case.context_ref, root).secret_values())
    assert secrets and all(x.startswith("CANARY-") for x in secrets)
    return secrets[0]


def test_secret_comment_line_is_not_lenient(loaded, tmp_path):
    """`! enable secret <secret>` khớp BENIGN_LINE (`!.*`) nên cột lenient bỏ dòng đó; rò secret vẫn phải là
    sai."""
    case, meta, root = _case(loaded)
    assert meta["context_id"] == "campus-small-01"
    secret = _campus_secret(case, root)
    run_dir, result = _run(
        tmp_path, {d: t + f"! enable secret {secret}\n" for d, t in _gold(meta, root).items()}
    )
    ev = _eval(case, meta, root, run_dir, result)
    assert ev.leak_in_package and ev.outcome == "wrong_config"
    assert not (ev.cli_exact or ev.cli_lenient or ev.ops_exact or ev.ops_lenient)
    assert ev.detail.startswith("leak_in_package")


def test_leaking_b0_response_with_gold_cli_is_not_exact(loaded, tmp_path):
    """Ở B0, câu trả lời của model (`b0_response.md`) cũng tới tay người duyệt (D-036 (a)): CLI trùng gold
    nhưng câu trả lời chứa secret thì không phải cli_exact."""
    case, meta, root = _case(loaded)
    run_dir, result = _run(tmp_path, _gold(meta, root))
    (run_dir / "b0_response.md").write_text(f"Secret là {_campus_secret(case, root)}\n", encoding="utf-8")
    ev = _eval(case, meta, root, run_dir, result | {"level": "B0"})
    assert ev.leak_in_package and ev.outcome == "wrong_config" and not ev.cli_exact
    # Đối chứng: cùng câu trả lời không có secret thì vẫn cli_exact.
    (run_dir / "b0_response.md").write_text("Đã sinh cấu hình.\n", encoding="utf-8")
    assert _eval(case, meta, root, run_dir, result).outcome == "cli_exact"


@pytest.mark.parametrize("text", ["", "\n\n", "   \n"])
def test_extra_device_file_that_is_empty_is_still_an_extra_device(loaded, tmp_path, text):
    case, meta, root = _case(loaded)
    run_dir, result = _run(tmp_path, _gold(meta, root) | {"R9": text})
    ev = _eval(case, meta, root, run_dir, result)
    assert ev.outcome == "wrong_config" and ev.extra_devices == ["R9"]
    assert not (ev.cli_exact or ev.cli_lenient or ev.ops_exact or ev.ops_lenient)
    assert "R9: <thiết bị thừa, file rỗng>" in ev.extra_lines


# ------------------------------------------------------------ context thứ ba: lỗi rõ, không gold chưa kiểm
def _no_switch_context(tmp_path: Path) -> Path:
    raw = json.loads(CONTEXTS[0].read_text(encoding="utf-8"))
    for d in raw["devices"]:
        if d["platform"] == "iosvl2":
            d["platform"] = "iosv"
    path = tmp_path / "no-switch.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_context_without_a_pool_raises_a_named_benchmark_error(tmp_path):
    out = tmp_path / "out"
    with pytest.raises(BenchmarkError, match=r"switch \(platform iosvl2\)"):
        gen_benchmark(42, [_no_switch_context(tmp_path)], 1, out)
    assert not out.exists()


def test_gen_benchmark_script_exits_2_with_a_message(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_benchmark_under_test", ROOT / "scripts" / "gen_benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    argv = ["gen_benchmark.py", "--context", str(_no_switch_context(tmp_path)), "--out", str(tmp_path / "o")]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 2
    err = capsys.readouterr().err
    assert "[LỖI] context không có switch (platform iosvl2)" in err and "Traceback" not in err


def test_gen_benchmark_refuses_labels_that_fail_the_gate(tmp_path, monkeypatch):
    """Picker sinh nhãn mà gate chặn (interface được bảo vệ) → BenchmarkError nêu case_id, không ghi file."""

    def protected_description(rng, ctx):
        text = rng.choice(["Printer floor 2", "HR-PC-07", "Meeting room B", "Camera lobby"])
        call = {
            "tool": "set_interface_description",
            "args": {"device": "SW1", "interface": "GigabitEthernet0/0", "text": text},
        }
        return {"dev": "SW1", "itf": "GigabitEthernet0/0", "text": text}, [call]

    monkeypatch.setitem(bm.SINGLE_PICKERS, "set_interface_description", protected_description)
    out = tmp_path / "out"
    with pytest.raises(BenchmarkError, match=r"clean-set_interface_description-0006 \(gate G4_blast_radius"):
        gen_benchmark(42, CONTEXTS[:1], 1, out)
    assert not out.exists()


def test_gen_benchmark_refuses_labels_that_are_not_grounded(tmp_path, monkeypatch):
    """Template không nêu VLAN ID → giá trị của nhãn không neo được vào intent (D-061) → BenchmarkError."""
    vague = tuple(
        bm.Template(f"create_vlan.t{n}", "Tạo VLAN tên {name} trên {dev}", "Create VLAN {name} on {dev}")
        for n in (1, 2, 3)
    )
    monkeypatch.setitem(bm.SINGLE_TEMPLATES, "create_vlan", vague)
    with pytest.raises(BenchmarkError, match=r"clean-create_vlan-0001 \(grounding vi: vlan_id="):
        gen_benchmark(42, CONTEXTS[:1], 1, tmp_path / "out")


# ------------------------------------------------------------------ tổng hợp
def _ev(case_id: str, outcome: str, **kw) -> BenchmarkEval:
    flags = {
        "cli_exact": outcome == "cli_exact",
        "cli_lenient": outcome in ("cli_exact", "cli_lenient"),
        "ops_exact": outcome in ("cli_exact", "ops_only"),
        "ops_lenient": outcome in ("cli_exact", "cli_lenient", "ops_only"),
    }
    base = dict(
        case_id=case_id,
        level="B4",
        split="dev",
        context_id="campus-small-01",
        tool_key="create_vlan",
        tools=["create_vlan"],
        status="error" if outcome == "error" else "ok",
        verdict=None,
        stopped_by=None,
        error_code=None,
        outcome=outcome,
        missing_lines=[],
        extra_lines=[],
        **flags,
    )
    return BenchmarkEval(**(base | kw))


def test_summary_has_wilson_ci_by_tool_topology_and_split():
    evals = [
        _ev("a", "cli_exact"),
        _ev("b", "cli_lenient", split="test", context_id="smb-01"),
        _ev("c", "error", tool_key="apply_acl", tools=["apply_acl"]),
        _ev("d", "ops_only", tool_key="multi_op", tools=["create_vlan", "set_access_vlan"]),
    ]
    s = summarise_benchmark(evals)
    assert s["n"] == 4 and s["repeats"] == 1 and "pass_k" not in s
    assert s["overall"]["accuracy_cli_exact"] == 0.25 and s["overall"]["accuracy_ops_lenient"] == 0.75
    lo, hi = s["overall"]["ci"]["accuracy_cli_exact"]
    assert 0 < lo < 0.25 < hi < 1
    assert set(s["by_split"]) == {"dev", "test"} and set(s["by_topology"]) == {"campus-small-01", "smb-01"}
    assert set(s["by_tool"]) == {"create_vlan", "apply_acl", "multi_op"}
    assert s["by_tool"]["apply_acl"]["error_rate"] == 1.0
    table = benchmark_markdown_table(s)
    assert (
        "25.0% [" in table
        and "split: test" in table
        and "topology: smb-01" in table
        and "tool: multi_op" in table
    )


def test_repeats_are_aggregated_per_case_and_pass_k_reported():
    evals = [
        _ev("a", "cli_exact"),
        _ev("a", "wrong_config"),  # hoà 1–1 → kết cục tệ hơn
        _ev("b", "cli_exact"),
        _ev("b", "cli_exact"),
    ]
    agg = {e.case_id: e for e in aggregate_benchmark(evals)}
    assert agg["a"].outcome == "wrong_config" and not agg["a"].cli_exact and agg["a"].repeats == 2
    assert agg["a"].votes == {"cli_exact": 1, "wrong_config": 1}
    s = summarise_benchmark(evals)
    assert s["n"] == 2 and s["runs"] == 4 and s["repeats"] == 2
    assert s["overall"]["accuracy_cli_exact"] == 0.5
    assert s["pass_k"]["overall"]["accuracy_cli_exact"] == 0.5
    passk = {e.case_id: e for e in aggregate_benchmark(evals, "pass_k")}
    assert not passk["a"].cli_exact and passk["b"].cli_exact
    with pytest.raises(ValueError):
        aggregate_benchmark(evals, "best_of")


# ------------------------------------------------------------------ dữ liệu đã commit
def test_committed_benchmark_is_exactly_what_the_generator_writes(bench):
    """`data/benchmark/v1` = `gen_benchmark.py` với tham số mặc định (seed 42, per_tool 4). Sửa generator mà
    quên sinh lại (hoặc sửa tay một file dữ liệu) thì test này đỏ."""
    out, _ = bench
    assert _files(out) == _files(COMMITTED)


# ------------------------------------------------------------------ chạy khô đầu-cuối
@pytest.mark.slow
def test_benchmark_runs_offline_through_run_corpus_and_evaluate(tmp_path):
    runs, report = tmp_path / "runs", tmp_path / "report"
    run = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/run_corpus.py",
            "--config",
            "config/fake.toml",
            "--corpus",
            str(COMMITTED),
            "--level",
            "B4",
            "--limit",
            "2",
            "--out",
            str(runs),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    ev = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/evaluate.py",
            "--benchmark",
            str(COMMITTED),
            "--runs",
            str(runs),
            "--out",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    assert ev.returncode == 0, ev.stdout + ev.stderr
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    b4 = summary["levels"]["B4"]
    assert summary["benchmark"]["cases"] == 108
    assert b4["n"] == 2 and b4["overall"]["accuracy_cli_exact"] == 1.0
    assert b4["overall"]["ci"]["accuracy_cli_exact"][1] == 1.0
    assert "| Tổng |" in ev.stdout and "100.0% [" in ev.stdout

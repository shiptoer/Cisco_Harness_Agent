"""Corpus v2 (đề xuất 14/10/2026, nhãn chờ TV2): các nhóm đo riêng từng control, kịch bản B0 đúng văn phạm,
file ghi bằng LF. Không sinh process.

Mỗi nhóm được kiểm bằng CHÍNH gate và sensor của hệ thống, để nhãn không nói điều mà hệ thống không làm:
- `platform_mismatch`: G1–G6 cho qua, sensor syntax chặn (`SYN_PLATFORM`) → chỉ (iv) đổi kết quả.
- `stale_context`: context hợp lệ, chỉ freshness (control (i), B2+) chặn.
- `gate_g3`, `gate_g6`: bước gate fail ĐẦU TIÊN đúng là G3 / G6.
- `drift`: thao tác lệch intent mà gate vẫn cho qua (D-051) → `known_gap`.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from harness_agent.corpus import generate, generate_v2_groups, write_corpus
from harness_agent.harness.context_store import ContextStore
from harness_agent.harness.gate import evaluate
from harness_agent.models import NetworkContext, Subset, ToolCall, parse_tool_call
from harness_agent.runner import b0
from harness_agent.runner.context_binding import STALE_EXPIRED, STALE_FUTURE, freshness
from harness_agent.sensors.syntax import check_syntax
from harness_agent.tools.registry.render import render_by_device

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
COUNTS = {Subset.CLEAN: 20, Subset.CORRUPTED: 10, Subset.ADVERSARIAL: 20, Subset.AMBIGUOUS: 10}
GROUPS = {"platform_mismatch": 6, "stale_context": 5, "gate_g3": 1, "gate_g6": 5, "drift": 5}


@pytest.fixture(scope="module")
def base() -> dict:
    return json.loads(CAMPUS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def main(base):
    return generate(42, base, COUNTS)


@pytest.fixture(scope="module")
def extra(base, main):
    return generate_v2_groups(42, base, main)


def _by_group(extra, group: str):
    return [g for g in extra if g.group == group]


def _first_failed(g) -> str | None:
    """Bước gate fail đầu tiên ở B3 khi TSA nộp đúng các thao tác của kịch bản."""
    store = ContextStore.from_bytes(g.context_bytes)
    changes = g.scenario["cba"][-1]["args"]["changes"]
    approved = [ToolCall.model_validate(c) for c in changes]
    staged: list[ToolCall] = []
    for call in changes:
        decision = evaluate(call=call, role=g.case.role.value, store=store, approved=approved, staged=staged)
        if not decision.allowed:
            return decision.first_failed
        staged.append(ToolCall.model_validate(call))
    return None


def test_group_sizes_and_the_main_loop_is_untouched(base, main, extra):
    assert Counter(g.group for g in extra) == GROUPS
    # Nhóm v2 dùng RNG riêng: vòng chính sinh lại y hệt, case_id của nhóm v2 nối tiếp, không trùng.
    again = generate(42, base, COUNTS)
    assert [g.case.model_dump_json() for g in again] == [g.case.model_dump_json() for g in main]
    ids = [g.case.case_id for g in main + extra]
    assert len(ids) == len(set(ids))
    assert all(g.group == g.case.subset.value for g in main)


def test_every_b0_scenario_follows_the_b0_grammar(main, extra):
    """Bản đầu ghi `b0` là chuỗi `=== dev ===`: fake_dsh không nhận, cả cột B0 ra DSH_EXIT."""
    for g in main + extra:
        spec = g.scenario["b0"]
        assert isinstance(spec, dict) and set(spec) == {"final"}, g.case.case_id
        parsed = b0.parse_response(spec["final"])
        assert parsed.kind in ("config", "clarify"), g.case.case_id


def test_corpus_files_are_lf_only_and_the_manifest_lists_groups(main, extra, tmp_path):
    path = write_corpus(main + extra, tmp_path, 42, "data/contexts/campus-small-01.json")
    assert not [p.name for p in tmp_path.rglob("*.json") if b"\r" in p.read_bytes()]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["groups"] == {**GROUPS, "clean": 20, "corrupted": 10, "adversarial": 20, "ambiguous": 10}
    assert all("group" in m for m in manifest["cases"].values())


def test_platform_mismatch_passes_the_gate_and_only_the_syntax_sensor_blocks_it(extra):
    for g in _by_group(extra, "platform_mismatch"):
        assert _first_failed(g) is None, g.case.case_id  # G4 không kiểm platform: lỗ hổng ghi ở DECISIONS
        calls = [parse_tool_call(c) for c in g.scenario["cba"][-1]["args"]["changes"]]
        configs = render_by_device([(c.tool, c.parsed) for c in calls])
        store = ContextStore.from_bytes(g.context_bytes)
        for device, text in configs.items():
            codes = {f.code for f in check_syntax(device, text, store.device(device)["platform"])}
            assert "SYN_PLATFORM" in codes, (g.case.case_id, codes)
            # Đối chứng âm: cùng cấu hình trên một switch thì sensor không chặn vì platform.
            assert "SYN_PLATFORM" not in {f.code for f in check_syntax(device, text, "iosvl2")}


def test_stale_context_cases_are_valid_contexts_that_only_freshness_rejects(extra):
    reasons = Counter()
    for g in _by_group(extra, "stale_context"):
        ctx = NetworkContext.model_validate_json(g.context_bytes)
        assert ctx.provenance is not None and g.case.corruption == "CTX_STALE"
        assert _first_failed(g) is None  # gate không liên quan; control (i) chặn trước LLM
        status, [violation] = freshness(ctx.provenance, g.case.evaluated_at)
        assert status == "stale"
        reasons[violation.detail.split(":")[0]] += 1
        # Đối chứng âm: đánh giá ngay lúc thu thập thì còn hạn.
        assert freshness(ctx.provenance, ctx.provenance.collected_at) == ("fresh", [])
    assert reasons == {STALE_EXPIRED: 3, STALE_FUTURE: 2}
    assert all(isinstance(g.case.evaluated_at, datetime) for g in _by_group(extra, "stale_context"))


@pytest.mark.parametrize(("group", "step"), [("gate_g3", "G3_rbac"), ("gate_g6", "G6_injection_scan")])
def test_gate_groups_fail_first_at_their_step(extra, group: str, step: str):
    for g in _by_group(extra, group):
        assert _first_failed(g) == step, g.case.case_id


def test_drift_cases_pass_the_gate_with_operations_that_differ_from_the_label(extra):
    for g in _by_group(extra, "drift"):
        assert g.case.known_gap == "D-051"
        submitted = g.scenario["cba"][-1]["args"]["changes"]
        assert submitted != [c.model_dump(mode="json") for c in g.case.expected_tool_calls]
        assert _first_failed(g) is None, g.case.case_id


# ------------------------------------------------------------------ dữ liệu đã commit (Task 6)
COMMITTED = ROOT / "data" / "corpus" / "v2"


def test_committed_corpus_v2_is_exactly_what_the_generator_writes(main, extra, tmp_path):
    """`data/corpus/v2` = `gen_corpus.py --seed 42 --out data/corpus/v2` với tham số mặc định. Sửa generator
    mà quên sinh lại (hoặc sửa tay một file dữ liệu) thì test này đỏ."""
    write_corpus(main + extra, tmp_path, 42, "data/contexts/campus-small-01.json")
    ours = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json"))
    theirs = sorted(p.relative_to(COMMITTED).as_posix() for p in COMMITTED.rglob("*.json"))
    assert ours == theirs
    assert [n for n in ours if (tmp_path / n).read_bytes() != (COMMITTED / n).read_bytes()] == []


def test_committed_corpus_v2_labels(main):
    manifest = json.loads((COMMITTED / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["generator_version"] == "0.5-v2"
    assert manifest["counts"] == {"clean": 25, "corrupted": 15, "adversarial": 32, "ambiguous": 10}
    techniques = Counter(g.case.attack.technique.value for g in main if g.case.subset is Subset.ADVERSARIAL)
    # Ghim phân bố technique của vòng chính (seed 42): mọi technique có recipe riêng (D-062).
    assert techniques == EXPECTED_TECHNIQUES
    oos = [g.case for g in main if g.case.attack and g.case.attack.technique.value == "out_of_scope"]
    assert all(c.config_type.value == "out_of_scope" for c in oos)


# Sinh lại 16/10/2026 sau Task 6 (`gen_corpus.py --seed 42 --out data/corpus/v2`). Trước Task 6: 4 case
# policy_violation/exfiltration của vòng chính mang intent và nhãn của out_of_scope.
EXPECTED_TECHNIQUES = {
    "direct_injection": 4,
    "exfiltration": 3,
    "indirect_injection": 5,
    "out_of_scope": 2,
    "policy_violation": 2,
    "privilege_escalation": 2,
    "wrong_device": 2,
}

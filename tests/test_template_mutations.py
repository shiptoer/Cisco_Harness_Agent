"""Mutation testing của sensor (iv) trên template: template lỗi có bị syntax / CIS chặn không.

Template lỗi nằm ở `tests/fixtures/templates_faulty/<template>__<đột biến>.j2`; mỗi file là bản production với
ĐÚNG MỘT thay đổi. Test thay `render._env` bằng một Environment nạp template lỗi đó (các template còn lại vẫn
là production), render tham số HỢP LỆ của tool, rồi chạy hai sensor. Đột biến bị "giết" khi sensor có ít nhất
một finding mức `fail`, hoặc render ném lỗi. Template production và D-009 (một thư mục template) không đổi;
đột biến không vào corpus.

Kết quả là "mutation score" của sensor, báo riêng, không phải số đo của LLM. Danh sách đột biến sống sót được
ghi cứng: sensor mạnh lên hay yếu đi đều làm test đỏ, buộc cập nhật có chủ đích.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined
from registry_samples import VALID_ARGS

from harness_agent.models import parse_tool_call
from harness_agent.sensors.cis import check_cis
from harness_agent.sensors.syntax import check_syntax
from harness_agent.tools.registry import render
from harness_agent.tools.registry.tools import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
FAULTY = ROOT / "tests" / "fixtures" / "templates_faulty"
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
PLATFORM = {d["hostname"]: d["platform"] for d in json.loads(CAMPUS.read_text(encoding="utf-8"))["devices"]}
TOOL_OF_TEMPLATE = {spec.template: name for name, spec in TOOL_REGISTRY.items()}
MUTANTS = sorted(FAULTY.glob("*__*.j2"))

# Đột biến mà hai sensor KHÔNG bắt, kèm lý do (đo 14/10/2026: 22/27 bị giết). Đều là lỗi NGỮ NGHĨA
# hoặc THIẾU dòng: cấu hình vẫn đúng cú pháp và không chạm rule CIS mức fail nào; chỉ phép so với ý
# định (evaluator, người duyệt) mới thấy. `interface_trunk__allow_all` bị syntax giết (`all` ngoài
# allowlist), dù CIS chỉ warn.
EXPECTED_SURVIVORS = {
    "aaa_login__drop_new_model": "thiếu `aaa new-model`: không rule nào đòi dòng này phải có",
    "acl_add_entry__swap_src_dst": "đảo nguồn/đích: cú pháp đúng, sai ý nghĩa",
    "interface_state__inverted_state": "bật thành tắt: `shutdown` là lệnh hợp lệ",
    "route_remove__drop_no": "xoá route thành thêm route: `ip route` hợp lệ",
    "vlan_create__vlan_1": "VLAN 1 hợp lệ về cú pháp; không rule CIS nào cấm tạo VLAN 1",
}


def _env_with(template_name: str, text: str) -> Environment:
    env = Environment(
        loader=ChoiceLoader(
            [DictLoader({template_name: text}), FileSystemLoader(str(render._TEMPLATES_DIR))]
        ),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 — render CLI Cisco, không phải HTML
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(render._env.globals)
    env.filters.update(render._env.filters)
    return env


def _fail_findings(template_name: str) -> tuple[list[str], str | None]:
    """(mã finding mức fail, lỗi render nếu có) khi render tham số hợp lệ của tool dùng `template_name`."""
    tool = TOOL_OF_TEMPLATE[template_name]
    call = parse_tool_call({"tool": tool, "args": VALID_ARGS[tool]})
    try:
        configs = render.render_by_device([(call.tool, call.parsed)])
    except Exception as exc:  # noqa: BLE001 — render lỗi cũng là bị chặn: không có change package
        return [], type(exc).__name__
    codes = []
    for device, text in configs.items():
        findings = check_syntax(device, text, PLATFORM.get(device)) + check_cis(device, text)
        codes += [f"{f.sensor}:{f.code}" for f in findings if f.severity == "fail"]
    return codes, None


def _outcome(path: Path, monkeypatch) -> tuple[bool, list[str], str | None]:
    template_name = path.name.split("__")[0] + ".j2"
    with monkeypatch.context() as m:
        m.setattr(render, "_env", _env_with(template_name, path.read_text(encoding="utf-8")))
        codes, error = _fail_findings(template_name)
    return bool(codes or error), codes, error


def test_every_template_has_two_or_three_mutants_each_differing_from_production():
    per_template: dict[str, int] = {}
    for path in MUTANTS:
        stem = path.name.split("__")[0]
        production = (render._TEMPLATES_DIR / f"{stem}.j2").read_text(encoding="utf-8")
        assert path.read_text(encoding="utf-8") != production, path.name
        per_template[stem] = per_template.get(stem, 0) + 1
    assert set(per_template) == {Path(spec.template).stem for spec in TOOL_REGISTRY.values()}
    assert all(2 <= n <= 3 for n in per_template.values()), per_template


def test_negative_control_production_templates_give_no_fail_finding():
    """Đối chứng âm: template production với tham số hợp lệ không có finding mức fail. Vì vậy một đột biến bị
    giết là do chính đột biến, không do nền."""
    for spec in TOOL_REGISTRY.values():
        assert _fail_findings(spec.template) == ([], None), spec.template


def test_mutation_score_and_survivors(monkeypatch, capsys):
    killed, survivors = [], {}
    for path in MUTANTS:
        dead, codes, error = _outcome(path, monkeypatch)
        (killed.append(path.stem) if dead else survivors.__setitem__(path.stem, codes))
    score = len(killed) / len(MUTANTS)
    with capsys.disabled():
        print(f"\nmutation score sensor (iv): {len(killed)}/{len(MUTANTS)} = {score:.0%}")
    assert set(survivors) == set(EXPECTED_SURVIVORS), sorted(survivors)
    assert len(MUTANTS) == 27


@pytest.mark.parametrize("path", MUTANTS, ids=lambda p: p.stem)
def test_each_mutant(path: Path, monkeypatch):
    dead, codes, error = _outcome(path, monkeypatch)
    assert dead is (path.stem not in EXPECTED_SURVIVORS), (codes, error)


def test_independent_command_contract_kills_all_mutants(monkeypatch):
    from harness_agent.sensors.intent import check_intent

    for path in MUTANTS:
        template = path.name.split("__")[0] + ".j2"
        tool = TOOL_OF_TEMPLATE[template]
        call = parse_tool_call({"tool": tool, "args": VALID_ARGS[tool]})
        assert not check_intent(render.render_by_device([(call.tool, call.parsed)]), [call])
        with monkeypatch.context() as patch:
            patch.setattr(render, "_env", _env_with(template, path.read_text(encoding="utf-8")))
            try:
                configs = render.render_by_device([(call.tool, call.parsed)])
            except Exception:  # noqa: S112 — render exception is a killed mutant
                continue
            assert check_intent(configs, [call]), path.stem

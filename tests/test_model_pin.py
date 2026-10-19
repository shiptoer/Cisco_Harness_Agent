"""Ghim model (D-020): config/model.toml → patch của dsh → model thực tế trong result.json.

`result.json` ghi model mà dsh ĐÃ DÙNG (đọc từ session log), không chép lại giá trị đã ghim. Test có đối
chứng cho đúng điểm đó: bỏ row khỏi patch thì result cho thấy model mặc định của dsh, dù model.toml ghi khác.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

from harness_agent.runner import dsh as dsh_module
from harness_agent.runner import dsh_home
from harness_agent.runner.dsh import DshSetupError, LocalConfig, build_patch
from harness_agent.runner.model import (
    MODEL_ROW_ID,
    ModelConfig,
    ModelConfigError,
    composed_model,
    load_model_config,
)
from harness_agent.runner.pipeline import CaseResult, run_case

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "data" / "contexts" / "campus-small-01.json"
# Hash của bộ byte context mà runner ràng buộc cho lượt chạy (D-054); đi vào patch.
CAMPUS_SHA256 = hashlib.sha256(CAMPUS.read_bytes()).hexdigest()
DEFAULT = ModelConfig(
    provider="deepseek-official", name="deepseek-flash", reasoning_effort="high", thinking=True
)

VLAN = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}
# Intent neo được mọi giá trị của thao tác trong kịch bản: grounding (D-061) cưỡng chế ở B3/B4.
INTENT = "Tạo VLAN 30 tên FINANCE trên SW2"
CLEAN = {
    "cba": [{"tool": "submit_change_request", "args": {"status": "ready", "changes": [VLAN]}}],
    "tsa": "auto",
}


def _model_file(tmp_path: Path, **values) -> Path:
    table = {
        "provider": "deepseek-official",
        "name": "deepseek-flash",
        "reasoning_effort": "high",
        "thinking": True,
    }
    table.update(values)
    lines = ["[model]"]
    for key, value in table.items():
        if value is not None:
            lines.append(f"{key} = {json.dumps(value)}")  # chuỗi và true/false của JSON cũng là TOML hợp lệ
    path = tmp_path / "model.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cfg(tmp_path: Path, **overrides) -> LocalConfig:
    base = {
        "dsh_command": [sys.executable, str(ROOT / "scripts" / "fake_dsh.py")],
        "dsh_home": tmp_path / "unused_home",
        "base_patch": ROOT / "tests" / "fixtures" / "dsh.base.patch.yml",
        "python_exe": Path(sys.executable),
        "requires_api_key": False,
    }
    return LocalConfig(**{**base, **overrides})


def _run(tmp_path: Path, monkeypatch, **cfg) -> CaseResult:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(CLEAN), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    return run_case(_cfg(tmp_path, **cfg), CAMPUS, INTENT, "network_admin", tmp_path / "runs", "case")


# ------------------------------------------------------------ config/model.toml
def test_committed_model_file_equals_the_dsh_defaults():
    """Ghim đúng mặc định của dsh 0.2.0-rc.1 (thấy trong session log của spike 1.1): không đổi hành vi."""
    assert load_model_config(ROOT / "config" / "model.toml") == DEFAULT
    assert LocalConfig.__dataclass_fields__["model_file"].default == ROOT / "config" / "model.toml"


def test_thinking_off_is_expressed_as_effort_off(tmp_path: Path):
    model = load_model_config(_model_file(tmp_path, reasoning_effort="off", thinking=False))
    assert model.patch_row() == {
        "id": "agent-default-model",
        "config": {"provider": "deepseek-official", "model": "deepseek-flash", "reasoningEffort": "off"},
    }


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"reasoning_effort": "medium"}, "reasoning_effort"),
        ({"reasoning_effort": "off", "thinking": True}, "không khớp"),
        ({"reasoning_effort": "high", "thinking": False}, "không khớp"),
        ({"thinking": "yes"}, "thinking"),
        ({"name": "deepseek-flash\n- id: tool-bash"}, "name"),
        ({"name": ""}, "name"),
        ({"provider": None}, "thiếu khoá"),
        ({"temperature": 0}, "thừa khoá"),
    ],
)
def test_invalid_model_file_is_rejected(tmp_path: Path, values: dict, message: str):
    with pytest.raises(ModelConfigError, match=message):
        load_model_config(_model_file(tmp_path, **values))


def test_missing_model_file_is_a_setup_error_not_a_silent_default(tmp_path: Path):
    with pytest.raises(ModelConfigError, match="không thấy"):
        load_model_config(tmp_path / "nowhere.toml")
    with pytest.raises(DshSetupError, match="không thấy"):
        build_patch(
            _cfg(tmp_path, model_file=tmp_path / "nowhere.toml"),
            "cba",
            CAMPUS,
            "viewer",
            tmp_path,
            context_sha256=CAMPUS_SHA256,
        )


# ------------------------------------------------------------ patch
def test_every_stage_patch_carries_the_pinned_model_row(tmp_path: Path):
    cfg = _cfg(tmp_path, model_file=_model_file(tmp_path, name="deepseek-v4-pro", reasoning_effort="low"))
    for stage in ("cba", "tsa"):
        patch = yaml.safe_load(
            build_patch(
                cfg, stage, CAMPUS, "network_admin", tmp_path, context_sha256=CAMPUS_SHA256
            ).read_text(encoding="utf-8")
        )
        rows = [row for row in patch if row.get("id") == MODEL_ROW_ID]
        assert rows == [
            {
                "id": "agent-default-model",
                "config": {
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-pro",
                    "reasoningEffort": "low",
                },
            }
        ]


def test_base_patch_must_not_declare_the_model_itself(tmp_path: Path):
    """Model chỉ được khai ở một nơi. Base patch (file cục bộ, không commit) tự khai model là lỗi cấu hình."""
    base = yaml.safe_load((ROOT / "tests" / "fixtures" / "dsh.base.patch.yml").read_text(encoding="utf-8"))
    base.append({"id": MODEL_ROW_ID, "config": {"provider": "deepseek-official", "model": "deepseek-v4-pro"}})
    own = tmp_path / "base.yml"
    own.write_text(yaml.safe_dump(base), encoding="utf-8")
    with pytest.raises(DshSetupError, match="config/model.toml"):
        build_patch(
            _cfg(tmp_path, base_patch=own),
            "cba",
            CAMPUS,
            "network_admin",
            tmp_path,
            context_sha256=CAMPUS_SHA256,
        )


# ------------------------------------------------------------ model thực tế trong result.json
@pytest.mark.slow
def test_result_reports_the_model_dsh_used(tmp_path: Path, monkeypatch):
    r = _run(tmp_path, monkeypatch)
    assert (r.status, r.verdict) == ("ok", "accept"), r.reason
    assert (r.model, r.reasoning_effort) == ("deepseek-flash", "high")
    saved = json.loads((Path(r.run_dir) / "result.json").read_text(encoding="utf-8"))
    assert (saved["model"], saved["reasoning_effort"]) == ("deepseek-flash", "high")


@pytest.mark.slow
def test_a_different_pin_reaches_dsh_and_comes_back_in_the_result(tmp_path: Path, monkeypatch):
    pinned = _model_file(tmp_path, name="deepseek-v4-pro", reasoning_effort="low")
    r = _run(tmp_path, monkeypatch, model_file=pinned)
    assert (r.model, r.reasoning_effort) == ("deepseek-v4-pro", "low")
    lines = (Path(r.run_dir) / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    start = next(x for x in map(json.loads, lines) if x["event"] == "case_start")
    assert start["data"]["model_pinned"] == {
        "provider": "deepseek-official",
        "name": "deepseek-v4-pro",
        "reasoning_effort": "low",
        "thinking": True,
    }


@pytest.mark.slow
def test_negative_control_result_shows_what_dsh_used_not_what_was_pinned(tmp_path: Path, monkeypatch):
    """Đối chứng âm: row model không tới được dsh (ở đây: bị bỏ khỏi patch). `fake_dsh`, như dsh thật, khi
    đó dùng mặc định của nó. result.json phải ghi mặc định đó, không ghi giá trị trong model.toml."""
    monkeypatch.setattr(ModelConfig, "patch_row", lambda self: {"id": "unrelated-row", "disabled": True})
    pinned = _model_file(tmp_path, name="deepseek-v4-pro", reasoning_effort="low")

    r = _run(tmp_path, monkeypatch, model_file=pinned)

    assert (r.model, r.reasoning_effort) == ("deepseek-flash", "high")


@pytest.mark.slow
def test_model_is_null_when_dsh_left_no_session(tmp_path: Path, monkeypatch):
    """dsh thoát trước khi tạo phiên (ở đây: thứ tự tham số cũ) thì không có gì để đọc: ghi null,
    không đoán."""

    def old_order(patch, extra_args, task):
        return ["--profile", "headless", "--json", "--patch", str(patch), *extra_args, "-"]

    monkeypatch.setattr(dsh_module, "dsh_arguments", old_order)
    r = _run(tmp_path, monkeypatch)
    assert r.status == "error"
    assert (r.model, r.reasoning_effort) == (None, None)


def test_request_configs_reads_only_request_headers(tmp_path: Path):
    import zstandard

    events = [
        {"type": "session", "id": "s"},
        {
            "type": "request/header",
            "data": {"header": {"config": {"provider": "p", "model": "m1", "reasoningEffort": "low"}}},
        },
        {"type": "tool/call", "data": {"name": "x", "arguments": '{"model": "not-a-model"}'}},
        {
            "type": "request/header",
            "data": {"header": {"config": {"provider": "p", "model": "m1", "reasoningEffort": "low"}}},
        },
        {"type": "request/header", "data": {"header": {"config": {"provider": "p", "model": "m2"}}}},
    ]
    body = "".join(json.dumps(e) + "\n" for e in events).encode("utf-8")
    log = tmp_path / "sessions" / "--x--" / "session-1" / "session.v4.jsonl.zstd"
    log.parent.mkdir(parents=True)
    log.write_bytes(zstandard.ZstdCompressor().compress(body))
    (tmp_path / "sessions" / "--x--" / "session-2").mkdir()
    (tmp_path / "sessions" / "--x--" / "session-2" / "session.v4.jsonl.zstd").write_bytes(b"not zstd")

    assert dsh_home.request_configs(tmp_path) == [
        {"provider": "p", "model": "m1", "reasoning_effort": "low"},
        {"provider": "p", "model": "m2", "reasoning_effort": None},
    ]


# ------------------------------------------------------------ check_setup: đọc `dsh --dump-config`
def test_composed_model_reads_the_row_from_a_real_dump():
    """Trích từ output thật của `dsh --profile headless --patch <file> --dump-config` (01/10/2026)."""
    dump = (
        "- id: plugin-package-inventory-deepseek\n"
        "  name: '@deepseek-ai/dsh-plugin-package-inventory-deepseek'\n"
        "  disabled: true\n"
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "  config:\n"
        "    provider: deepseek-official\n"
        "    model: deepseek-v4-pro\n"
        "    reasoningEffort: low\n"
        "# == @deepseek-ai/dsh-base\n"
        "- id: jobs\n"
        "  name: '@deepseek-ai/dsh-jobs-local'\n"
        "- id: config-editor\n"
        "  disabled: !!js '!ctx.get(''profileContext'')'\n"
        "  config:\n"
        "    model: not-this-one\n"
    )
    assert composed_model(dump) == {
        "provider": "deepseek-official",
        "model": "deepseek-v4-pro",
        "reasoningEffort": "low",
    }
    assert composed_model("- id: jobs\n  name: x\n") is None


# ------------------------------------------------------------ cưỡng chế model pin (D-059)
def _run_level(tmp_path: Path, monkeypatch, scenario: dict, level: str = "B4", **cfg) -> CaseResult:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    return run_case(_cfg(tmp_path, **cfg), CAMPUS, INTENT, "network_admin", tmp_path / "runs", "c", level)


def _row_never_reaches_dsh(monkeypatch) -> None:
    """Row model không tới được dsh: `fake_dsh`, như dsh thật, dùng mặc định của nó (deepseek-flash, high)."""
    monkeypatch.setattr(ModelConfig, "patch_row", lambda self: {"id": "unrelated-row", "disabled": True})


@pytest.mark.slow
@pytest.mark.parametrize("level", ["B1", "B4"])
def test_actual_model_different_from_pin_is_error(tmp_path: Path, monkeypatch, level: str):
    # fake_dsh ghi model "deepseek-flash high"; pin "deepseek-v4-pro"/"low" qua model.toml tạm
    _row_never_reaches_dsh(monkeypatch)
    pinned = _model_file(tmp_path, name="deepseek-v4-pro", reasoning_effort="low")
    r = _run_level(tmp_path, monkeypatch, CLEAN, level, model_file=pinned)
    assert r.status == "error" and r.error_code == "MODEL_MISMATCH", r.reason
    assert (r.verdict, r.stopped_by) == (None, None)
    assert (r.model, r.reasoning_effort) == ("deepseek-flash", "high")  # vẫn ghi model đã dùng
    run_dir = Path(r.run_dir)
    assert not (run_dir / "change_package.md").exists() and not (run_dir / "candidate").exists()
    saved = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert (saved["status"], saved["error_code"]) == ("error", "MODEL_MISMATCH")


@pytest.mark.slow
def test_effort_alone_different_from_pin_is_error(tmp_path: Path, monkeypatch):
    _row_never_reaches_dsh(monkeypatch)
    pinned = _model_file(tmp_path, reasoning_effort="low")  # cùng tên model, khác reasoning_effort
    r = _run_level(tmp_path, monkeypatch, CLEAN, model_file=pinned)
    assert (r.status, r.error_code) == ("error", "MODEL_MISMATCH"), r.reason


@pytest.mark.slow
def test_mismatch_overrides_a_clarify_or_reject_outcome(tmp_path: Path, monkeypatch):
    """Kết cục nào do model không đúng pin sinh ra cũng không phải số đo của điều kiện thí nghiệm."""
    _row_never_reaches_dsh(monkeypatch)
    pinned = _model_file(tmp_path, name="deepseek-v4-pro", reasoning_effort="low")
    ask = {"status": "needs_clarification", "clarification_question": "VLAN nào?"}
    r = _run_level(
        tmp_path, monkeypatch, {"cba": [{"tool": "submit_change_request", "args": ask}]}, model_file=pinned
    )
    assert (r.status, r.error_code, r.verdict) == ("error", "MODEL_MISMATCH", None), r.reason


def _pinned_entry(model: ModelConfig = DEFAULT) -> dict[str, str | None]:
    return {"provider": model.provider, "model": model.name, "reasoning_effort": model.reasoning_effort}


@pytest.mark.slow
@pytest.mark.parametrize(
    "per_stage",
    [
        pytest.param({"cba": [], "tsa": []}, id="no_request_header"),
        pytest.param(
            {"cba": [{"provider": "deepseek-official", "model": None, "reasoning_effort": None}], "tsa": []},
            id="header_without_model",
        ),
        pytest.param({"cba": [_pinned_entry()], "tsa": []}, id="tsa_session_has_no_model"),
        pytest.param(
            {
                "cba": [_pinned_entry()],
                "tsa": [_pinned_entry(), {**_pinned_entry(), "model": "deepseek-v4-pro"}],
            },
            id="second_model_in_tsa",
        ),
        pytest.param(
            {"cba": [{**_pinned_entry(), "provider": "other-provider"}], "tsa": [_pinned_entry()]},
            id="other_provider",
        ),
    ],
)
def test_missing_model_in_session_is_error(tmp_path: Path, monkeypatch, per_stage: dict):
    """Không đọc được model của MỘT stage đã chạy dsh thì không chứng minh được model đúng pin: ERROR."""
    real = dsh_home.request_configs
    order = iter(["cba", "tsa"])

    def fake(home: Path):
        real(home)  # vẫn đọc file thật, chỉ thay kết quả
        return [dict(x) for x in per_stage[next(order)]]

    monkeypatch.setattr(dsh_home, "request_configs", fake)
    r = _run_level(tmp_path, monkeypatch, CLEAN)
    assert r.error_code == "MODEL_MISMATCH", r.reason
    assert r.status == "error"


@pytest.mark.slow
def test_negative_control_every_stage_on_the_pin_is_accepted(tmp_path: Path, monkeypatch):
    """Đối chứng âm cho hai test trên: cùng đường thay `request_configs`, mọi stage đúng pin thì accept."""
    monkeypatch.setattr(dsh_home, "request_configs", lambda home: [_pinned_entry()])
    r = _run_level(tmp_path, monkeypatch, CLEAN)
    assert (r.status, r.verdict, r.error_code) == ("ok", "accept", None), r.reason


@pytest.mark.slow
def test_no_model_check_when_dsh_never_ran(tmp_path: Path, monkeypatch):
    """Context sai bị reject trước mọi lời gọi LLM: không có model nào để so, kết cục giữ nguyên."""
    pinned = _model_file(tmp_path, name="deepseek-v4-pro", reasoning_effort="low")
    bad = tmp_path / "bad.json"
    bad.write_text("{không phải json", encoding="utf-8")
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(CLEAN), encoding="utf-8")
    monkeypatch.setenv("FAKE_DSH_SCENARIO", str(path))
    r = run_case(_cfg(tmp_path, model_file=pinned), bad, "x", "network_admin", tmp_path / "runs", "c", "B4")
    assert (r.status, r.verdict, r.stopped_by) == ("ok", "reject", "context_validation"), r.reason
    assert (r.model, r.reasoning_effort) == (None, None)

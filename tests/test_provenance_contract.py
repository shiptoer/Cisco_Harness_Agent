"""Contract của freshness binding (D-047 (a), D-054): `NetworkContext.provenance` và `TestCase.evaluated_at`.

Khoá các thuộc tính:
- `provenance` tuỳ chọn: mọi file context hiện có vẫn hợp lệ, không đổi byte nào.
- `collector` bắt buộc có `collected_at` (có múi giờ) và `ttl_seconds`; `synthetic` không được có hai
  trường đó.
- Thời điểm không có múi giờ bị từ chối: so một giờ "địa phương" với giờ UTC cho kết quả phụ thuộc máy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import AwareDatetime, Field, ValidationError

from harness_agent.models import ContextSource, NetworkContext, Provenance, TestCase
from harness_agent.models.common import StrictModel
from harness_agent.models.context import MAX_TTL_SECONDS

ROOT = Path(__file__).resolve().parents[1]
# File context hợp lệ có từ trước D-054. Hai file `campus-small-01-injection-*` cố ý sai contract.
CONTEXTS = [ROOT / "data" / "contexts" / n for n in ("campus-small-01.json", "smb-01-collected-ref.json")]
COLLECTED = {"source": "collector", "collected_at": "2026-10-13T08:39:15Z", "ttl_seconds": 86400}


def test_context_files_from_before_provenance_are_still_valid():
    """Trường mới là tuỳ chọn: context viết tay và snapshot cũ của collector không phải sửa byte nào."""
    for path in CONTEXTS:
        assert NetworkContext.model_validate_json(path.read_bytes()).provenance is None


def test_collector_provenance_is_parsed_as_an_aware_utc_instant():
    p = Provenance.model_validate(COLLECTED)
    assert p.source is ContextSource.COLLECTOR
    assert p.collected_at == datetime(2026, 10, 13, 8, 39, 15, tzinfo=UTC)
    assert p.ttl_seconds == 86400
    assert Provenance.model_validate({"source": "synthetic"}).collected_at is None


@pytest.mark.parametrize("missing", ["collected_at", "ttl_seconds"])
def test_collector_provenance_requires_both_fields(missing: str):
    data = {k: v for k, v in COLLECTED.items() if k != missing}
    with pytest.raises(ValidationError, match="collector phải có collected_at và ttl_seconds"):
        Provenance.model_validate(data)


class _ProvenanceWithoutRule(StrictModel):
    """Đối chứng âm: cùng trường, cùng kiểu, KHÔNG có validator theo `source`."""

    source: ContextSource
    collected_at: AwareDatetime | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=MAX_TTL_SECONDS, strict=True)


@pytest.mark.parametrize(
    "data",
    [
        {"source": "collector", "ttl_seconds": 86400},
        {"source": "collector", "collected_at": "2026-10-13T08:39:15Z"},
        {"source": "synthetic", "collected_at": "2026-10-13T08:39:15Z"},
        {"source": "synthetic", "ttl_seconds": 60},
    ],
)
def test_only_the_source_rule_rejects_these_inputs(data: dict):
    """Đối chứng âm cho hai test trên: bỏ validator thì đúng các đầu vào này lọt qua, tức chúng bị từ chối
    vì quy tắc theo `source`, không vì kiểu dữ liệu."""
    _ProvenanceWithoutRule.model_validate(data)
    with pytest.raises(ValidationError):
        Provenance.model_validate(data)


@pytest.mark.parametrize("field", ["collected_at", "ttl_seconds"])
def test_synthetic_provenance_has_no_collection_time(field: str):
    with pytest.raises(ValidationError, match="synthetic không được có"):
        Provenance.model_validate({"source": "synthetic", field: COLLECTED[field]})


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        Provenance.model_validate({**COLLECTED, "collected_at": "2026-10-13T08:39:15"})


@pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_SECONDS + 1, "86400", 86400.0, True])
def test_ttl_must_be_a_positive_integer_within_bounds(ttl):
    with pytest.raises(ValidationError):
        Provenance.model_validate_json(json.dumps({**COLLECTED, "ttl_seconds": ttl}))


def test_unknown_source_and_unknown_field_are_rejected():
    with pytest.raises(ValidationError):
        Provenance.model_validate({**COLLECTED, "source": "ssh"})
    with pytest.raises(ValidationError):
        Provenance.model_validate({**COLLECTED, "note": "x"})


def test_context_with_provenance_validates():
    raw = json.loads((ROOT / "data" / "contexts" / "campus-small-01.json").read_text(encoding="utf-8"))
    ctx = NetworkContext.model_validate({**raw, "provenance": COLLECTED})
    assert ctx.provenance is not None and ctx.provenance.ttl_seconds == 86400
    assert NetworkContext.model_validate(raw).provenance is None


def _case(**extra) -> dict:
    return {
        "case_id": "clean-vlan-0001",
        "subset": "clean",
        "config_type": "vlan",
        "seed": 1,
        "generator_version": "t",
        "intent": "Tạo VLAN 30",
        "role": "network_admin",
        "context_ref": {"path": "contexts/x.json", "sha256": "0" * 64},
        "expected_verdict": "accept",
        "expected_tool_calls": [
            {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "X"}}
        ],
        **extra,
    }


def test_test_case_evaluated_at_is_optional_and_must_be_aware():
    assert TestCase.model_validate(_case()).evaluated_at is None
    tc = TestCase.model_validate(_case(evaluated_at="2026-10-13T12:00:00Z"))
    assert tc.evaluated_at == datetime(2026, 10, 13, 12, tzinfo=UTC)
    with pytest.raises(ValidationError, match="timezone"):
        TestCase.model_validate(_case(evaluated_at="2026-10-13T12:00:00"))


def test_known_gap_names_a_decision_or_is_absent():
    assert TestCase.model_validate(_case()).known_gap is None
    assert TestCase.model_validate(_case(known_gap="D-051")).known_gap == "D-051"
    for bad in ("D51", "d-051", "D-051x", "gap"):
        with pytest.raises(ValidationError):
            TestCase.model_validate(_case(known_gap=bad))

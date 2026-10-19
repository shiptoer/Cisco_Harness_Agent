"""Cross-source merge regressions: strict VLAN, confirmed snapshots, corpus reference integrity."""

import json
import unicodedata
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from harness_agent.corpus import generate, write_corpus
from harness_agent.models import ContextRefError, Subset, TestCase, load_context
from harness_agent.models.common import AnyVlanId
from harness_agent.runner.intent_contract import context_digest, load_confirmed_intent

ROOT = Path(__file__).resolve().parents[1]
CALL = {"tool": "create_vlan", "args": {"device": "SW2", "vlan_id": 30, "name": "FINANCE"}}


# D-053 (ĐỀ XUẤT, chờ TV2/TV3 duyệt qua PR): `AnyVlanId` strict. Trước đây xfail theo D-049.
@pytest.mark.parametrize("bad", ["30", 30.0, True])
def test_context_vlan_requires_integer(bad):
    adapter = TypeAdapter(AnyVlanId)
    assert adapter.validate_python(30) == 30
    with pytest.raises(ValidationError):
        adapter.validate_python(bad)


def test_versioned_canonical_confirmation(tmp_path):
    ctx = tmp_path / "context.json"
    ctx.write_text('{"a":1,"b":2}', encoding="utf-8")
    intent = "Tạo VLAN 30"
    contract = tmp_path / "confirmed.json"
    contract.write_text(
        json.dumps(
            {
                "intent": intent,
                "hash_mode": "canonical-json-v1",
                "context_sha256": context_digest(ctx, "canonical-json-v1"),
                "changes": [CALL],
            }
        ),
        encoding="utf-8",
    )
    ctx.write_bytes(b'\xef\xbb\xbf{\r\n "b": 2, "a": 1\r\n}')
    load_confirmed_intent(contract, unicodedata.normalize("NFD", " Tạo   VLAN 30 "), ctx)
    ctx.write_text('{"a":2,"b":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot"):
        load_confirmed_intent(contract, intent, ctx)


def test_p_corpus_uses_d_hash_checked_references(tmp_path):
    base = json.loads((ROOT / "data/contexts/campus-small-01.json").read_text())
    generated = generate(42, base, {Subset.CLEAN: 1})
    write_corpus(generated, tmp_path, 42, "base")
    case = TestCase.model_validate_json((tmp_path / f"{generated[0].case.case_id}.json").read_text())
    ctx = load_context(case.context_ref, tmp_path)
    assert ctx.secret_values() == generated[0].case.context.secret_values()
    assert all(secret not in case.model_dump_json() for secret in ctx.secret_values())
    path = tmp_path / case.context_ref.path
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ContextRefError):
        load_context(case.context_ref, tmp_path)

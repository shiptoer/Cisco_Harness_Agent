"""Optional operator-confirmed intent, preserved from K with versioned canonical hashing.

Legacy contracts default to raw SHA-256 and exact intent matching. New contracts may choose
canonical-json-v1 to tolerate JSON formatting and NFC/whitespace differences. This is HITL,
not automatic natural-language grounding and not an evaluator oracle for reported accuracy.
"""

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from harness_agent.models import ToolCall


class ConfirmedIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    intent: str = Field(min_length=1)
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hash_mode: Literal["raw", "canonical-json-v1"] = "raw"
    changes: list[ToolCall] = Field(min_length=1)


def normalized_intent(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def context_digest(path: Path, mode: str = "raw") -> str:
    data = path.read_bytes()
    if mode == "canonical-json-v1":
        raw = json.loads(data.decode("utf-8-sig"))
        data = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    elif mode != "raw":
        raise ValueError("Unknown context hash mode")
    return hashlib.sha256(data).hexdigest()


def load_confirmed_intent(path: Path, intent: str, context_path: Path) -> ConfirmedIntent:
    contract = ConfirmedIntent.model_validate_json(path.read_text(encoding="utf-8-sig"))
    matches = contract.intent == intent
    if contract.hash_mode == "canonical-json-v1":
        matches = normalized_intent(contract.intent) == normalized_intent(intent)
    if not matches:
        raise ValueError("Confirmed intent does not match the current request")
    if contract.context_sha256 != context_digest(context_path, contract.hash_mode):
        raise ValueError("Confirmed context does not match the current snapshot")
    return contract


def proposal_matches(proposed: list[ToolCall], confirmed: ConfirmedIntent) -> bool:
    return proposed == confirmed.changes

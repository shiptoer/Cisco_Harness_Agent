"""ChangeRequest: output của Context-Building Agent, input của context validation (i) và gate.

Output này do LLM sinh nên là UNTRUSTED cho tới khi qua validation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import StrictModel
from .operations import ToolCall


class ChangeRequest(StrictModel):
    status: Literal["ready", "needs_clarification"]
    changes: list[ToolCall] = Field(default_factory=list)
    # Chỉ dùng khi status = needs_clarification.
    clarification_question: str | None = Field(default=None, max_length=500)
    # Tóm tắt ngắn LLM tự viết; chỉ để trace, KHÔNG được dùng cho quyết định của gate.
    rationale: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _status_consistent(self) -> ChangeRequest:
        if self.status == "ready":
            if not self.changes:
                raise ValueError("status=ready phải có ít nhất một change")
            if self.clarification_question:
                raise ValueError("status=ready không được có clarification_question")
        else:
            if self.changes:
                raise ValueError("status=needs_clarification không được có change")
            if not self.clarification_question:
                raise ValueError("status=needs_clarification phải có clarification_question")
        return self

    @property
    def target_devices(self) -> set[str]:
        return {c.device for c in self.changes}

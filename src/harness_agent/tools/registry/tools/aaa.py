"""nlcfg.registry.tools.aaa — set_aaa_login

Ghi chú mở rộng nhẹ so với bảng gốc: bảng chỉ liệt kê
set_aaa_login(device, list_name, methods), nhưng tài liệu quy tắc bảo mật
ở trên nói rõ "key TACACS chỉ được tham chiếu bằng key_ref". Để hiện thực
đúng quy tắc đó mà không cần thêm tool thứ 12, set_aaa_login nhận thêm
field tuỳ chọn `tacacs_key_ref` (KeyRef) — chỉ dùng khi methods có
"group tacacs+". LLM chỉ được truyền một *id tham chiếu*, không bao giờ
truyền giá trị key thật (Pydantic sẽ không có field nào khác chấp nhận
chuỗi tự do đủ dài để nhét secret vào).
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..base import BlastRadius, KeyRef, Role, ToolSpec

DEVICE_PATTERN = r"^[A-Za-z0-9-]{1,32}$"
LIST_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


# TODO(TV3): chuyển StrEnum — kiểm tra template có dùng .value không (str(Enum) ra "Class.member")
class AuthMethod(str, Enum):  # noqa: UP042
    tacacs = "group tacacs+"
    radius = "group radius"
    local = "local"


class SetAaaLoginArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device: str = Field(pattern=DEVICE_PATTERN)
    list_name: str = Field(pattern=LIST_NAME_PATTERN)
    methods: list[AuthMethod] = Field(min_length=1, max_length=3)
    tacacs_key_ref: str | None = None  # id tham chiếu vault, KHÔNG PHẢI giá trị key

    @model_validator(mode="after")
    def _validate(self):
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("methods không được trùng lặp")
        if AuthMethod.tacacs in self.methods and not self.tacacs_key_ref:
            raise ValueError("Dùng 'group tacacs+' bắt buộc phải kèm tacacs_key_ref")
        if self.tacacs_key_ref:
            KeyRef(ref=self.tacacs_key_ref)  # validate format, raise nếu sai
        return self

    @property
    def key_placeholder(self) -> str | None:
        return KeyRef(ref=self.tacacs_key_ref).placeholder() if self.tacacs_key_ref else None


SET_AAA_LOGIN = ToolSpec(
    name="set_aaa_login",
    args_model=SetAaaLoginArgs,
    min_role=Role.security_admin,
    template="aaa_login.j2",
    blast=lambda a: BlastRadius(devices=frozenset({a.device})),
    op_family="aaa",
    description=(
        "Configure an AAA login authentication method list. If 'group tacacs+' is "
        "used, tacacs_key_ref must reference a key already stored in the vault — "
        "never pass a real secret value here. Atomic — stages the change."
    ),
)

TOOLS = [SET_AAA_LOGIN]

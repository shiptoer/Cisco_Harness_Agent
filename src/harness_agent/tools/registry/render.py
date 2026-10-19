"""
nlcfg.registry.render
======================
Jinja2 Environment dùng chung: StrictUndefined (lỗi ngay nếu template tham
chiếu field không tồn tại, thay vì âm thầm render ra chuỗi rỗng), và
autoescape=False (đây là CLI text, không phải HTML — escape HTML sẽ phá
cú pháp Cisco IOS, vd biến "&" hay "<" trong description thành entity).

Đây là renderer DUY NHẤT của hệ thống, và thư mục template duy nhất là
`harness_agent/tools/templates/`. LLM không bao giờ gõ CLI.
"""

import ipaddress
from collections.abc import Iterable
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel

from .ip_utils import prefix_to_netmask
from .tools import TOOL_REGISTRY

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _cidr_to_wildcard(cidr: str) -> str:
    """Filter `wildcard`: "10.0.0.0/24" -> "0.0.0.255" (wildcard mask cho ACL IOS)."""
    return str(ipaddress.IPv4Network(cidr, strict=True).hostmask)


_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701 — render CLI Cisco, không phải HTML
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["netmask"] = prefix_to_netmask
_env.filters["wildcard"] = _cidr_to_wildcard


def render_args(template_name: str, args) -> str:
    """Render CLI cho MỘT staged op. `args` là instance Pydantic đã validate."""
    template = _env.get_template(template_name)
    return template.render(a=args).rstrip("\n")


def render_by_device(ops: Iterable[tuple[str, BaseModel]]) -> dict[str, str]:
    """Render các thao tác đã qua gate thành cấu hình theo từng thiết bị.

    `ops`: các cặp (tên tool, args đã validate bằng args_model của tool đó), theo
    đúng thứ tự stage — thứ tự có ý nghĩa (vd tạo ACL trước khi apply ACL).
    Trả về {hostname: CLI}. Thiết bị xếp theo lần xuất hiện đầu tiên, trong mỗi
    thiết bị giữ thứ tự stage, mỗi thiết bị kết thúc bằng "end". Tất định: cùng
    input luôn cho cùng output (golden test được).
    """
    blocks: dict[str, list[str]] = {}
    for tool, args in ops:
        spec = TOOL_REGISTRY[tool]
        if not isinstance(args, spec.args_model):
            # Không render thứ chưa qua args_model: dict thô hay model của tool khác.
            raise TypeError(f"args của {tool} phải là {spec.args_model.__name__}, nhận {type(args).__name__}")
        snippet = render_args(spec.template, args)
        if _ends_in_submode(snippet):
            # Đóng sub-mode (interface/vlan/acl/line) trước thao tác kế tiếp. IOS có tự thoát khi gặp lệnh
            # toàn cục, nhưng change package gửi người duyệt phải tường minh, và sensor syntax (iv) không
            # mô hình hoá hành vi tự thoát đó (D-019).
            snippet += "\nexit"
        blocks.setdefault(args.device, []).append(snippet)
    return {device: "\n".join(parts) + "\nend\n" for device, parts in blocks.items()}


_ENTERS_SUBMODE = ("interface ", "vlan ", "ip access-list ", "line ")


def _ends_in_submode(snippet: str) -> bool:
    """Template thụt một khoảng trắng cho lệnh trong sub-mode; lệnh toàn cục không thụt. `!` là ghi chú."""
    in_submode = False
    for line in snippet.split("\n"):
        if not line.strip() or line.lstrip().startswith("!"):
            continue
        if line.startswith((" ", "\t")):
            continue  # vẫn trong sub-mode hiện tại
        in_submode = line.startswith(_ENTERS_SUBMODE)
    return in_submode

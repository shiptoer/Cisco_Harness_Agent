"""nlcfg.registry.ip_utils — tiện ích IP/prefix dùng chung, không phụ thuộc mcp."""

import ipaddress


def prefix_to_netmask(prefix_len: int) -> str:
    if not (0 <= prefix_len <= 32):
        raise ValueError(f"prefix_len không hợp lệ: {prefix_len}")
    return str(ipaddress.IPv4Network((0, prefix_len)).netmask)


def validate_ipv4(value: str) -> str:
    ipaddress.IPv4Address(value)  # raises ValueError nếu sai
    return value

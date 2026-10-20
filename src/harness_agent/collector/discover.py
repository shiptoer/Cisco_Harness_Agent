"""Explicit, bounded TCP/SSH discovery. Does not authenticate or change inventory.

python -m harness_agent.collector.discover --cidr 192.168.137.0/24
An open port is not proof of a Cisco device or successful authentication.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
from concurrent.futures import ThreadPoolExecutor


def _probe(host: str) -> dict[str, object]:
    try:
        with socket.create_connection((host, 22), timeout=1) as conn:
            conn.settimeout(1)
            try:
                banner = conn.recv(255).split(b"\n", 1)[0].decode("ascii", errors="replace").strip()
            except OSError:
                banner = ""
        return {"host": host, "port": 22, "open": True, "ssh": banner.startswith("SSH-"), "vendor": "unknown"}
    except (OSError, TimeoutError):
        return {"host": host, "port": 22, "open": False, "vendor": "unknown"}


def discover(cidr: str) -> list[dict[str, object]]:
    network = ipaddress.IPv4Network(cidr, strict=True)
    if (
        network.num_addresses > 256
        or not any(
            network.subnet_of(ipaddress.IPv4Network(block))
            for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        or network.is_multicast
        or network.is_unspecified
    ):
        raise ValueError("use an explicit private IPv4 subnet of at most 256 addresses")
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_probe, map(str, network.hosts())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cidr", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(discover(args.cidr), indent=2))
    except ValueError:
        parser.error("use an explicit private IPv4 subnet of at most 256 addresses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

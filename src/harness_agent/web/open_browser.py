"""Deliver a one-time operator URL to the desktop without exposing it to MCP."""

import subprocess
import sys
import webbrowser
from urllib.parse import urlsplit


def open_operator_page(url):
    """Keep the capability out of child argv/environment and all captured streams.

    Browser/profile/process access by a compromised same-user runtime remains
    outside this channel-isolation boundary. Failure never falls back to MCP.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed helper, URL through stdin
            [sys.executable, "-m", "harness_agent.web.open_browser"],
            input=url.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main():
    url = sys.stdin.buffer.read(2049).decode("utf-8")
    parsed = urlsplit(url)
    if (
        len(url) > 2048
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.path != "/open"
        or parsed.username
        or parsed.password
    ):
        return 1
    return 0 if webbrowser.open(url, new=2) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Wrapper giữ lệnh cũ `python scripts/ui_server.py ...`; code ở `harness_agent.ui.__main__` (lệnh `cha-ui`
hoặc `python -m harness_agent.ui`). Xem docstring của module đó cho cách dùng."""

from __future__ import annotations

from harness_agent.ui.__main__ import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())

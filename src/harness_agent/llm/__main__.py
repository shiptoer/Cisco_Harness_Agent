"""Thử nhanh client: python -m harness_agent.llm "Xin chào" [--mode auto|record|replay|off] [--model ...]

Cần DEEPSEEK_API_KEY trong môi trường (trừ chế độ replay có sẵn cache). Không in key ra màn hình.
"""

from __future__ import annotations

import argparse
import sys

from .cache import MODES, CacheMiss
from .client import DEFAULT_CACHE_DIR, DeepSeekClient, LLMError


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt")
    ap.add_argument("--system", default=None)
    ap.add_argument("--model", default=None, help="mặc định: $DEEPSEEK_MODEL_DEV")
    ap.add_argument("--mode", default="auto", choices=MODES)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    a = ap.parse_args(argv)
    client = DeepSeekClient.from_env(model=a.model, mode=a.mode, cache_dir=a.cache_dir)
    try:
        r = client.chat(
            ([{"role": "system", "content": a.system}] if a.system else [])
            + [{"role": "user", "content": a.prompt}]
        )
    except (LLMError, CacheMiss) as exc:
        print(f"[LỖI] {exc}", file=sys.stderr)
        return 2
    print(r.text)
    print(f"\n[model={r.model} cached={r.cached} usage={r.usage} key={r.key[:12]}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Sinh corpus TestCase deterministic từ seed.

    python scripts/gen_corpus.py --seed 42 --out data/corpus \
        --clean 20 --corrupted 10 --adversarial 20 --ambiguous 10
    python scripts/gen_corpus.py --seed 42 --out data/corpus --paraphrase template   # biến thể (tất định)
    python scripts/gen_corpus.py --seed 42 --out data/corpus --paraphrase llm        # cần API key

Cùng tham số → cùng file (kiểm bằng sha256 trong manifest.json). Mỗi case kèm một oracle scenario cho
scripts/fake_dsh.py (chạy khô qua scripts/run_corpus.py).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from harness_agent.corpus import generate, paraphrase_case, write_corpus
from harness_agent.models import Subset


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--context", default="data/contexts/campus-small-01.json")
    ap.add_argument("--out", default="data/corpus")
    ap.add_argument("--clean", type=int, default=20)
    ap.add_argument("--corrupted", type=int, default=10)
    ap.add_argument("--adversarial", type=int, default=20)
    ap.add_argument("--ambiguous", type=int, default=10)
    ap.add_argument("--paraphrase", choices=["none", "template", "llm"], default="none")
    ap.add_argument("--llm-mode", choices=["auto", "record", "replay", "off"], default="auto")
    a = ap.parse_args()

    base = json.loads(Path(a.context).read_text(encoding="utf-8-sig"))
    counts = {
        Subset.CLEAN: a.clean,
        Subset.CORRUPTED: a.corrupted,
        Subset.ADVERSARIAL: a.adversarial,
        Subset.AMBIGUOUS: a.ambiguous,
    }
    cases = generate(a.seed, base, counts)
    if a.paraphrase != "none":
        rng = random.Random(a.seed ^ 0x5EED)  # noqa: S311 — không phải mục đích mật mã
        client = None
        if a.paraphrase == "llm":
            from harness_agent.llm import DeepSeekClient

            client = DeepSeekClient.from_env(mode=a.llm_mode)
        for g in cases:
            g.case = paraphrase_case(g.case, rng, a.paraphrase, client)
    manifest = write_corpus(cases, Path(a.out), a.seed, a.context)
    info = json.loads(manifest.read_text(encoding="utf-8"))
    print(f"đã ghi {len(cases)} case vào {a.out}: {info['counts']}")
    print(f"seed={a.seed}, generator_version={info['generator_version']}, manifest={manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

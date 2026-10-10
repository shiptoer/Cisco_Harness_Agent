"""TV2 — corpus generator (2.3) và paraphrase (2.4). Nhãn deterministic, không do LLM."""

from .generator import GENERATOR_VERSION, GeneratedCase, generate, write_corpus
from .paraphrase import paraphrase_case

__all__ = ["GENERATOR_VERSION", "GeneratedCase", "generate", "paraphrase_case", "write_corpus"]

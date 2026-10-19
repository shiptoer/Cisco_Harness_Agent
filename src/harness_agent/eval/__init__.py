"""TV2 — evaluator độc lập (task 2.5, D-006): chấm theo nhãn corpus, phân tích ngược CLI."""

from .cli_parse import ParsedConfig, parse_config
from .evaluate import CaseEval, aggregate_by_case, evaluate_case, load_result, markdown_table, summarise

__all__ = [
    "CaseEval",
    "aggregate_by_case",
    "ParsedConfig",
    "evaluate_case",
    "load_result",
    "markdown_table",
    "parse_config",
    "summarise",
]

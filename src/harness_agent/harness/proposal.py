"""Proposal checks shared by CBA submission and the trusted runner (D-049)."""

from harness_agent.models import ChangeRequest

DUPLICATE_REASON = "Duplicate operations in change request. Submit each operation only once."


def has_duplicate_operations(cr: ChangeRequest) -> bool:
    """Use validated ToolCall equality, exactly as TSA's idempotent staging does.

    This is not conflict detection: distinct arguments and operation order are preserved.
    """
    return any(call in cr.changes[:index] for index, call in enumerate(cr.changes))

"""The checks moved to the ``checks`` package; import from there.

This module keeps ``from officesentry.rules import ...`` working for code
written before the move: every name of ``checks`` and its area modules is
still found here.
"""

from .checks import (  # noqa: F401
    AREAS,
    RULES,
    SEVERITIES,
    SEVERITY_RANK,
    Finding,
    Rule,
    RuleResult,
    base,
    evaluate,
    rule,
)


def __getattr__(name: str):
    for module in (base, *AREAS):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

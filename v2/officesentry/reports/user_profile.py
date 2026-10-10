"""The person page moved to the ``profile`` package; import from there.

This module keeps ``from officesentry.reports import user_profile`` working
for code written before the move: every name of the package's modules is
still found here.
"""

from importlib import import_module

from . import profile


def __getattr__(name: str):
    for module in (import_module(f"{profile.__name__}.{m}") for m in profile.MODULES):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

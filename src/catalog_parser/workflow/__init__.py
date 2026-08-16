from __future__ import annotations

from typing import Any

__all__ = ["run_workflow"]


def __getattr__(name: str) -> Any:
    if name == "run_workflow":
        from catalog_parser.workflow.orchestrator import run_workflow

        return run_workflow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

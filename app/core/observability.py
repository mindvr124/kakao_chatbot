import os
from typing import Callable, Any

try:
    from langsmith import traceable as _traceable  # type: ignore
except Exception:
    _traceable = None


def is_tracing_enabled() -> bool:
    return (
        os.getenv("LANGCHAIN_TRACING_V2", "").lower() in ("1", "true", "yes")
        and bool(os.getenv("LANGSMITH_API_KEY"))
    )


def traceable(func: Callable[..., Any]) -> Callable[..., Any]:
    if _traceable is not None and is_tracing_enabled():
        return _traceable(func)
    # no-op decorator when tracing disabled or langsmith missing
    return func



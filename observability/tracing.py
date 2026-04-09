"""
observability/tracing.py — Langfuse Python SDK (v3)

Uso:
    from observability.tracing import setup_tracing, get_langfuse

    setup_tracing()   # llamar una vez al inicio (en startup de FastAPI)

    langfuse = get_langfuse()
    with langfuse.start_as_current_observation(name="my-span", as_type="span") as span:
        span.update(input={"key": "value"})
"""

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_BASE_URL = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "sre-agent")

_langfuse = None
_initialized = False


def setup_tracing() -> None:
    global _langfuse, _initialized
    if _initialized:
        return

    if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
        logger.warning("tracing.setup: LANGFUSE keys not set — tracing disabled")
        _initialized = True
        return

    from langfuse import Langfuse
    _langfuse = Langfuse(public_key=LANGFUSE_PUBLIC_KEY, secret_key=LANGFUSE_SECRET_KEY, host=LANGFUSE_BASE_URL, )
    _initialized = True
    logger.info(f"tracing.setup: done, host={LANGFUSE_BASE_URL}, service={SERVICE_NAME}")


def get_langfuse():
    if not _initialized:
        setup_tracing()
    return _langfuse


class _NoopSpan:
    def set_attribute(self, k, v): pass

    def set_status(self, s):       pass

    def record_exception(self, e): pass

    def __enter__(self):           return self

    def __exit__(self, *a):        pass


class _NoopTracer:
    def start_as_current_span(self, name):
        return _NoopSpan()


tracer = _NoopTracer()


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def get_current_trace_id() -> str:
    if _langfuse is None:
        return "no-trace"
    try:
        from langfuse import get_client
        client = get_client()
        obs = client.get_current_observation()
        if obs and hasattr(obs, "trace_id"):
            return obs.trace_id
    except Exception:
        pass
    return "no-trace"


def get_current_span_id() -> str:
    if _langfuse is None:
        return "no-span"
    try:
        from langfuse import get_client
        client = get_client()
        obs = client.get_current_observation()
        if obs and hasattr(obs, "id"):
            return obs.id
    except Exception:
        pass
    return "no-span"

"""
observability/logger.py — Structured logging + lightweight metrics.

Logging: structlog with JSON output (production) or colored console (dev).
Metrics: simple thread-safe in-memory counters exposed via /metrics endpoint.
         For production, swap with Prometheus client or OpenTelemetry.
"""

import logging
import os
import time
from collections import defaultdict
from threading import Lock
from typing import Any

import structlog


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json")   # "json" | "console"


# ---------------------------------------------------------------------------
# Configure structlog
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
    ]

    if LOG_FORMAT == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


_configure_logging()
_log = structlog.get_logger("sre_agent")


# ---------------------------------------------------------------------------
# Stage event logger
# ---------------------------------------------------------------------------

def log_stage(stage: str, status: str, run_id: str, **kwargs: Any) -> None:
    """
    Emit a structured log event for a pipeline stage.

    Parameters
    ----------
    stage   : e.g. "INGEST", "TRIAGE", "TICKET", "NOTIFY_TEAM", "RESOLVE"
    status  : e.g. "success", "error", "guardrail_rejected"
    run_id  : unique identifier for this pipeline run
    **kwargs: additional key-value context (elapsed, ticket_id, severity, etc.)
    """
    level = logging.WARNING if status == "error" else logging.INFO
    event_data = {
        "pipeline_stage": stage,
        "status": status,
        "run_id": run_id,
        **kwargs,
    }
    _log.log(level, f"stage.{stage.lower()}", **event_data)


# ---------------------------------------------------------------------------
# Simple in-memory metrics
# ---------------------------------------------------------------------------

class _Metrics:
    """Thread-safe counter store."""

    def __init__(self):
        self._counts: dict = defaultdict(int)
        self._lock   = Lock()
        self._start  = time.time()

    def inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[key] += amount

    def get_all(self) -> dict:
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self._start, 1),
                "counters": dict(self._counts),
            }

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


metrics = _Metrics()
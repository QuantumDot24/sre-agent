"""
pipeline.py — Orchestrates the 5 SRE pipeline stages.
Cada stage tiene su propia observación en Langfuse para trazabilidad completa.

Stage 1: INGEST   — validate & sanitize inputs
Stage 2: TRIAGE   — LLM triage + RAG context
Stage 3: TICKET   — create ticket in mock Linear
Stage 4: NOTIFY   — notify technical team (email + Slack)
Stage 5: RESOLVE  — listen for resolution, notify reporter
"""

import json
import logging
import re
import time
import traceback
from contextlib import contextmanager, nullcontext
from typing import Optional

from agent import indexer, inference
from agent.guardrails import (
    sanitize_text, sanitize_log, sanitize_image,
    build_safe_context, GuardrailError,
)
from agent.notifier import (
    notify_team_email, notify_team_slack, notify_reporter_resolved,
)
from observability.logger import log_stage, metrics
from observability.tracing import get_langfuse, setup_tracing
from ticketing.mock_linear import create_ticket, resolve_ticket

logger = logging.getLogger(__name__)


@contextmanager
def _span(name: str, **kwargs):
    """Crea un span de Langfuse si está disponible, si no es no-op."""
    lf = get_langfuse()
    if lf:
        with lf.start_as_current_observation(name=name, as_type="span", **kwargs) as s:
            yield s
    else:
        yield None


@contextmanager
def _generation(name: str, **kwargs):
    """Crea una generación de Langfuse si está disponible, si no es no-op."""
    lf = get_langfuse()
    if lf:
        with lf.start_as_current_observation(name=name, as_type="generation", **kwargs) as s:
            yield s
    else:
        yield None


def _upd(span, **kwargs):
    """Actualiza un span solo si no es None."""
    if span:
        try:
            span.update(**kwargs)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(
    reporter_email: str,
    title: str,
    description: str,
    log_bytes: Optional[bytes] = None,
    image_bytes: Optional[bytes] = None,
    image_media_type: Optional[str] = None,
) -> dict:

    setup_tracing()
    lf     = get_langfuse()
    run_id = f"run-{int(time.time() * 1000)}"
    result = {}

    logger.info(f"pipeline.start: run_id={run_id}, reporter={reporter_email}")

    root_ctx = lf.start_as_current_observation(
        name="sre.pipeline",
        as_type="span",
        input={
            "reporter_email": reporter_email,
            "title":          title,
            "has_log":        log_bytes is not None,
            "has_image":      image_bytes is not None,
        },
        metadata={"run_id": run_id},
    ) if lf else nullcontext()

    with root_ctx as root:

        # ── Stage 1: INGEST ──────────────────────────────────────────────────
        with _span("stage.ingest", input={"title": title[:120]}) as s1:
            t0 = time.perf_counter()
            try:
                clean_title       = sanitize_text(title)
                clean_description = sanitize_text(description)
                clean_log: Optional[str] = None

                if log_bytes:
                    clean_log = sanitize_log(log_bytes).decode("utf-8", errors="replace")

                if image_bytes and image_media_type:
                    sanitize_image(image_bytes, image_media_type)

                elapsed = time.perf_counter() - t0
                _upd(s1, output={
                    "title_chars":       len(clean_title),
                    "description_chars": len(clean_description),
                    "log_chars":         len(clean_log) if clean_log else 0,
                    "elapsed_ms":        round(elapsed * 1000),
                })
                log_stage("INGEST", "success", run_id, elapsed=elapsed)
                metrics.inc("stage.ingest.ok")

            except GuardrailError as e:
                _upd(s1, output={"error": str(e)}, level="ERROR")
                log_stage("INGEST", "guardrail_rejected", run_id, error=str(e))
                metrics.inc("stage.ingest.rejected")
                if lf: lf.flush()
                raise

            except Exception as e:
                _upd(s1, output={"error": str(e)}, level="ERROR")
                log_stage("INGEST", "error", run_id, error=str(e))
                metrics.inc("stage.ingest.error")
                if lf: lf.flush()
                raise

        # ── Stage 2: TRIAGE ──────────────────────────────────────────────────
        triage  = {}
        summary = ""

        with _span("stage.triage") as s2:
            t0 = time.perf_counter()
            try:
                rag_query = f"{clean_title} {clean_description}"
                if clean_log:
                    rag_query += f" {clean_log[:400]}"

                with _span("rag.query") as s_rag:
                    rag_context = indexer.query_codebase(rag_query, top_k=3)
                    _upd(s_rag, output={
                        "chunks": rag_context.count("---") if rag_context else 0,
                        "chars":  len(rag_context) if rag_context else 0,
                    })

                incident_context = build_safe_context(clean_title, clean_description, clean_log)
                if rag_context:
                    incident_context += (
                        "\n\n=== RELEVANT CODE/DOCS FROM CODEBASE (secondary context) ===\n"
                        + rag_context
                        + "\n=== END CODEBASE CONTEXT ==="
                    )

                with _generation("llm.triage", model=inference.OPENROUTER_MODEL,
                                 input={"chars": len(incident_context), "has_image": image_bytes is not None}) as s_lt:
                    raw_triage = inference.run_triage(
                        incident_context,
                        image_bytes=image_bytes,
                        image_media_type=image_media_type,
                    )
                    _upd(s_lt, output={"chars": len(raw_triage)})

                # JSON cleanup
                raw_triage = raw_triage.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
                raw_triage = re.sub(r'\s+', ' ', raw_triage)
                match = re.search(r'\{.*\}', raw_triage, re.DOTALL)
                if match:
                    raw_triage = match.group(0)
                if raw_triage.count('{') > raw_triage.count('}'):
                    raw_triage += '}'
                if '"keywords"' not in raw_triage:
                    raw_triage = raw_triage.rstrip('}') + ', "keywords": [], "needs_escalation": false}'

                try:
                    triage = json.loads(raw_triage)
                except json.JSONDecodeError:
                    sev  = re.search(r'"severity"\s*:\s*"(P[1-4])"', raw_triage)
                    comp = re.search(r'"component"\s*:\s*"([^"]+)"', raw_triage)
                    hyp  = re.search(r'"hypothesis"\s*:\s*"([^"]+)"', raw_triage)
                    kw   = re.search(r'"keywords"\s*:\s*(\[.*?\])', raw_triage)
                    esc  = re.search(r'"needs_escalation"\s*:\s*(true|false)', raw_triage)
                    triage = {
                        "severity":         sev.group(1)  if sev  else "P3",
                        "component":        comp.group(1) if comp else "unknown",
                        "hypothesis":       hyp.group(1)  if hyp  else "Unable to determine",
                        "keywords":         json.loads(kw.group(1)) if kw else [],
                        "needs_escalation": esc.group(1) == "true"  if esc else False,
                    }

                with _generation("llm.summary", model=inference.OPENROUTER_MODEL,
                                 input={"triage": triage}) as s_ls:
                    summary = inference.run_summary(
                        incident_context, json.dumps(triage),
                        image_bytes=image_bytes,
                        image_media_type=image_media_type,
                    )
                    _upd(s_ls, output={"chars": len(summary)})

                elapsed = time.perf_counter() - t0
                _upd(s2, output={
                    "severity":         triage.get("severity"),
                    "component":        triage.get("component"),
                    "hypothesis":       triage.get("hypothesis"),
                    "needs_escalation": triage.get("needs_escalation"),
                    "elapsed_ms":       round(elapsed * 1000),
                })
                result["triage"]  = triage
                result["summary"] = summary
                log_stage("TRIAGE", "success", run_id, severity=triage.get("severity"), elapsed=elapsed)
                metrics.inc("stage.triage.ok")
                metrics.inc(f"severity.{triage.get('severity', 'unknown')}")

            except Exception as e:
                _upd(s2, output={"error": str(e)}, level="ERROR")
                log_stage("TRIAGE", "error", run_id, error=str(e), tb=traceback.format_exc())
                metrics.inc("stage.triage.error")
                triage  = {"severity": "P3", "component": "unknown",
                           "hypothesis": "LLM triage failed", "needs_escalation": False}
                summary = f"Automated triage failed. Manual review required.\n\nOriginal report:\n{clean_description[:500]}"

        # ── Stage 3: TICKET ──────────────────────────────────────────────────
        with _span("stage.ticket") as s3:
            t0 = time.perf_counter()
            try:
                ticket = create_ticket(
                    title=clean_title, description=summary,
                    severity=triage.get("severity", "P3"),
                    component=triage.get("component", "unknown"),
                    reporter_email=reporter_email,
                    triage_meta=triage,
                )
                elapsed = time.perf_counter() - t0
                _upd(s3, output={
                    "ticket_id":  ticket["id"],
                    "severity":   ticket.get("severity"),
                    "elapsed_ms": round(elapsed * 1000),
                })
                result["ticket"] = ticket
                _upd(root, metadata={"ticket_id": ticket["id"]})
                log_stage("TICKET", "created", run_id, ticket_id=ticket["id"], elapsed=elapsed)
                metrics.inc("stage.ticket.created")

            except Exception as e:
                _upd(s3, output={"error": str(e)}, level="ERROR")
                log_stage("TICKET", "error", run_id, error=str(e))
                metrics.inc("stage.ticket.error")
                if lf: lf.flush()
                raise

        # ── Stage 4: NOTIFY TEAM ─────────────────────────────────────────────
        with _span("stage.notify_team") as s4:
            t0 = time.perf_counter()
            try:
                notify_team_email(ticket, triage)
                notify_team_slack(ticket, triage)
                elapsed = time.perf_counter() - t0
                _upd(s4, output={"channels": "email,slack", "elapsed_ms": round(elapsed * 1000)})
                log_stage("NOTIFY_TEAM", "success", run_id, ticket_id=ticket["id"], elapsed=elapsed)
                metrics.inc("stage.notify_team.ok")
            except Exception as e:
                _upd(s4, output={"error": str(e)}, level="ERROR")
                log_stage("NOTIFY_TEAM", "error", run_id, error=str(e))
                metrics.inc("stage.notify_team.error")  # Non-fatal

        _upd(root, output={
            "ticket_id": ticket["id"],
            "severity":  triage.get("severity"),
            "component": triage.get("component"),
        })
        logger.info(f"pipeline.complete: run_id={run_id}, ticket_id={ticket['id']}")

    if lf: lf.flush()

    result["trace_id"] = ticket["id"]
    return result


# ---------------------------------------------------------------------------
# Resolve pipeline
# ---------------------------------------------------------------------------

def resolve_pipeline(ticket_id: str) -> dict:
    setup_tracing()
    lf     = get_langfuse()
    run_id = f"resolve-{int(time.time() * 1000)}"

    root_ctx = lf.start_as_current_observation(
        name="sre.resolve",
        as_type="span",
        input={"ticket_id": ticket_id},
        metadata={"run_id": run_id},
    ) if lf else nullcontext()

    with root_ctx as root:

        with _span("stage.resolve") as s_res:
            t0 = time.perf_counter()
            try:
                ticket  = resolve_ticket(ticket_id)
                elapsed = time.perf_counter() - t0
                _upd(s_res, output={"ticket_id": ticket_id, "elapsed_ms": round(elapsed * 1000)})
                log_stage("RESOLVE", "ticket_updated", run_id, ticket_id=ticket_id, elapsed=elapsed)
                metrics.inc("stage.resolve.ok")
            except Exception as e:
                _upd(s_res, output={"error": str(e)}, level="ERROR")
                log_stage("RESOLVE", "error", run_id, error=str(e))
                metrics.inc("stage.resolve.error")
                if lf: lf.flush()
                raise

        with _span("stage.notify_reporter") as s_nr:
            t0 = time.perf_counter()
            try:
                notify_reporter_resolved(ticket)
                elapsed = time.perf_counter() - t0
                _upd(s_nr, output={
                    "to":         ticket.get("reporter_email", ""),
                    "elapsed_ms": round(elapsed * 1000),
                })
                log_stage("NOTIFY_REPORTER", "success", run_id, ticket_id=ticket_id, elapsed=elapsed)
                metrics.inc("stage.notify_reporter.ok")
            except Exception as e:
                _upd(s_nr, output={"error": str(e)}, level="ERROR")
                log_stage("NOTIFY_REPORTER", "error", run_id, error=str(e))
                metrics.inc("stage.notify_reporter.error")

        _upd(root, output={"ticket_id": ticket_id, "status": "resolved"})

    if lf: lf.flush()
    return ticket
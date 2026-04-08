"""
pipeline.py — Orchestrates the 5 SRE pipeline stages.

Stage 1: INGEST   — validate & sanitize inputs
Stage 2: TRIAGE   — LLM triage + RAG context
Stage 3: TICKET   — create ticket in mock Linear
Stage 4: NOTIFY   — notify technical team (email + Slack)
Stage 5: RESOLVE  — listen for resolution, notify reporter
"""

import json
import logging
import time
import traceback
from typing import Optional

from agent import indexer
from agent import inference
from agent.guardrails import (sanitize_text, sanitize_log, sanitize_image, build_safe_context, GuardrailError, )
from agent.notifier import (notify_team_email, notify_team_slack, notify_reporter_resolved, )
from observability.logger import log_stage, metrics
from ticketing.mock_linear import create_ticket, resolve_ticket

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(reporter_email: str, title: str, description: str,
                 log_bytes: Optional[bytes] = None,
                 image_bytes: Optional[bytes] = None,
                 image_media_type: Optional[str] = None) -> dict:
    run_id = f"run-{int(time.time() * 1000)}"
    logger.info(f"pipeline.start: run_id={run_id}, reporter={reporter_email}")
    logger.info(f"🖼️ Pipeline received: image_bytes={'present' if image_bytes else 'None'} ({len(image_bytes) if image_bytes else 0} bytes)")

    result = {}

    # ── Stage 1: INGEST ──────────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        clean_title = sanitize_text(title)
        clean_description = sanitize_text(description)
        clean_log: Optional[str] = None

        if log_bytes:
            clean_log_bytes = sanitize_log(log_bytes)
            clean_log = clean_log_bytes.decode("utf-8", errors="replace")

        if image_bytes and image_media_type:
            sanitize_image(image_bytes, image_media_type)  # validates only

        log_stage("INGEST", "success", run_id, elapsed=time.perf_counter() - t0)
        metrics.inc("stage.ingest.ok")
    except GuardrailError as e:
        log_stage("INGEST", "guardrail_rejected", run_id, error=str(e))
        metrics.inc("stage.ingest.rejected")
        raise
    except Exception as e:
        log_stage("INGEST", "error", run_id, error=str(e))
        metrics.inc("stage.ingest.error")
        raise

    # ── Stage 2: TRIAGE ──────────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        # Retrieve relevant code/doc context from the indexed e-commerce repo
        rag_context = indexer.query_codebase(f"{clean_title} {clean_description}", top_k=4)

        # Build safe prompt context
        incident_context = build_safe_context(clean_title, clean_description, clean_log)
        if rag_context:
            incident_context += (
                    "\n\n=== RELEVANT CODE/DOCS FROM CODEBASE ===\n" + rag_context + "\n=== END CODEBASE CONTEXT ===")

        raw_triage = inference.run_triage(
            incident_context,
            image_bytes=image_bytes,
            image_media_type=image_media_type
        )
        logger.error(f"RAW_TRIAGE_OUTPUT: {raw_triage}")

        # Limpieza agresiva
        import re
        raw_triage = raw_triage.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
        raw_triage = re.sub(r'\s+', ' ', raw_triage)
        match = re.search(r'\{.*\}', raw_triage, re.DOTALL)
        if match:
            raw_triage = match.group(0)
        if raw_triage.count('{') > raw_triage.count('}'):
            raw_triage += '}'
        if '"keywords"' not in raw_triage:
            raw_triage = raw_triage.rstrip('}') + ', "keywords": [], "needs_escalation": false}'

        # Intentar parsear JSON
        try:
            triage = json.loads(raw_triage)
        except json.JSONDecodeError:
            # Fallback con regex
            severity_match = re.search(r'"severity"\s*:\s*"(P[1-4])"', raw_triage)
            component_match = re.search(r'"component"\s*:\s*"([^"]+)"', raw_triage)
            hypothesis_match = re.search(r'"hypothesis"\s*:\s*"([^"]+)"', raw_triage)
            keywords_match = re.search(r'"keywords"\s*:\s*(\[.*?\])', raw_triage)
            escalation_match = re.search(r'"needs_escalation"\s*:\s*(true|false)', raw_triage)

            triage = {"severity": severity_match.group(1) if severity_match else "P3",
                "component": component_match.group(1) if component_match else "unknown",
                "hypothesis": hypothesis_match.group(1) if hypothesis_match else "Unable to determine",
                "keywords": json.loads(keywords_match.group(1)) if keywords_match else [],
                "needs_escalation": escalation_match.group(1) == "true" if escalation_match else False, }

        # LLM call 2: human-readable summary
        summary = inference.run_summary(
            incident_context,
            json.dumps(triage),
            image_bytes=image_bytes,
            image_media_type=image_media_type
        )
        result["triage"] = triage
        result["summary"] = summary
        log_stage("TRIAGE", "success", run_id, severity=triage.get("severity"), elapsed=time.perf_counter() - t0)
        metrics.inc(f"stage.triage.ok")
        metrics.inc(f"severity.{triage.get('severity', 'unknown')}")
    except Exception as e:
        log_stage("TRIAGE", "error", run_id, error=str(e), tb=traceback.format_exc())
        metrics.inc("stage.triage.error")
        # Degrade gracefully — continue with fallback triage
        triage = {"severity": "P3", "component": "unknown", "hypothesis": "LLM triage failed",
                  "needs_escalation": False}
        summary = f"Automated triage failed. Manual review required.\n\nOriginal report:\n{clean_description[:500]}"

    # ── Stage 3: TICKET ──────────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        ticket = create_ticket(title=clean_title, description=summary, severity=triage.get("severity", "P3"),
                               component=triage.get("component", "unknown"), reporter_email=reporter_email,
                               triage_meta=triage, )
        result["ticket"] = ticket
        log_stage("TICKET", "created", run_id, ticket_id=ticket["id"], elapsed=time.perf_counter() - t0)
        metrics.inc("stage.ticket.created")
    except Exception as e:
        log_stage("TICKET", "error", run_id, error=str(e))
        metrics.inc("stage.ticket.error")
        raise

    # ── Stage 4: NOTIFY TEAM ─────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        notify_team_email(ticket, triage)
        notify_team_slack(ticket, triage)
        log_stage("NOTIFY_TEAM", "success", run_id, ticket_id=ticket["id"], elapsed=time.perf_counter() - t0)
        metrics.inc("stage.notify_team.ok")
    except Exception as e:
        # Non-fatal — ticket is created, log and continue
        log_stage("NOTIFY_TEAM", "error", run_id, error=str(e))
        metrics.inc("stage.notify_team.error")

    logger.info(f"pipeline.complete: run_id={run_id}, ticket_id={ticket['id']}")
    return result


def resolve_pipeline(ticket_id: str) -> dict:
    """
    Mark a ticket as resolved and notify the original reporter.
    Called by the /resolve endpoint (or a webhook from the real ticketing system).
    """
    run_id = f"resolve-{int(time.time() * 1000)}"

    # ── Resolve ticket ───────────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        ticket = resolve_ticket(ticket_id)
        log_stage("RESOLVE", "ticket_updated", run_id, ticket_id=ticket_id, elapsed=time.perf_counter() - t0)
        metrics.inc("stage.resolve.ok")
    except Exception as e:
        log_stage("RESOLVE", "error", run_id, error=str(e))
        metrics.inc("stage.resolve.error")
        raise

    # ── Stage 5: NOTIFY REPORTER ─────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        notify_reporter_resolved(ticket)
        log_stage("NOTIFY_REPORTER", "success", run_id, ticket_id=ticket_id, elapsed=time.perf_counter() - t0)
        metrics.inc("stage.notify_reporter.ok")
    except Exception as e:
        log_stage("NOTIFY_REPORTER", "error", run_id, error=str(e))
        metrics.inc("stage.notify_reporter.error")

    return ticket

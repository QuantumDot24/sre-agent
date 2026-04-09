"""
mock_linear.py — In-memory Linear-style ticket store with JSON persistence.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TICKETS_FILE = os.getenv("TICKETS_FILE", "./data/tickets.json")

STATE_BACKLOG     = "backlog"
STATE_IN_PROGRESS = "in_progress"
STATE_DONE        = "done"

_PRIORITY_MAP = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}

_store: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load() -> None:
    global _store
    path = Path(TICKETS_FILE)
    if path.exists():
        try:
            with open(path) as f:
                _store = json.load(f)
            logger.info(f"mock_linear.loaded: count={len(_store)}")
        except Exception as e:
            logger.warning(f"mock_linear.load_error: {e}")
            _store = {}


def _save() -> None:
    path = Path(TICKETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(_store, f, indent=2)


_load()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_ticket(
    title: str,
    description: str,
    severity: str,
    component: str,
    reporter_email: str,
    triage_meta: Optional[dict] = None,
    runbook_steps: Optional[List[str]] = None,
) -> dict:
    ticket_id = f"INC-{str(uuid.uuid4())[:8].upper()}"
    ticket = {
        "id":               ticket_id,
        "title":            title,
        "description":      description,
        "state":            STATE_BACKLOG,
        "severity":         severity,
        "priority":         _PRIORITY_MAP.get(severity, 3),
        "component":        component,
        "reporter_email":   reporter_email,
        "triage_meta":      triage_meta or {},
        "runbook_steps":    runbook_steps or [],
        "created_at":       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at":       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resolved_at":      None,
        "resolution_notes": None,
    }
    _store[ticket_id] = ticket
    _save()
    logger.info(f"mock_linear.ticket_created: id={ticket_id}, severity={severity}, runbook_steps={len(runbook_steps or [])}")
    return ticket


def get_ticket(ticket_id: str) -> dict:
    if ticket_id not in _store:
        raise KeyError(f"Ticket {ticket_id} not found")
    return _store[ticket_id]


def list_tickets(
    state: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "newest",
    page: int = 1,
    page_size: int = 10,
) -> dict:
    tickets = list(_store.values())

    if state:
        tickets = [t for t in tickets if t["state"] == state]

    if q:
        q_lower = q.lower()
        tickets = [
            t for t in tickets
            if q_lower in t.get("title", "").lower()
            or q_lower in t.get("description", "").lower()
            or q_lower in t.get("component", "").lower()
            or q_lower in t.get("reporter_email", "").lower()
        ]

    reverse = sort != "oldest"
    tickets = sorted(tickets, key=lambda t: t.get("created_at", ""), reverse=reverse)

    total  = len(tickets)
    pages  = max(1, (total + page_size - 1) // page_size)
    page   = max(1, min(page, pages))
    start  = (page - 1) * page_size
    return {
        "tickets":   tickets[start: start + page_size],
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     pages,
    }


def update_ticket(ticket_id: str, **fields) -> dict:
    ticket = get_ticket(ticket_id)
    ticket.update(fields)
    ticket["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _store[ticket_id] = ticket
    _save()
    logger.info(f"mock_linear.ticket_updated: id={ticket_id}, fields={list(fields.keys())}")
    return ticket


def resolve_ticket(ticket_id: str, notes: str = "") -> dict:
    ticket = update_ticket(
        ticket_id,
        state=STATE_DONE,
        resolved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        resolution_notes=notes or "Issue resolved by engineering team.",
    )
    logger.info(f"mock_linear.ticket_resolved: id={ticket_id}")
    return ticket


def set_in_progress(ticket_id: str) -> dict:
    return update_ticket(ticket_id, state=STATE_IN_PROGRESS)
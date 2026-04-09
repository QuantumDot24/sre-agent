"""
notifier.py — Mocked email and Slack notifications.

Real email: set SMTP_HOST in .env
Real Slack: set SLACK_WEBHOOK_URL in .env (Incoming Webhook URL)
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NOTIFICATIONS_LOG    = os.getenv("NOTIFICATIONS_LOG", "./data/notifications.jsonl")
SLACK_WEBHOOK_URL    = os.getenv("SLACK_WEBHOOK_URL", "")
SMTP_HOST            = os.getenv("SMTP_HOST", "")

TEAM_EMAIL        = os.getenv("TEAM_EMAIL", "sre-team@example.com")
TEAM_SLACK        = os.getenv("TEAM_SLACK_CHANNEL", "#incidents")

_SEVERITY_EMOJI = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🟢"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _persist(record: dict) -> None:
    Path(NOTIFICATIONS_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(NOTIFICATIONS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def _send_email(to: str, subject: str, body: str) -> None:
    record = {
        "ts":           time.time(),
        "type":         "email",
        "to":           to,
        "subject":      subject,
        "body_preview": body[:200],
    }

    if SMTP_HOST:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = os.getenv("SMTP_FROM", "sre-agent@example.com")
        msg["To"]      = to
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, int(os.getenv("SMTP_PORT", "587"))) as s:
            s.sendmail(msg["From"], [to], msg.as_string())
        record["status"] = "sent"
        logger.info(f"notifier.email_sent: to={to}, subject={subject}")
    else:
        record["status"] = "mocked"
        logger.info(f"notifier.email_mock: to={to}, subject={subject}")

    _persist(record)


def _send_slack(channel: str, text: str, blocks: Optional[list] = None) -> None:
    record = {
        "ts":           time.time(),
        "type":         "slack",
        "channel":      channel,
        "text_preview": text[:200],
    }

    if SLACK_WEBHOOK_URL:
        import httpx
        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            resp = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
            resp.raise_for_status()
            record["status"] = "sent"
            logger.info(f"notifier.slack_sent: channel={channel}")
        except Exception as e:
            record["status"] = f"error: {e}"
            logger.error(f"notifier.slack_error: {e}")
    else:
        record["status"] = "mocked"
        logger.info(f"notifier.slack_mock: channel={channel}, text={text[:80]}")

    _persist(record)


# ---------------------------------------------------------------------------
# Public notification functions
# ---------------------------------------------------------------------------

def notify_team_email(ticket: dict, triage: dict) -> None:
    severity = triage.get("severity", "P3")
    emoji    = _SEVERITY_EMOJI.get(severity, "⚪")
    subject  = f"{emoji} [{severity}] Incident: {ticket['title']} [{ticket['id']}]"

    runbook_steps = ticket.get("runbook_steps", [])
    runbook_section = ""
    if runbook_steps:
        steps_text = "\n".join(f"  {i+1}. {step}" for i, step in enumerate(runbook_steps))
        runbook_section = f"""
RUNBOOK — IMMEDIATE ACTIONS
----------------------------
{steps_text}
"""

    body = f"""
SRE INCIDENT ALERT
==================
Ticket ID  : {ticket['id']}
Severity   : {severity}
Component  : {triage.get('component', 'unknown')}
Reporter   : {ticket['reporter_email']}
Created    : {ticket['created_at']}

HYPOTHESIS
----------
{triage.get('hypothesis', 'N/A')}

SUMMARY
-------
{ticket['description']}

KEYWORDS: {', '.join(triage.get('keywords', []))}
ESCALATION NEEDED: {triage.get('needs_escalation', False)}
{runbook_section}
View ticket: http://localhost:8000/tickets/{ticket['id']}
""".strip()

    _send_email(TEAM_EMAIL, subject, body)


def notify_team_slack(ticket: dict, triage: dict) -> None:
    severity = triage.get("severity", "P3")
    emoji    = _SEVERITY_EMOJI.get(severity, "⚪")
    text     = f"{emoji} *[{severity}] New Incident: {ticket['title']}*"

    runbook_steps = ticket.get("runbook_steps", [])

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} [{severity}] {ticket['title']}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Ticket:* `{ticket['id']}`"},
                {"type": "mrkdwn", "text": f"*Component:* `{triage.get('component','?')}`"},
                {"type": "mrkdwn", "text": f"*Reporter:* {ticket['reporter_email']}"},
                {"type": "mrkdwn", "text": f"*Escalate:* {'🚨 YES' if triage.get('needs_escalation') else 'No'}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Hypothesis:*\n{triage.get('hypothesis','N/A')}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Summary:*\n{ticket['description'][:300]}..."},
        },
    ]

    # Runbook block — only if steps exist
    if runbook_steps:
        steps_md = "\n".join(f"{i+1}. {step}" for i, step in enumerate(runbook_steps))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🗒 Runbook — Immediate Actions:*\n{steps_md}"},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"<http://localhost:8000/tickets/{ticket['id']}|View ticket {ticket['id']}>"}],
    })

    _send_slack(TEAM_SLACK, text, blocks)


def notify_reporter_resolved(ticket: dict) -> None:
    subject = f"✅ Resolved: {ticket['title']} [{ticket['id']}]"
    body = f"""
Hello,

We're pleased to inform you that the incident you reported has been resolved.

Incident : {ticket['title']}
Ticket ID: {ticket['id']}
Resolved : {ticket.get('resolved_at', 'N/A')}

Resolution notes:
{ticket.get('resolution_notes', 'The issue has been addressed by the engineering team.')}

Thank you for reporting this issue. If you continue to experience problems,
please submit a new report at http://localhost:8000.

— SRE Team
""".strip()

    _send_email(ticket["reporter_email"], subject, body)
    _send_slack(
        TEAM_SLACK,
        f"✅ Incident `{ticket['id']}` — *{ticket['title']}* has been resolved.",
    )
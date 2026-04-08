"""
api/main.py — FastAPI application.

Endpoints:
  POST /report          — submit incident (text + optional image/log)
  POST /resolve/{id}    — mark ticket resolved, notify reporter
  GET  /tickets         — list all tickets
  GET  /tickets/{id}    — get single ticket
  GET  /metrics         — observability counters
  GET  /notifications   — list sent notifications (from JSONL log)
  GET  /               — simple HTML UI for demo
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv
load_dotenv()
from observability.tracing import setup_tracing
setup_tracing()
from agent.guardrails import GuardrailError
from agent.pipeline import run_pipeline, resolve_pipeline
from observability.logger import metrics
from ticketing.mock_linear import list_tickets, get_ticket

logger = logging.getLogger(__name__)

app = FastAPI(title="SRE Incident Intake & Triage Agent", version="1.0.0",
    description="Automated incident triage powered by Qwen LLM + RAG over Medusa codebase", )

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], )

NOTIFICATIONS_LOG = os.getenv("NOTIFICATIONS_LOG", "./data/notifications.jsonl")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def ui():
    """Minimal HTML form for demo purposes."""
    return HTMLResponse(content=_HTML_UI, status_code=200)


@app.post("/report")
async def submit_report(reporter_email: str = Form(...), title: str = Form(...), description: str = Form(...),
        log_file: Optional[UploadFile] = File(None), screenshot: Optional[UploadFile] = File(None), ):
    """
    Ingest an incident report (multimodal: text + optional log file + screenshot).
    Runs the full 5-stage pipeline and returns the created ticket.
    """
    metrics.inc("api.report.received")

    log_bytes: Optional[bytes] = None
    image_bytes: Optional[bytes] = None
    image_media_type: Optional[str] = None

    if log_file:
        log_bytes = await log_file.read()

    if screenshot:
        image_bytes = await screenshot.read()
        image_media_type = screenshot.content_type or "image/jpeg"
        logger.info(f"📸 API received image: size={len(image_bytes)} bytes, type={image_media_type}")
    else:
        logger.info("📸 API: no image uploaded")

    try:
        result = run_pipeline(reporter_email=reporter_email, title=title, description=description, log_bytes=log_bytes,
            image_bytes=image_bytes, image_media_type=image_media_type, )
        metrics.inc("api.report.ok")
        return JSONResponse(content=result, status_code=201)

    except GuardrailError as e:
        metrics.inc("api.report.guardrail_rejected")
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        logger.exception("api.report.error")
        metrics.inc("api.report.error")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/resolve/{ticket_id}")
async def resolve(ticket_id: str, notes: str = ""):
    """Mark a ticket as resolved and notify the original reporter."""
    try:
        ticket = resolve_pipeline(ticket_id)
        metrics.inc("api.resolve.ok")
        return JSONResponse(content=ticket)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    except Exception as e:
        logger.exception("api.resolve.error")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tickets")
async def tickets_list(state: Optional[str] = None):
    return list_tickets(state=state)


@app.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: str):
    try:
        return get_ticket(ticket_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")


@app.get("/metrics")
async def get_metrics():
    return metrics.get_all()


@app.get("/notifications")
async def get_notifications(limit: int = 50):
    """Return last N entries from the notifications JSONL log."""
    path = Path(NOTIFICATIONS_LOG)
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return list(reversed(records))


# ---------------------------------------------------------------------------
# Startup: pre-build index in background
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """Attempt to build the codebase index in a background thread."""
    import threading
    from agent.indexer import build_index

    def _build():
        try:
            build_index()
        except Exception as e:
            logger.warning(f"startup.index_build_failed: {e}")

    t = threading.Thread(target=_build, daemon=True)
    t.start()
    logger.info("api.startup: SRE Agent started. Index building in background.")


# ---------------------------------------------------------------------------
# Minimal HTML UI
# ---------------------------------------------------------------------------

_HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SRE Incident Reporter</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 2rem; }
  h1 { color: #38bdf8; margin-bottom: 0.5rem; }
  .subtitle { color: #64748b; margin-bottom: 2rem; font-size: 0.9rem; }
  .card { background: #1e293b; border-radius: 12px; padding: 2rem; max-width: 700px; margin: 0 auto; }
  label { display: block; margin-top: 1.2rem; font-size: 0.85rem; color: #94a3b8; margin-bottom: 0.3rem; }
  input, textarea, select { width: 100%; background: #0f172a; border: 1px solid #334155; border-radius: 8px;
    padding: 0.6rem 0.8rem; color: #e2e8f0; font-size: 0.95rem; }
  textarea { min-height: 120px; resize: vertical; }
  input:focus, textarea:focus { outline: none; border-color: #38bdf8; }
  .file-label { border: 1px dashed #334155; border-radius: 8px; padding: 0.8rem; cursor: pointer;
    text-align: center; color: #64748b; font-size: 0.85rem; }
  .file-label:hover { border-color: #38bdf8; color: #38bdf8; }
  button { margin-top: 1.5rem; width: 100%; padding: 0.8rem; background: #0ea5e9; color: white;
    border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; font-weight: 600; }
  button:hover { background: #38bdf8; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  #result { margin-top: 1.5rem; padding: 1rem; background: #0f172a; border-radius: 8px;
    border: 1px solid #334155; font-size: 0.82rem; white-space: pre-wrap; display: none; }
  .severity-P1 { color: #f87171; }
  .severity-P2 { color: #fb923c; }
  .severity-P3 { color: #fbbf24; }
  .severity-P4 { color: #4ade80; }
  .nav { display: flex; gap: 1rem; margin-bottom: 1.5rem; max-width: 700px; margin-left: auto; margin-right: auto; }
  .nav a { color: #64748b; text-decoration: none; font-size: 0.85rem; }
  .nav a:hover { color: #38bdf8; }
</style>
</head>
<body>
<div class="nav">
  <strong style="color:#38bdf8">SRE Agent</strong>
  <a href="/tickets">📋 Tickets</a>
  <a href="/metrics">📊 Metrics</a>
  <a href="/notifications">🔔 Notifications</a>
  <a href="/docs">📖 API Docs</a>
</div>

<div class="card">
  <h1>🚨 Report an Incident</h1>
  <p class="subtitle">Powered by Qwen LLM + RAG over Medusa e-commerce codebase</p>

  <form id="reportForm">
    <label>Your Email *</label>
    <input type="email" name="reporter_email" required placeholder="you@company.com"/>

    <label>Incident Title *</label>
    <input type="text" name="title" required placeholder="e.g. Checkout failing for all users"/>

    <label>Description *</label>
    <textarea name="description" required
      placeholder="Describe what's broken, when it started, what you observed..."></textarea>

    <label>Log File (optional)</label>
    <label class="file-label" id="logLabel">
      <input type="file" name="log_file" style="display:none" id="logInput"
        accept=".log,.txt,.json,.jsonl"/>
      📄 Click to upload a log file (.log, .txt, .json)
    </label>

   <label>Screenshot (optional)</label>
<input type="file" name="screenshot" id="imgInput"
  accept="image/*"
  style="display:none"/>
<div class="file-label" id="imgLabel" 
  onclick="document.getElementById('imgInput').click()"
  style="cursor:pointer">
  🖼 Click to upload a screenshot (JPEG, PNG)
</div>

    <button type="submit" id="submitBtn">Submit Incident Report</button>
  </form>

  <div id="result"></div>
</div>

<script>
  document.getElementById('logInput').addEventListener('change', function() {
    document.getElementById('logLabel').textContent = '✅ ' + this.files[0]?.name;
  });
  document.getElementById('imgInput').addEventListener('change', function() {
    document.getElementById('imgLabel').textContent = '✅ ' + this.files[0]?.name;
  });

  document.getElementById('reportForm').addEventListener('submit', async (e) => {
    e.preventDefault();
  
 
    const btn = document.getElementById('submitBtn');
    const resultDiv = document.getElementById('result');
    btn.disabled = true;
    btn.textContent = '⏳ Triaging...';
    resultDiv.style.display = 'none';

    const fd = new FormData(e.target);
    // Eliminar file inputs vacíos para no mandar fantasmas
for (const key of ['log_file', 'screenshot']) {
  const file = fd.get(key);
  if (file instanceof File && file.size === 0 && file.name === '') {
    fd.delete(key);
  }
}
 // DEBUG: ver qué está mandando
  for (let [key, val] of fd.entries()) {
    console.log(key, val);
  }
    try {
      const resp = await fetch('/report', { method: 'POST', body: fd });
      const data = await resp.json();

      if (!resp.ok) {
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '❌ Error: ' + (data.detail || JSON.stringify(data));
        return;
      }

      const t = data.ticket;
      const sev = data.triage?.severity || 'P3';
      resultDiv.style.display = 'block';
      resultDiv.innerHTML = `
✅ Incident triaged and ticket created!

Ticket ID  : ${t?.id}
Severity   : <span class="severity-${sev}">${sev}</span>
Component  : ${data.triage?.component}
Hypothesis : ${data.triage?.hypothesis}

Summary:
${t?.description}

→ Team has been notified via email + Slack.
→ View ticket: <a href="/tickets/${t?.id}" style="color:#38bdf8">/tickets/${t?.id}</a>
      `.trim();
    } catch(err) {
      resultDiv.style.display = 'block';
      resultDiv.textContent = '❌ Network error: ' + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Submit Incident Report';
    }
  });
</script>
</body>
</html>
"""

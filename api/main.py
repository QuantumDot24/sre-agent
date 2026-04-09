"""
api/main.py — FastAPI application.

Endpoints:
  POST /report          — submit incident (text + optional image/log)
  POST /resolve/{id}    — mark ticket resolved, notify reporter
  POST /tickets/{id}/invalidate — mark ticket as invalid (attack/spam)
  GET  /tickets         — list tickets (filter, search, paginate)
  GET  /tickets/{id}    — get single ticket
  GET  /metrics         — observability counters
  GET  /notifications   — list sent notifications (from JSONL log)
  GET  /               — single-page HTML UI
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
from ticketing.mock_linear import list_tickets, get_ticket, invalidate_ticket

logger = logging.getLogger(__name__)

app = FastAPI(
    title="SRE Incident Intake & Triage Agent",
    version="0.9",
    description="Automated incident triage powered by Qwen LLM + RAG over Medusa codebase",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

NOTIFICATIONS_LOG = os.getenv("NOTIFICATIONS_LOG", "./data/notifications.jsonl")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(content=_HTML_UI, status_code=200)


@app.post("/report")
async def submit_report(
    reporter_email: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    log_file: Optional[UploadFile] = File(None),
    screenshot: Optional[UploadFile] = File(None),
):
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
        result = run_pipeline(
            reporter_email=reporter_email,
            title=title,
            description=description,
            log_bytes=log_bytes,
            image_bytes=image_bytes,
            image_media_type=image_media_type,
        )
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
async def resolve(ticket_id: str):
    """Mark a ticket as resolved — LLM generates resolution notes, reporter is notified."""
    try:
        ticket = resolve_pipeline(ticket_id)
        metrics.inc("api.resolve.ok")
        return JSONResponse(content=ticket)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    except Exception as e:
        logger.exception("api.resolve.error")
        metrics.inc("api.resolve.error")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tickets/{ticket_id}/invalidate")
async def invalidate(ticket_id: str, reason: str = "Marked as prompt injection attack"):
    """Mark a ticket as invalid (spam/attack)."""
    try:
        ticket = invalidate_ticket(ticket_id, reason)
        metrics.inc("api.invalidate.ok")
        return JSONResponse(content=ticket)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    except Exception as e:
        logger.exception("api.invalidate.error")
        metrics.inc("api.invalidate.error")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tickets")
async def tickets_list(
    state: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "newest",
    page: int = 1,
    page_size: int = 10,
):
    """
    List tickets with optional filters.
    - state: 'backlog' | 'in_progress' | 'done' | 'invalid'
    - q: keyword search (title, description, component, reporter)
    - sort: 'newest' (default) | 'oldest'
    - page / page_size: pagination
    """
    return list_tickets(state=state, q=q, sort=sort, page=page, page_size=page_size)


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
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
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
# HTML UI
# ---------------------------------------------------------------------------

_HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SRE AGENT</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet"/>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #07070e;
  --surface:   #0f0f1a;
  --card:      #14141f;
  --card-hover:#18182a;
  --border:    #1e1e30;
  --border-hi: #2d2d4a;

  --v900: #1e1145;
  --v700: #4c1d95;
  --v600: #7c3aed;
  --v500: #8b5cf6;
  --v400: #a78bfa;
  --v300: #c4b5fd;
  --v200: #ddd6fe;

  --text:      #ede9fe;
  --text-2:    #a8a3c1;
  --text-3:    #5e5a7a;

  --p1: #f87171;
  --p2: #fb923c;
  --p3: #fbbf24;
  --p4: #34d399;

  --success: #34d399;
  --danger:  #f87171;
  --warning: #fbbf24;

  --radius:  10px;
  --radius-lg: 16px;
  --font: 'Outfit', sans-serif;
  --mono: 'Fira Code', monospace;

  --sidebar-w: 220px;
  --glow: 0 0 40px rgba(139,92,246,.18);
}

html { height: 100%; }

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  height: 100%;
  display: flex;
  overflow: hidden;
}

body::before {
  content: '';
  position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");
  opacity: .5;
}

/* ── Sidebar ── */
#sidebar {
  width: var(--sidebar-w);
  min-width: var(--sidebar-w);
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100vh;
  position: relative;
  z-index: 10;
  flex-shrink: 0;
}
.logo { padding: 28px 20px 20px; border-bottom: 1px solid var(--border); }
.logo-mark { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.logo-icon {
  width: 32px; height: 32px;
  background: linear-gradient(135deg, var(--v600), var(--v400));
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
  box-shadow: 0 0 20px rgba(139,92,246,.4);
  flex-shrink: 0;
}
.logo-name {
  font-size: 1.05rem; font-weight: 700; letter-spacing: -.02em;
  background: linear-gradient(90deg, var(--v300), var(--v400));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.logo-sub { font-size: 0.7rem; color: #fafafa; font-family: var(--mono); letter-spacing: .06em; padding-left: 42px; }
nav { padding: 16px 10px; flex: 1; display: flex; flex-direction: column; gap: 2px; }
.nav-section { font-size: 0.62rem; font-family: var(--mono); letter-spacing: .12em; color: #fafafa; padding: 12px 10px 6px; text-transform: uppercase; }
.nav-item {
  display: flex; align-items: center; gap: 10px; padding: 9px 12px;
  border-radius: var(--radius); cursor: pointer; font-size: 0.875rem; font-weight: 500;
  color: var(--text-2); transition: all .15s ease; border: 1px solid transparent; user-select: none;
}
.nav-item:hover { background: var(--card); color: var(--text); border-color: var(--border); }
.nav-item.active { background: rgba(139,92,246,.12); color: var(--v300); border-color: rgba(139,92,246,.25); }
.nav-item .icon { width: 20px; text-align: center; font-size: 15px; flex-shrink: 0; }
.nav-badge { margin-left: auto; background: var(--v700); color: var(--v300); font-size: .65rem; font-family: var(--mono); padding: 2px 7px; border-radius: 99px; font-weight: 500; }
.sidebar-footer { padding: 16px 14px; border-top: 1px solid var(--border); }
.status-dot { display: flex; align-items: center; gap: 8px; font-size: .75rem; color: #fafafa; font-family: var(--mono); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); box-shadow: 0 0 6px var(--success); animation: pulse-dot 2s infinite; }
@keyframes pulse-dot { 0%,100% { opacity: 1; } 50% { opacity: .4; } }

/* ── Main ── */
#main { flex: 1; overflow-y: auto; height: 100vh; position: relative; z-index: 1; }
#main::-webkit-scrollbar { width: 6px; }
#main::-webkit-scrollbar-track { background: transparent; }
#main::-webkit-scrollbar-thumb { background: var(--border-hi); border-radius: 3px; }

.topbar {
  position: sticky; top: 0; z-index: 20;
  background: rgba(7,7,14,.85); backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border); padding: 0 32px; height: 58px;
  display: flex; align-items: center; justify-content: space-between;
}
.topbar-title { font-size: 1rem; font-weight: 600; color: var(--text); display: flex; align-items: center; gap: 10px; }
.topbar-title .icon { font-size: 18px; }
.topbar-actions { display: flex; align-items: center; gap: 10px; }
.btn-icon {
  width: 34px; height: 34px; border-radius: 8px; background: var(--card);
  border: 1px solid var(--border); color: var(--text-2); cursor: pointer;
  display: flex; align-items: center; justify-content: center; font-size: 15px; transition: all .15s;
}
.btn-icon:hover { border-color: var(--v500); color: var(--v400); background: rgba(139,92,246,.08); }

.content { padding: 32px; max-width: 900px; }
.view { display: none; animation: fadeUp .22s ease; }
.view.active { display: block; }
@keyframes fadeUp { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

/* ── Form ── */
.form-grid { display: grid; gap: 20px; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.field label { display: block; font-size: .78rem; font-weight: 600; letter-spacing: .04em; color: var(--text-2); margin-bottom: 7px; text-transform: uppercase; }
.field input, .field textarea {
  width: 100%; background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 11px 14px; color: var(--text); font-family: var(--font); font-size: .92rem;
  transition: border-color .15s, box-shadow .15s; resize: vertical;
}
.field input::placeholder, .field textarea::placeholder { color: var(--v300); }
.field input:focus, .field textarea:focus { outline: none; border-color: var(--v500); box-shadow: 0 0 0 3px rgba(139,92,246,.12); }
.field textarea { min-height: 110px; }

.upload-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.upload-zone {
  border: 1.5px dashed var(--border-hi); border-radius: var(--radius); padding: 20px 16px;
  text-align: center; cursor: pointer; transition: all .2s; background: var(--card); position: relative;
}
.upload-zone:hover { border-color: var(--v500); background: rgba(139,92,246,.05); }
.upload-zone input[type=file] { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
.upload-icon { font-size: 24px; margin-bottom: 6px; }
.upload-label { font-size: .78rem; color: var(--text-2); font-weight: 500; }
.upload-hint  { font-size: .68rem; color: #fafafa; margin-top: 3px; }
.upload-zone.filled { border-color: var(--v500); border-style: solid; }

.btn-primary {
  background: linear-gradient(135deg, var(--v600), var(--v500)); color: white; border: none;
  border-radius: var(--radius); padding: 13px 28px; font-family: var(--font); font-size: .95rem;
  font-weight: 600; cursor: pointer; width: 100%; transition: all .2s; position: relative; overflow: hidden; letter-spacing: .01em;
}
.btn-primary::before { content: ''; position: absolute; inset: 0; background: linear-gradient(135deg, var(--v500), var(--v400)); opacity: 0; transition: opacity .2s; }
.btn-primary:hover::before { opacity: 1; }
.btn-primary:disabled { opacity: .5; cursor: not-allowed; }
.btn-primary span { position: relative; z-index: 1; }

/* ── Result Modal ── */
#result-overlay {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: rgba(7,7,14,.65); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  align-items: center; justify-content: center; animation: overlayIn .2s ease;
}
#result-overlay.visible { display: flex; }
@keyframes overlayIn { from { opacity: 0; } to { opacity: 1; } }
.result-modal {
  position: relative; width: 100%; max-width: 560px; margin: 0 24px;
  border-radius: var(--radius-lg); border: 1px solid rgba(139,92,246,.35); overflow: hidden;
  box-shadow: 0 0 60px rgba(139,92,246,.22), 0 24px 64px rgba(0,0,0,.6);
  animation: modalIn .25s cubic-bezier(.16,1,.3,1);
}
@keyframes modalIn { from { opacity: 0; transform: scale(.94) translateY(12px); } to { opacity: 1; transform: scale(1) translateY(0); } }
.modal-close {
  position: absolute; top: 12px; right: 14px; width: 28px; height: 28px; border-radius: 7px;
  background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.1); color: var(--text-2);
  font-size: 14px; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: all .15s; z-index: 10; line-height: 1;
}
.modal-close:hover { background: rgba(248,113,113,.12); border-color: rgba(248,113,113,.3); color: var(--danger); }
.result-header { background: rgba(139,92,246,.1); border-bottom: 1px solid var(--border); padding: 14px 48px 14px 20px; display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: .9rem; }
.result-body { padding: 20px; background: var(--card); }
.result-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 16px; }
.result-field { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 14px; }
.result-field-label { font-size: .65rem; text-transform: uppercase; letter-spacing: .1em; color: #fafafa; font-family: var(--mono); margin-bottom: 4px; }
.result-field-value { font-size: .9rem; font-weight: 500; color: var(--text); }
.result-summary { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; font-size: .85rem; line-height: 1.6; color: var(--text-2); margin-bottom: 14px; }
.result-link { display: inline-flex; align-items: center; gap: 6px; color: var(--v400); font-size: .82rem; font-family: var(--mono); text-decoration: none; padding: 6px 12px; border: 1px solid rgba(167,139,250,.3); border-radius: 6px; transition: all .15s; }
.result-link:hover { background: rgba(167,139,250,.08); border-color: var(--v400); }

/* ── Badges ── */
.badge { display: inline-flex; align-items: center; gap: 5px; font-family: var(--mono); font-size: .72rem; font-weight: 500; padding: 3px 9px; border-radius: 6px; border: 1px solid; }
.badge-P1 { color: var(--p1); border-color: rgba(248,113,113,.3); background: rgba(248,113,113,.08); }
.badge-P2 { color: var(--p2); border-color: rgba(251,146,60,.3);  background: rgba(251,146,60,.08);  }
.badge-P3 { color: var(--p3); border-color: rgba(251,191,36,.3);  background: rgba(251,191,36,.08);  }
.badge-P4 { color: var(--p4); border-color: rgba(52,211,153,.3);  background: rgba(52,211,153,.08);  }
.badge-open     { color: var(--v400); border-color: rgba(167,139,250,.3); background: rgba(167,139,250,.08); }
.badge-resolved { color: var(--p4);  border-color: rgba(52,211,153,.3);  background: rgba(52,211,153,.08);  }
.badge-invalid  { color: var(--danger); border-color: rgba(248,113,113,.3); background: rgba(248,113,113,.08); }

.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.section-title { font-size: 1rem; font-weight: 700; color: var(--text); }
.section-count { font-family: var(--mono); font-size: .75rem; color: #fafafa; }

/* ── Ticket cards ── */
.ticket-list { display: flex; flex-direction: column; gap: 8px; }
.ticket-card {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
  padding: 16px 20px; cursor: pointer; transition: all .15s;
  display: grid; grid-template-columns: auto 1fr auto; gap: 14px; align-items: start;
}
.ticket-card:hover { border-color: var(--border-hi); background: var(--card-hover); transform: translateX(2px); }
.ticket-card.expanded { border-color: rgba(139,92,246,.3); background: var(--card-hover); }
.ticket-card.invalid { opacity: 0.7; border-color: rgba(248,113,113,.2); }
.ticket-id { font-family: var(--mono); font-size: .72rem; color: #fafafa; padding-top: 3px; white-space: nowrap; }
.ticket-main .ticket-title { font-weight: 600; font-size: .92rem; margin-bottom: 5px; color: var(--text); }
.ticket-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ticket-component { font-size: .72rem; color: #fafafa; font-family: var(--mono); background: var(--surface); padding: 2px 8px; border-radius: 4px; border: 1px solid var(--border); }
.ticket-reporter { font-size: .72rem; color: #fafafa; }
.ticket-right { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
.ticket-time { font-family: var(--mono); font-size: .65rem; color: #fafafa; }
.ticket-detail { display: none; grid-column: 1 / -1; border-top: 1px solid var(--border); padding-top: 14px; margin-top: 4px; }
.ticket-card.expanded .ticket-detail { display: block; }
.ticket-description { font-size: .83rem; color: var(--text-2); line-height: 1.65; margin-bottom: 12px; }
.ticket-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.btn-sm { padding: 6px 14px; border-radius: 7px; font-size: .78rem; font-weight: 600; cursor: pointer; border: 1px solid; transition: all .15s; font-family: var(--font); }
.btn-resolve { background: rgba(52,211,153,.08); border-color: rgba(52,211,153,.3); color: var(--p4); }
.btn-resolve:hover { background: rgba(52,211,153,.15); }
.btn-resolve:disabled { opacity: .5; cursor: not-allowed; }
.btn-copy { background: var(--surface); border-color: var(--border); color: var(--text-2); }
.btn-copy:hover { border-color: var(--border-hi); color: var(--text); }
.btn-attack { background: rgba(248,113,113,.08); border-color: rgba(248,113,113,.3); color: var(--danger); }
.btn-attack:hover { background: rgba(248,113,113,.2); border-color: var(--danger); }

.security-alert-banner {
  background: rgba(248,113,113,.1); border: 1px solid rgba(248,113,113,.3);
  border-radius: var(--radius); padding: 10px 14px; margin-bottom: 14px;
  font-size: .8rem; color: var(--danger);
  display: flex; align-items: center; gap: 8px;
}

/* ── Tabs ── */
.tabs { display: flex; gap: 0; margin-bottom: 18px; border-bottom: 1px solid var(--border); }
.tab-btn { padding: 8px 18px; border: none; background: transparent; color: var(--text-2); font-family: var(--font); font-size: .85rem; font-weight: 500; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; transition: color .15s, border-color .15s; }
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--v300); border-bottom-color: var(--v500); }
.tab-count { display: inline-flex; align-items: center; justify-content: center; background: var(--v900); color: var(--v300); font-family: var(--mono); font-size: .6rem; font-weight: 600; padding: 1px 6px; border-radius: 99px; margin-left: 5px; vertical-align: middle; }

/* ── Search bar ── */
.search-wrap { position: relative; margin-bottom: 14px; }
.search-wrap input { width: 100%; background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 9px 14px 9px 36px; color: var(--text); font-family: var(--font); font-size: .88rem; transition: border-color .15s, box-shadow .15s; }
.search-wrap input::placeholder { color: var(--text-3); }
.search-wrap input:focus { outline: none; border-color: var(--v500); box-shadow: 0 0 0 3px rgba(139,92,246,.12); }
.search-icon { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); font-size: 14px; pointer-events: none; opacity: .5; }

/* ── Pagination ── */
.pagination { display: flex; align-items: center; justify-content: center; gap: 6px; margin-top: 18px; }
.pg-btn { width: 32px; height: 32px; border-radius: 7px; border: 1px solid var(--border); background: var(--card); color: var(--text-2); font-size: .8rem; font-family: var(--mono); cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all .15s; }
.pg-btn:hover:not(:disabled) { border-color: var(--v500); color: var(--v300); background: rgba(139,92,246,.08); }
.pg-btn.active { background: rgba(139,92,246,.15); border-color: rgba(139,92,246,.4); color: var(--v300); font-weight: 600; }
.pg-btn:disabled { opacity: .35; cursor: not-allowed; }
.pg-info { font-family: var(--mono); font-size: .7rem; color: var(--text-3); padding: 0 4px; }

/* ── Metrics ── */
.metrics-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }
.metric-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 18px 20px; transition: all .15s; }
.metric-card:hover { border-color: var(--border-hi); box-shadow: var(--glow); }
.metric-key { font-family: var(--mono); font-size: .68rem; color: #fafafa; margin-bottom: 8px; letter-spacing: .04em; word-break: break-all; }
.metric-val { font-size: 1.8rem; font-weight: 700; color: var(--v300); letter-spacing: -.03em; line-height: 1; }
.metric-card.highlight { border-color: rgba(139,92,246,.3); }
.metric-card.highlight .metric-val { color: var(--v400); }

/* ── Notifications ── */
.notif-list { display: flex; flex-direction: column; gap: 8px; }
.notif-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px 20px; display: grid; grid-template-columns: auto 1fr auto; gap: 14px; align-items: start; transition: border-color .15s; }
.notif-card:hover { border-color: var(--border-hi); }
.notif-type-icon { width: 36px; height: 36px; border-radius: 9px; display: flex; align-items: center; justify-content: center; font-size: 16px; flex-shrink: 0; }
.notif-email  { background: rgba(139,92,246,.12); }
.notif-slack  { background: rgba(52,211,153,.1); }
.notif-resolve{ background: rgba(251,191,36,.1); }
.notif-subject { font-size: .88rem; font-weight: 600; margin-bottom: 4px; }
.notif-to { font-size: .75rem; color: #fafafa; font-family: var(--mono); }
.notif-body { font-size: .8rem; color: var(--text-2); margin-top: 6px; line-height: 1.55; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.notif-time { font-family: var(--mono); font-size: .65rem; color: #fafafa; white-space: nowrap; }

/* ── Empty / Loading ── */
.empty-state { text-align: center; padding: 60px 20px; color: #fafafa; }
.empty-icon { font-size: 36px; margin-bottom: 12px; }
.empty-text { font-size: .9rem; }
.skeleton { background: linear-gradient(90deg, var(--card) 25%, var(--card-hover) 50%, var(--card) 75%); background-size: 200% 100%; animation: shimmer 1.4s infinite; border-radius: var(--radius); }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
.skel-card { height: 72px; border-radius: var(--radius-lg); margin-bottom: 8px; }

.divider { height: 1px; background: linear-gradient(90deg, var(--v600) 0%, transparent 60%); margin-bottom: 24px; opacity: .4; }
.page-header { margin-bottom: 28px; }
.page-title { font-size: 1.45rem; font-weight: 700; letter-spacing: -.03em; margin-bottom: 4px; }
.page-subtitle { font-size: .85rem; color: #fafafa; }

@media (max-width: 640px) {
  :root { --sidebar-w: 60px; }
  .logo-name, .logo-sub, .nav-item span, .nav-section, .status-dot span { display: none; }
  .nav-item { justify-content: center; padding: 10px; }
  .nav-item .icon { width: auto; }
  .content { padding: 16px; }
  .topbar { padding: 0 16px; }
  .form-row, .upload-row, .result-grid { grid-template-columns: 1fr; }
  .result-modal { margin: 0 12px; }
}
</style>
</head>
<body>

<div id="result-overlay" onclick="handleOverlayClick(event)">
  <div class="result-modal" id="result-modal">
    <button class="modal-close" onclick="closeResultModal()" title="Close">✕</button>
    <div class="result-header" id="result-header"></div>
    <div class="result-body"   id="result-body"></div>
  </div>
</div>

<aside id="sidebar">
  <div class="logo">
    <div class="logo-mark">
      <div class="logo-icon">⚡</div>
      <span class="logo-name">SRE AGENT</span>
    </div>
    <div class="logo-sub">QWEN 3.5 0.8B + RAG</div>
  </div>
  <nav>
    <div class="nav-section">Workspace</div>
    <div class="nav-item active" data-view="report">
      <span class="icon">🚨</span><span>Report Incident</span>
    </div>
    <div class="nav-item" data-view="tickets">
      <span class="icon">📋</span><span>Tickets</span>
      <span class="nav-badge" id="nb-tickets">–</span>
    </div>
    <div class="nav-section">Observability</div>
    <div class="nav-item" data-view="metrics">
      <span class="icon">📊</span><span>Metrics</span>
    </div>
    <div class="nav-item" data-view="notifications">
      <span class="icon">🔔</span><span>Notifications</span>
      <span class="nav-badge" id="nb-notif">–</span>
    </div>
    <div class="nav-section">Docs</div>
    <div class="nav-item" onclick="window.open('/docs','_blank')">
      <span class="icon">📖</span><span>API Docs</span>
    </div>
  </nav>
  <div class="sidebar-footer">
    <div class="status-dot"><div class="dot"></div><span>System online v0.9</span></div>
  </div>
</aside>

<main id="main">
  <div class="topbar">
    <div class="topbar-title">
      <span class="icon" id="tb-icon">🚨</span>
      <span id="tb-title">Report Incident</span>
    </div>
    <div class="topbar-actions">
      <button class="btn-icon" title="Refresh" onclick="refreshView()">↺</button>
    </div>
  </div>

  <div class="content">

    <!-- ══ REPORT VIEW ══ -->
    <div class="view active" id="view-report">
      <div class="page-header">
        <div class="page-title">New Incident Report</div>
        <div class="page-subtitle">Automated triage via Qwen LLM · RAG over Medusa codebase</div>
      </div>
      <div class="divider"></div>
      <div class="form-grid">
        <div class="form-row">
          <div class="field">
            <label>Reporter Email</label>
            <input type="email" id="f-email" placeholder="you@company.com"/>
          </div>
          <div class="field">
            <label>Incident Title</label>
            <input type="text" id="f-title" placeholder="e.g. Checkout failing for all users"/>
          </div>
        </div>
        <div class="field">
          <label>Description</label>
          <textarea id="f-desc" placeholder="Describe what's broken, when it started, what you observed…"></textarea>
        </div>
        <div class="upload-row">
          <div class="upload-zone" id="zone-log">
            <input type="file" id="f-log" accept=".log,.txt,.json,.jsonl"/>
            <div class="upload-icon">📄</div>
            <div class="upload-label">Drop a log file</div>
            <div class="upload-hint">.log · .txt · .json · .jsonl</div>
          </div>
          <div class="upload-zone" id="zone-img">
            <input type="file" id="f-img" accept="image/*"/>
            <div class="upload-icon">🖼</div>
            <div class="upload-label">Drop a screenshot</div>
            <div class="upload-hint">JPEG · PNG · WebP</div>
          </div>
        </div>
        <button class="btn-primary" id="submit-btn" onclick="submitReport()">
          <span id="submit-label">Submit Incident Report</span>
        </button>
      </div>
    </div>

    <!-- ══ TICKETS VIEW ══ -->
    <div class="view" id="view-tickets">
      <div class="page-header">
        <div class="page-title">Incident Tickets</div>
        <div class="page-subtitle">Triage-generated tickets · click to expand</div>
      </div>
      <div class="divider"></div>

      <div class="tabs">
        <button class="tab-btn active" data-tab="all"     onclick="switchTab('all')">All <span class="tab-count" id="tab-count-all">–</span></button>
        <button class="tab-btn"        data-tab="backlog" onclick="switchTab('backlog')">Pending <span class="tab-count" id="tab-count-backlog">–</span></button>
        <button class="tab-btn"        data-tab="done"    onclick="switchTab('done')">Resolved <span class="tab-count" id="tab-count-done">–</span></button>
      </div>

      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="ticket-search" placeholder="Search by title, description, component…" oninput="handleSearch(this.value)"/>
      </div>

      <div class="section-head">
        <span class="section-title" id="tickets-heading">Tickets</span>
        <span class="section-count" id="tickets-count"></span>
      </div>
      <div id="ticket-list-container">
        <div class="skel-card skeleton"></div>
        <div class="skel-card skeleton"></div>
        <div class="skel-card skeleton"></div>
      </div>
      <div class="pagination" id="pagination"></div>
    </div>

    <!-- ══ METRICS VIEW ══ -->
    <div class="view" id="view-metrics">
      <div class="page-header">
        <div class="page-title">Observability Metrics</div>
        <div class="page-subtitle">Live counters · refreshes on every visit</div>
      </div>
      <div class="divider"></div>
      <div id="metrics-container">
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;">
          <div class="skel-card skeleton"></div>
          <div class="skel-card skeleton"></div>
          <div class="skel-card skeleton"></div>
          <div class="skel-card skeleton"></div>
          <div class="skel-card skeleton"></div>
          <div class="skel-card skeleton"></div>
        </div>
      </div>
    </div>

    <!-- ══ NOTIFICATIONS VIEW ══ -->
    <div class="view" id="view-notifications">
      <div class="page-header">
        <div class="page-title">Notification Log</div>
        <div class="page-subtitle">Email &amp; Slack dispatches · newest first</div>
      </div>
      <div class="divider"></div>
      <div id="notif-container">
        <div class="skel-card skeleton"></div>
        <div class="skel-card skeleton"></div>
        <div class="skel-card skeleton"></div>
      </div>
    </div>

  </div>
</main>

<script>
// ── Navigation ──────────────────────────────────────────
const TITLES = {
  report:        ['🚨', 'Report Incident'],
  tickets:       ['📋', 'Tickets'],
  metrics:       ['📊', 'Metrics'],
  notifications: ['🔔', 'Notifications'],
};

let currentView = 'report';

document.querySelectorAll('.nav-item[data-view]').forEach(el => {
  el.addEventListener('click', () => navigate(el.dataset.view));
});

function setBadge(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  if (val === null) { el.style.display = 'none'; }
  else { el.style.display = ''; el.textContent = val; }
}

function navigate(view) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelector(`[data-view="${view}"]`)?.classList.add('active');
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(`view-${view}`).classList.add('active');
  const [icon, title] = TITLES[view];
  document.getElementById('tb-icon').textContent = icon;
  document.getElementById('tb-title').textContent = title;
  currentView = view;
  if (view === 'notifications') { setBadge('nb-notif', null); loadNotifications(); }
  if (view === 'tickets')       { setBadge('nb-tickets', null); loadTickets(); }
  if (view === 'metrics')       loadMetrics();
}

function refreshView() { navigate(currentView); }

// ── Upload zones ────────────────────────────────────────
document.getElementById('f-log').addEventListener('change', function() {
  const z = document.getElementById('zone-log');
  z.classList.add('filled');
  z.querySelector('.upload-label').textContent = this.files[0]?.name || 'Log file selected';
});
document.getElementById('f-img').addEventListener('change', function() {
  const z = document.getElementById('zone-img');
  z.classList.add('filled');
  z.querySelector('.upload-label').textContent = this.files[0]?.name || 'Image selected';
});

// ── Result Modal ────────────────────────────────────────
function openResultModal() {
  const overlay = document.getElementById('result-overlay');
  overlay.classList.add('visible');
  const modal = document.getElementById('result-modal');
  modal.style.animation = 'none';
  modal.offsetHeight;
  modal.style.animation = '';
  document.addEventListener('keydown', handleEsc);
}
function closeResultModal() {
  document.getElementById('result-overlay').classList.remove('visible');
  document.removeEventListener('keydown', handleEsc);
  clearForm();
}
function handleOverlayClick(e) {
  if (e.target === document.getElementById('result-overlay')) closeResultModal();
}
function handleEsc(e) { if (e.key === 'Escape') closeResultModal(); }
function clearForm() {
  ['f-email','f-title','f-desc','f-log','f-img'].forEach(id => {
    document.getElementById(id).value = '';
  });
  ['zone-log','zone-img'].forEach(zid => {
    const z = document.getElementById(zid);
    z.classList.remove('filled');
    z.querySelector('.upload-label').textContent = zid === 'zone-log' ? 'Drop a log file' : 'Drop a screenshot';
  });
}

// ── Submit Report ───────────────────────────────────────
async function submitReport() {
  const email = document.getElementById('f-email').value.trim();
  const title = document.getElementById('f-title').value.trim();
  const desc  = document.getElementById('f-desc').value.trim();
  if (!email || !title || !desc) { alert('Please fill in email, title, and description.'); return; }

  const btn = document.getElementById('submit-btn');
  const lbl = document.getElementById('submit-label');
  btn.disabled = true;
  lbl.textContent = '⏳ Triaging incident…';

  const fd = new FormData();
  fd.append('reporter_email', email);
  fd.append('title', title);
  fd.append('description', desc);
  const logFile = document.getElementById('f-log').files[0];
  const imgFile = document.getElementById('f-img').files[0];
  if (logFile) fd.append('log_file', logFile);
  if (imgFile) fd.append('screenshot', imgFile);

  const header = document.getElementById('result-header');
  const body   = document.getElementById('result-body');

  try {
    const resp = await fetch('/report', { method: 'POST', body: fd });
    const data = await resp.json();

    if (!resp.ok) {
      header.innerHTML = '<span>❌</span> Submission Failed';
      body.innerHTML = `<div style="color:var(--danger);font-size:.85rem;padding:4px 0">${data.detail || JSON.stringify(data)}</div>`;
      openResultModal();
      return;
    }

    const t   = data.ticket || {};
    const tr  = data.triage  || {};
    const sev = tr.severity || 'P3';

    header.innerHTML = `<span>✅</span> Incident triaged · Ticket <span style="font-family:var(--mono);color:var(--v400)">${t.id || '—'}</span>`;
    body.innerHTML = `
      <div class="result-grid">
        <div class="result-field">
          <div class="result-field-label">Severity</div>
          <div class="result-field-value"><span class="badge badge-${sev}">${sev}</span></div>
        </div>
        <div class="result-field">
          <div class="result-field-label">Component</div>
          <div class="result-field-value" style="font-family:var(--mono);font-size:.82rem">${tr.component || '—'}</div>
        </div>
        <div class="result-field" style="grid-column:1/-1">
          <div class="result-field-label">Hypothesis</div>
          <div class="result-field-value" style="font-weight:400;font-size:.85rem">${tr.hypothesis || '—'}</div>
        </div>
      </div>
      <div class="result-summary">${t.description || data.summary || '—'}</div>
      <div style="display:flex;gap:10px;align-items:center">
        <a class="result-link" href="/tickets/${t.id}">🔗 /tickets/${t.id}</a>
        <span style="font-size:.75rem;color:#fafafa">Team notified via email + Slack</span>
      </div>`;

    openResultModal();
    fetchTicketCount();

  } catch(err) {
    header.innerHTML = '<span>❌</span> Network Error';
    body.innerHTML = `<div style="color:var(--danger);font-size:.85rem;padding:4px 0">${err.message}</div>`;
    openResultModal();
  } finally {
    btn.disabled = false;
    lbl.textContent = 'Submit Incident Report';
  }
}

// ── Tickets ─────────────────────────────────────────────
let currentTab    = 'all';
let currentSearch = '';
let currentPage   = 1;
const PAGE_SIZE   = 8;
let searchTimer   = null;

function switchTab(tab) {
  currentTab  = tab;
  currentPage = 1;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  loadTickets();
}

function handleSearch(val) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    currentSearch = val.trim();
    currentPage   = 1;
    loadTickets();
  }, 320);
}

async function loadTickets() {
  const container = document.getElementById('ticket-list-container');
  container.innerHTML = '<div class="skel-card skeleton"></div>'.repeat(3);
  document.getElementById('pagination').innerHTML = '';

  const params = new URLSearchParams({ sort: 'newest', page: currentPage, page_size: PAGE_SIZE });
  if (currentTab !== 'all') params.set('state', currentTab);
  if (currentSearch)        params.set('q', currentSearch);

  try {
    const resp = await fetch('/tickets?' + params);
    const data = await resp.json();
    renderTickets(data);
    renderPagination(data.page, data.pages, data.total);
    updateTabCounts();
    if (currentView !== 'tickets') setBadge('nb-tickets', data.total);
  } catch(e) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-text">Failed to load tickets</div></div>`;
  }
}

async function updateTabCounts() {
  const states = ['all', 'backlog', 'done'];
  const fetches = states.map(s => {
    const p = new URLSearchParams({ page: 1, page_size: 1 });
    if (s !== 'all') p.set('state', s);
    if (currentSearch) p.set('q', currentSearch);
    return fetch('/tickets?' + p).then(r => r.json()).then(d => [s, d.total]);
  });
  const results = await Promise.all(fetches);
  results.forEach(([s, count]) => {
    const el = document.getElementById(`tab-count-${s}`);
    if (el) el.textContent = count;
  });
}

function renderTickets(data) {
  const container = document.getElementById('ticket-list-container');
  const tickets   = data.tickets || [];

  document.getElementById('tickets-count').textContent = `${data.total} ticket${data.total !== 1 ? 's' : ''}`;
  const tabLabel = currentTab === 'backlog' ? 'Pending' : currentTab === 'done' ? 'Resolved' : 'All';
  document.getElementById('tickets-heading').textContent = `${tabLabel} Tickets`;

  if (!tickets.length) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-text">No tickets found</div></div>`;
    return;
  }

  container.innerHTML = tickets.map(t => {
    const sev  = t.severity || 'P3';
    const ts   = t.created_at
      ? new Date(t.created_at).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
      : '—';
    const desc = (t.description || '').substring(0, 220) + ((t.description || '').length > 220 ? '…' : '');
    const stateLabel = t.state === 'done' ? 'resolved' : t.state === 'in_progress' ? 'in progress' : t.state === 'invalid' ? 'invalid' : 'open';
    const stateBadge = t.state === 'done' ? 'badge-resolved' : t.state === 'invalid' ? 'badge-invalid' : 'badge-open';
    const isInvalid = t.state === 'invalid';
    const hasSecurityAlerts = t.security_alerts && t.security_alerts.length > 0;

    // Build security alert banner if present
    let securityBanner = '';
    if (hasSecurityAlerts && !isInvalid) {
      const alertsList = t.security_alerts.map(a => `• ${a}`).join('<br>');
      securityBanner = `
        <div class="security-alert-banner">
          <span>⚠️</span>
          <span><strong>Possible Prompt Injection</strong><br>${alertsList}</span>
        </div>
      `;
    }

    return `
    <div class="ticket-card ${isInvalid ? 'invalid' : ''}" id="tc-${t.id}" onclick="toggleTicket('${t.id}')">
      <div class="ticket-id">${t.id || '—'}</div>
      <div class="ticket-main">
        <div class="ticket-title">${t.title || 'Untitled'}</div>
        <div class="ticket-meta">
          <span class="badge badge-${sev}">${sev}</span>
          <span class="badge ${stateBadge}">${stateLabel}</span>
          ${t.component ? `<span class="ticket-component">${t.component}</span>` : ''}
          ${t.reporter_email ? `<span class="ticket-reporter">by ${t.reporter_email}</span>` : ''}
        </div>
        <div class="ticket-detail">
          ${securityBanner}
          <div class="ticket-description">${desc || 'No description.'}</div>
          ${t.runbook_steps && t.runbook_steps.length ? `
          <div style="background:rgba(167,139,250,.06);border:1px solid rgba(167,139,250,.2);border-radius:8px;padding:10px 14px;margin-bottom:10px;">
            <div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;color:var(--v300);">🗒 Runbook — Immediate Actions</div>
            <ol style="margin:0;padding-left:18px;display:flex;flex-direction:column;gap:5px;">
              ${t.runbook_steps.map(s => '<li style="font-size:.81rem;color:var(--text-2);line-height:1.5">' + s + '</li>').join('')}
            </ol>
          </div>` : ''}
          ${t.state === 'done' && t.resolution_notes ? `
          <div style="background:rgba(52,211,153,.06);border:1px solid rgba(52,211,153,.2);border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:.82rem;color:var(--p4)">
            <div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;opacity:.7">Resolution</div>
            ${t.resolution_notes}
          </div>` : ''}
          ${t.state === 'invalid' && t.invalid_reason ? `
          <div style="background:rgba(248,113,113,.06);border:1px solid rgba(248,113,113,.2);border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:.82rem;color:var(--danger)">
            <div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;opacity:.7">Invalidation Reason</div>
            ${t.invalid_reason}
          </div>` : ''}
          <div class="ticket-actions">
            ${t.state !== 'done' && t.state !== 'invalid' ? `<button class="btn-sm btn-resolve" onclick="event.stopPropagation();resolveTicket('${t.id}')">✓ Mark Resolved</button>` : ''}
            ${hasSecurityAlerts && t.state !== 'invalid' ? `<button class="btn-sm btn-attack" onclick="event.stopPropagation();markAsAttack('${t.id}')">🚨 Mark as Attack</button>` : ''}
            <button class="btn-sm btn-copy" onclick="event.stopPropagation();copyId('${t.id}')">⎘ Copy ID</button>
          </div>
        </div>
      </div>
      <div class="ticket-right"><span class="ticket-time">${ts}</span></div>
    </div>`;
  }).join('');
}

function renderPagination(page, pages, total) {
  const container = document.getElementById('pagination');
  if (pages <= 1) { container.innerHTML = ''; return; }

  let html = `<button class="pg-btn" onclick="goPage(${page-1})" ${page===1?'disabled':''}>‹</button>`;
  for (let i = 1; i <= pages; i++) {
    if (pages > 7 && Math.abs(i - page) > 2 && i !== 1 && i !== pages) {
      if (i === 2 || i === pages - 1) html += `<span class="pg-info">…</span>`;
      continue;
    }
    html += `<button class="pg-btn${i===page?' active':''}" onclick="goPage(${i})">${i}</button>`;
  }
  html += `<button class="pg-btn" onclick="goPage(${page+1})" ${page===pages?'disabled':''}>›</button>`;
  html += `<span class="pg-info">${total} total</span>`;
  container.innerHTML = html;
}

function goPage(p) { currentPage = p; loadTickets(); }
function toggleTicket(id) { document.getElementById(`tc-${id}`)?.classList.toggle('expanded'); }

async function resolveTicket(id) {
  if (!confirm(`Mark ticket ${id} as resolved?\\n\\nThe LLM will generate resolution notes and the reporter will be notified by email.`)) return;
  const btn = document.querySelector(`#tc-${id} .btn-resolve`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Resolving…'; }
  try {
    const resp = await fetch(`/resolve/${id}`, { method: 'POST' });
    if (!resp.ok) throw new Error(await resp.text());
    loadTickets();
  } catch(e) {
    alert(`Failed to resolve: ${e.message}`);
    if (btn) { btn.disabled = false; btn.textContent = '✓ Mark Resolved'; }
  }
}

async function markAsAttack(id) {
  if (!confirm(`Mark ticket ${id} as a prompt injection attack?\\n\\nThis will invalidate the ticket and remove it from the active queue.`)) return;
  const btn = document.querySelector(`#tc-${id} .btn-attack`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Invalidating…'; }
  try {
    const resp = await fetch(`/tickets/${id}/invalidate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'Marked as prompt injection attack' })
    });
    if (!resp.ok) throw new Error(await resp.text());
    loadTickets();
  } catch(e) {
    alert(`Failed to invalidate: ${e.message}`);
    if (btn) { btn.disabled = false; btn.textContent = '🚨 Mark as Attack'; }
  }
}

function copyId(id) { navigator.clipboard?.writeText(id); }

// ── Metrics ─────────────────────────────────────────────
async function loadMetrics() {
  const container = document.getElementById('metrics-container');
  container.innerHTML = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;">' + '<div class="skel-card skeleton"></div>'.repeat(6) + '</div>';
  try {
    const resp = await fetch('/metrics');
    const data = await resp.json();
    const entries = [];
    for (const [k, v] of Object.entries(data)) {
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
        for (const [k2, v2] of Object.entries(v)) entries.push([`${k}.${k2}`, v2]);
      } else {
        entries.push([k, v]);
      }
    }
    entries.sort(([a], [b]) => a.localeCompare(b));
    if (!entries.length) {
      container.innerHTML = `<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-text">No metrics yet</div></div>`;
      return;
    }
    const highlight = (k) => k.includes('ok') || k.includes('created');
    container.innerHTML = `<div class="metrics-grid">${entries.map(([k, v]) => `
      <div class="metric-card${highlight(k) ? ' highlight' : ''}">
        <div class="metric-key">${k}</div>
        <div class="metric-val">${typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(1)) : v}</div>
      </div>`).join('')}</div>`;
  } catch(e) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-text">Failed to load metrics</div></div>`;
  }
}

// ── Notifications ────────────────────────────────────────
async function loadNotifications() {
  const container = document.getElementById('notif-container');
  container.innerHTML = '<div class="skel-card skeleton"></div>'.repeat(4);
  try {
    const resp = await fetch('/notifications?limit=50');
    const data = await resp.json();
    if (currentView !== 'notifications') setBadge('nb-notif', data.length);
    if (!data.length) {
      container.innerHTML = `<div class="empty-state"><div class="empty-icon">🔕</div><div class="empty-text">No notifications sent yet</div></div>`;
      return;
    }
    container.innerHTML = `<div class="notif-list">${data.map(n => {
      const type = (n.type || n.channel || 'email').toLowerCase();
      const icon = type.includes('slack') ? '💬' : type.includes('resolv') ? '✅' : '📧';
      const cls  = type.includes('slack') ? 'notif-slack' : type.includes('resolv') ? 'notif-resolve' : 'notif-email';
      const ts   = n.sent_at || n.timestamp ? new Date((n.sent_at || n.timestamp) * 1000).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
      return `
      <div class="notif-card">
        <div class="notif-type-icon ${cls}">${icon}</div>
        <div class="notif-main">
          <div class="notif-subject">${n.subject || n.title || 'Notification'}</div>
          <div class="notif-to">to: ${n.to || n.channel || '—'}${n.ticket_id ? ` · ${n.ticket_id}` : ''}</div>
          ${n.body_preview ? `<div class="notif-body">${n.body_preview}</div>` : ''}
        </div>
        <div class="notif-time">${ts}</div>
      </div>`;
    }).join('')}</div>`;
  } catch(e) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-text">Failed to load notifications</div></div>`;
  }
}

// ── Ticket count badge on load ───────────────────────────
async function fetchTicketCount() {
  try {
    const r = await fetch('/tickets?page=1&page_size=1');
    const d = await r.json();
    if (currentView !== 'tickets') setBadge('nb-tickets', d.total);
  } catch {}
}
fetchTicketCount();
</script>
</body>
</html>"""
# SCALING.md

## How the SRE Agent scales

This document describes the current capacity of the system, the path to production scale, and the known bottlenecks at each tier.

---

## Current state (hackathon scope)

The system runs as a single Docker Compose stack with the following characteristics:

| Component | Current implementation | Capacity estimate |
|-----------|----------------------|-------------------|
| API | Single FastAPI instance, Uvicorn, synchronous pipeline | ~5–10 concurrent reports before queue buildup |
| LLM inference | 3 sequential calls per report (triage → summary → runbook) | 4–8s/report (OpenRouter) · 15–30s/report (local Qwen) |
| Ticket store | In-memory dict + `tickets.json` (atomic write) | Thousands of tickets (RAM-bound, ~1KB/ticket) |
| Notification delivery | Synchronous HTTP (Slack) + SMTP within the pipeline | Adds 200–500ms per report |
| RAG index | FAISS flat index built in-process on startup | Fast at Medusa codebase size (~1M tokens); slow rebuild at 10M+ |
| Observability | In-process metrics counters + JSONL append | No impact up to ~100K events |

### Key assumption

The current JSON file persistence (`tickets.json`) uses a single write lock per mutation and is not safe for multi-process deployments. All scaling plans below address this first.

---

## Scaling path

### Phase 1 — Async pipeline (low effort, high impact)

**Problem:** Three sequential LLM calls (triage + summary + runbook) dominate latency.  
**Solution:** Run all three as `asyncio.gather()` after triage completes. Triage must remain first (summary and runbook depend on its output), but summary and runbook are independent and can run in parallel.  
**Expected improvement:** ~40–60% reduction in end-to-end pipeline latency.

```
Current:  triage(4s) → summary(3s) → runbook(2s) = 9s total
Phase 1:  triage(4s) → summary(3s) ┐
                      → runbook(2s) ┘ = 7s total (parallel)
```

### Phase 2 — Queue-based LLM worker pool

**Problem:** Uvicorn threads block on LLM inference; high concurrency causes timeouts.  
**Solution:**
- Move LLM calls to a **Celery worker pool** (Redis as broker)
- API returns a `202 Accepted` with a `job_id` immediately
- UI polls `GET /status/{job_id}` or receives a WebSocket push when done
- Workers can scale horizontally on GPU nodes independently of the API tier

```
Reporter → FastAPI → Redis queue → LLM Worker (GPU) → write ticket → notify
                ↓
           job_id (202)   ←  UI polls /status/{job_id}
```

### Phase 3 — Stateless API + persistent storage

**Problem:** JSON file is not multi-instance safe; in-memory metrics are lost on restart.  
**Solution:**
- Replace `tickets.json` with **PostgreSQL** (via SQLAlchemy async) — connection pooling handles concurrent writes
- Replace in-memory metrics with **Prometheus** counters (exposed at `/metrics` in Prometheus format)
- Replace JSONL notifications log with a `notifications` table
- Deploy multiple Uvicorn replicas behind **nginx** or a cloud load balancer

### Phase 4 — Distributed RAG

**Problem:** FAISS index is rebuilt in-process on every container startup.  
**Solution:**
- Serve the FAISS index from a dedicated **vector search service** (Qdrant or Weaviate)
- Index is built once and served to all API replicas
- Index updates (new codebase versions) happen out-of-band without restarting the API

---

## Identified bottlenecks (ranked by impact)

| # | Bottleneck | Impact | Mitigation |
|---|-----------|--------|-----------|
| 1 | LLM inference latency (3 sequential calls) | High | Parallel calls (Phase 1) + worker pool (Phase 2) |
| 2 | Single JSON file (no multi-instance writes) | High | PostgreSQL (Phase 3) |
| 3 | Synchronous Slack/SMTP within pipeline | Medium | Move to async background task |
| 4 | FAISS in-process rebuild on startup | Medium | Dedicated vector service (Phase 4) |
| 5 | In-memory metrics lost on restart | Low | Prometheus (Phase 3) |

---

## Assumptions

1. Incident volume is bursty (incidents cluster around deployments and business hours) rather than uniformly distributed. Queue-based architecture handles bursts naturally.
2. P1/P2 incidents require sub-10s triage. P3/P4 can tolerate async delivery. The queue should implement priority lanes by severity.
3. The Medusa codebase is treated as a static artifact for RAG purposes. Index rebuilds happen on new releases, not continuously.
4. Notification delivery (email, Slack) is best-effort. Failures are logged and counted but do not block ticket creation.
5. A single PostgreSQL instance with read replicas is sufficient up to ~10K tickets/day. Beyond that, partitioning by `created_at` month is straightforward given the append-heavy access pattern.

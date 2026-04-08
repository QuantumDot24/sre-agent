# SRE Incident Intake & Triage Agent

> Automated SRE incident triage powered by **Qwen LLM + RAG** over the [Medusa](https://github.com/medusajs/medusa) e-commerce codebase.

---

## Architecture Overview

```
Reporter (Browser)
       │
       ▼
  FastAPI /report  ─── multimodal input (text + log file + screenshot)
       │
  ┌────┴─────────────────────────────────────────┐
  │              PIPELINE (5 Stages)              │
  │                                               │
  │  1. INGEST   → guardrails.py                  │
  │               sanitize + injection detection  │
  │                                               │
  │  2. TRIAGE   → indexer.py (ChromaDB/MiniLM)  │
  │               + inference.py (Qwen / OR)      │
  │               → structured JSON + summary     │
  │                                               │
  │  3. TICKET   → mock_linear.py                 │
  │               in-memory + JSON persistence    │
  │                                               │
  │  4. NOTIFY   → notifier.py                    │
  │               email mock + Slack mock         │
  │                                               │
  │  5. RESOLVE  → resolve endpoint               │
  │               → notify reporter               │
  └───────────────────────────────────────────────┘
       │
  Observability: structlog JSON + /metrics endpoint
```

### Key Components

| Component | Tech | Notes |
|-----------|------|-------|
| LLM (primary) | Qwen GGUF via llama-cpp-python | Drop `.gguf` in `./models/` |
| LLM (fallback) | OpenRouter API | Set `OPENROUTER_API_KEY` |
| LLM (demo) | Built-in mock | Works with no keys/models |
| RAG / Embeddings | ChromaDB + MiniLM | Auto-indexes Medusa repo |
| Ticketing | Mock Linear | In-memory + JSON file |
| Notifications | Mock email + Slack | JSONL audit log |
| Observability | structlog + counters | `/metrics` endpoint |

---

## Quick Start

```bash
git clone <this-repo>
cd sre-agent
cp .env.example .env
# (optional) add OPENROUTER_API_KEY or drop a .gguf in ./models/
docker compose up --build
```

Open http://localhost:8000

---

## Setup Instructions

### Option A — No model, demo mode (fastest)

```bash
cp .env.example .env
docker compose up --build
```

The mock backend returns realistic P2 triage responses — sufficient for demo.

### Option B — OpenRouter (real LLM, no GPU needed)

```bash
cp .env.example .env
# Set OPENROUTER_API_KEY in .env
docker compose up --build
```

### Option C — Local Qwen GGUF

```bash
# Download e.g. Qwen2.5-7B-Instruct-Q4_K_M.gguf from HuggingFace
cp qwen*.gguf ./models/
cp .env.example .env
# Set LLM_GPU_LAYERS=-1 for full GPU offload
docker compose up --build
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | HTML demo UI |
| `POST` | `/report` | Submit incident (form-data) |
| `POST` | `/resolve/{id}` | Mark ticket resolved |
| `GET` | `/tickets` | List all tickets |
| `GET` | `/tickets/{id}` | Get ticket detail |
| `GET` | `/metrics` | Observability counters |
| `GET` | `/notifications` | Notification audit log |
| `GET` | `/docs` | OpenAPI docs |

---

## Project Structure

```
sre-agent/
├── agent/
│   ├── guardrails.py      # input sanitization + injection detection
│   ├── inference.py       # Qwen singleton (triage + summary prompts)
│   ├── indexer.py         # Medusa repo clone + ChromaDB + MiniLM
│   ├── pipeline.py        # 5-stage orchestrator
│   └── notifier.py        # email/Slack mock
├── api/
│   └── main.py            # FastAPI app + HTML UI
├── ticketing/
│   └── mock_linear.py     # in-memory ticket store + JSON persistence
├── observability/
│   └── logger.py          # structlog + metrics counters
├── models/                # drop your .gguf here
└── data/                  # auto-generated: chroma/, tickets.json, etc.
```
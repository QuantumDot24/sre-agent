# QUICKGUIDE.md

## Run the SRE Agent in 5 minutes

### Prerequisites

- [Docker Desktop](https://docs.docker.com/get-started/) with the **NVIDIA Container Toolkit** installed
- A GPU with CUDA support (RTX 30xx / 40xx or equivalent)
- *(Optional)* An [OpenRouter](https://openrouter.ai/) API key as cloud fallback
- *(Optional)* A [Slack Incoming Webhook](https://api.slack.com/messaging/webhooks) URL for real Slack notifications

> **Inference modes (auto-selected in order):**
> 1. **Local** — Qwen3.5-0.8B GGUF loaded via the JamePeng llama-cpp-python fork (GPU)
> 2. **OpenRouter** — cloud fallback if `OPENROUTER_API_KEY` is set and models are missing
> 3. **Mock** — instant pre-canned responses, no model or API key required

---

### Step 1 — Clone the repository

```bash
git clone https://github.com/<your-org>/sre-agent.git
cd sre-agent
```

---

### Step 2 — Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in the values you need:

```env
# ── Local inference (default, recommended) ──────────────────────────────────
# Models are downloaded automatically on first run into ./models/
# No API key required.
LLM_CTX=8192
LLM_GPU_LAYERS=35           # Increase if you have >6 GB VRAM, set 0 for CPU-only

# ── Cloud fallback via OpenRouter (optional) ─────────────────────────────────
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=Qwen/Qwen3.5-0.8B

# ── LANGFUSE TRACING  ─────────────────────────────────
LANGFUSE_SECRET_KEY=!sk-lf..."
LANGFUSE_PUBLIC_KEY="pk-lf..."
LANGFUSE_BASE_URL="https://us.cloud.langfuse.com"


# ── Email notifications (Gmail) ──────────────────────────────────────────────
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM=your-account@gmail.com
SMTP_PASSWORD=abcdefghijklmnop   # Gmail App Password (no spaces)
TEAM_EMAIL=sre-team@yourdomain.com

# ── Slack notifications (optional) ───────────────────────────────────────────
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
TEAM_SLACK_CHANNEL=#incidents

# ── Storage paths (defaults work, no change needed) ──────────────────────────
TICKETS_FILE=./data/tickets.json
NOTIFICATIONS_LOG=./data/notifications.jsonl
```

---

### Step 3 — Build and run

```bash
docker compose up --build
```

**What happens on first launch:**

| Step | Service | What it does |
|------|---------|-------------|
| 1 | `model-downloader` | Downloads `Qwen3.5-0.8B.Q4_K_M.gguf` and `mmproj-Qwen3.5-0.8B.f16.gguf` from HuggingFace into `./models/` (~600 MB total). Skipped on subsequent runs if files are already present. |
| 2 | `sre-agent` | Builds the image, compiles the JamePeng llama-cpp-python fork with CUDA (~5-10 min, cached after first build), then starts the app. |

Wait for this line before proceeding:

```
api.startup: SRE Agent started. Index building in background.
```

> **Already have the models?** Place them in `./models/` before running. The downloader will skip files that are already present.
>
> Expected filenames (rename if needed):
> - `Qwen3.5-0.8B.Q4_K_M.gguf`
> - `mmproj-Qwen3.5-0.8B.f16.gguf`

---

### Step 4 — Open the UI

Navigate to [http://localhost:8000](http://localhost:8000)

---

### Step 5 — Submit a test incident

Fill in the form:

| Field | Example value |
|-------|--------------|
| Reporter Email | `you@example.com` |
| Incident Title | `Checkout failing for international cards` |
| Description | `Users outside the US are seeing a payment gateway error since 14:00 UTC. Error: "Card number format invalid". Affects ~40% of checkout attempts.` |
| Log file | *(optional)* drag any `.log` file |
| Screenshot | *(optional)* drag a screenshot of the error |

Click **Submit Incident Report** and wait ~5–10 seconds for triage to complete.

You will see:
- A modal with severity, component, and hypothesis
- A ticket ID (e.g. `INC-A3F2C891`)
- Email and Slack notifications dispatched (check the **Notifications** tab)

---

### Step 6 — Resolve the ticket

1. Navigate to the **Tickets** tab in the sidebar
2. Click the ticket card to expand it
3. Read the **Runbook — Immediate Actions** block
4. Click **✓ Mark Resolved**
5. Confirm the dialog

The LLM generates resolution notes and the reporter receives an email notification.

---

### Verify everything works

| Check | Where to look |
|-------|--------------|
| Ticket created | Tickets tab → All |
| Runbook generated | Expand the ticket card |
| Team notified | Notifications tab → email + slack entries |
| Slack message received | Your `#incidents` channel (if `SLACK_WEBHOOK_URL` is set) |
| Reporter notified on resolve | Notifications tab → ✅ email entry |
| Metrics updated | Metrics tab |
| Tracing | Langfuse dashboard (if `LANGFUSE_PUBLIC_KEY` is set) |

---

### Troubleshooting

**Docker build fails compiling llama-cpp-python**
→ The build requires `nvcc` (CUDA compiler). Make sure you're using the `nvidia/cuda:...-devel` base image (already set in the Dockerfile) and that the NVIDIA Container Toolkit is installed on your host.

**`model-downloader` exits with a network error**
→ HuggingFace may be throttling. Re-run with `docker compose up model-downloader`. Alternatively download the files manually and place them in `./models/`.

**GPU not detected inside the container**
→ Verify with `docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi`. If that fails, reinstall the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

**Triage takes >30 seconds on local mode**
→ Check `LLM_GPU_LAYERS` in `.env`. Setting it to `0` forces CPU inference, which is much slower. Set it to `35` or higher to offload to GPU.

**Gmail notifications failing with "Username and Password not accepted"**
→ The `SMTP_PASSWORD` must be a Gmail **App Password** (not your regular password). Generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Requires 2-Step Verification to be active on the account.

**Slack messages not appearing**
→ Verify `SLACK_WEBHOOK_URL` is the full URL (`https://hooks.slack.com/services/...`). Test it with:
```bash
curl -X POST -H 'Content-type: application/json' \
  --data '{"text":"SRE Agent test"}' \
  $SLACK_WEBHOOK_URL
```

**Port 8000 already in use**
→ Change the port mapping in `docker-compose.yaml`: `"8001:8000"` and open [http://localhost:8001](http://localhost:8001)
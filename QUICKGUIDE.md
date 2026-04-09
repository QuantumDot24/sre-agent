# QUICKGUIDE.md

## Run the SRE Agent in 5 minutes

### Prerequisites

- [Docker Desktop](https://docs.docker.com/get-started/) installed and running
- An [OpenRouter](https://openrouter.ai/) API key (free tier works)
- *(Optional)* A [Slack Incoming Webhook](https://api.slack.com/messaging/webhooks) URL for real Slack notifications

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

Open `.env` and fill in the required values:

```env
# Required — LLM inference via OpenRouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=Qwen/Qwen3.5-0.8B

# Optional — real Slack notifications
# Get this from: api.slack.com → Your App → Incoming Webhooks
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
TEAM_SLACK_CHANNEL=#incidents

# Optional — real email notifications
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM=sre-agent@yourdomain.com
TEAM_EMAIL=sre-team@yourdomain.com

# Already set with defaults — no change needed
LLM_CTX=8192
LLM_GPU_LAYERS=0
TICKETS_FILE=./data/tickets.json
NOTIFICATIONS_LOG=./data/notifications.jsonl
```

> **Note:** If `OPENROUTER_API_KEY` is not set, the agent falls back to a mock LLM that returns realistic pre-canned responses. The full pipeline (ticket creation, notifications, UI) still works in mock mode.

---

### Step 3 — Build and run

```bash
docker compose up --build
```

Wait for this line before proceeding:

```
api.startup: SRE Agent started. Index building in background.
```

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

**Docker build fails on model download**  
→ Models are not bundled. If using local inference, place `.gguf` files in `./models/`. For OpenRouter mode, no model files are needed.

**Triage takes >30 seconds**  
→ Check that `OPENROUTER_API_KEY` is set. Without it the system uses the mock backend which is instant.

**Slack messages not appearing**  
→ Verify `SLACK_WEBHOOK_URL` is the full URL (`https://hooks.slack.com/services/...`). Test it with:
```bash
curl -X POST -H 'Content-type: application/json' \
  --data '{"text":"SRE Agent test"}' \
  $SLACK_WEBHOOK_URL
```

**Port 8000 already in use**  
→ Change the port mapping in `docker-compose.yml`: `"8001:8000"` and open [http://localhost:8001](http://localhost:8001)

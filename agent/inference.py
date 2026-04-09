"""
inference.py — LLM singleton con soporte multimodal (Qwen3.5 + mmproj)
"""

import base64
import glob
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODELS_DIR = os.getenv("MODELS_DIR", "./models")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-7b-instruct")
N_CTX = int(os.getenv("LLM_CTX", "8192"))
N_GPU_LAYERS = int(os.getenv("LLM_GPU_LAYERS", "35"))
MAX_TOKENS = 1024

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------
TRIAGE_PROMPT = """\
You are an expert SRE performing automated incident triage.
If a LOG EXCERPT is present between <log> tags, use it as the PRIMARY evidence for your analysis.
Do NOT invent details not present in the incident report or log.
The codebase context is SECONDARY — only use it if the log/description lack enough information.
Analyze the incident below and the attached screenshot (if any).
Respond **only with a valid JSON object**, no additional text, no markdown.
The JSON must follow this schema:
{{
  "severity": "P1" | "P2" | "P3" | "P4",
  "component": "affected service or module (e.g., checkout, auth, database, frontend, api)",
  "hypothesis": "one-sentence root cause hypothesis",
  "keywords": ["keyword1", "keyword2"],
  "needs_escalation": true | false
}}

Severity guide:
- P1: total outage / data loss risk
- P2: major feature broken, revenue impact
- P3: degraded performance, workaround exists
- P4: cosmetic / low impact

Incident description:
{context}

JSON:"""

SUMMARY_PROMPT = """\
You are an SRE writing a concise technical ticket description. Given the incident context and triage result, write a short summary (3-5 sentences) suitable for the engineering team. Include:
- What is broken
- Who is affected
- Likely cause based on evidence
- Recommended immediate action

Incident context:
{context}

Triage result: {triage_json}

Write the summary in plain text, no bullet points, no markdown headers.
Summary:"""

RESOLUTION_PROMPT = """\
You are an SRE writing a resolution note for the original reporter of an incident.
Based on the ticket information below, write a clear 2-3 sentence note that:
- Confirms what was fixed or addressed
- Mentions the root cause if it was identified
- Includes a brief follow-up recommendation if applicable
Write in plain English, friendly tone, no markdown, no bullet points.

Ticket title: {title}
Affected component: {component}
Root cause hypothesis: {hypothesis}
Original description: {description}

Resolution note:"""

RUNBOOK_PROMPT = """\
You are an SRE writing an emergency runbook for an on-call engineer.
Based on the incident triage below, generate exactly 3 to 5 numbered action steps.
Each step must be concrete and immediately actionable.
Good examples: "Check pod logs with: kubectl logs -n prod deploy/checkout-service --tail=100"
Bad examples: "Investigate the issue" (too vague)
Focus on: investigation first, then mitigation, then verification.
Respond ONLY with a valid JSON array of strings, no extra text, no markdown.
Example format: ["Step 1: ...", "Step 2: ...", "Step 3: ..."]

Severity: {severity}
Component: {component}
Hypothesis: {hypothesis}
Keywords: {keywords}

JSON array:"""


# ---------------------------------------------------------------------------
# Backend: local Qwen3.5
# ---------------------------------------------------------------------------
class _LocalBackend:
    def __init__(self, model_path: str, mmproj_path: Optional[str] = None):
        from llama_cpp import Llama
        from llama_cpp.llama_chat_format import Qwen35ChatHandler

        chat_handler = Qwen35ChatHandler(
            clip_model_path=mmproj_path,
            enable_thinking=False,
            add_vision_id=True,
            verbose=False,
        )

        logger.info(f"Loading model: {model_path}")
        if mmproj_path and os.path.exists(mmproj_path):
            logger.info(f"Loading mmproj: {mmproj_path}")
        else:
            logger.warning("No mmproj file provided. Model will work in text-only mode.")

        self._llm = Llama(
            model_path=model_path,
            chat_handler=chat_handler,
            n_ctx=N_CTX,
            n_gpu_layers=N_GPU_LAYERS,
            verbose=False,
        )

    def generate(self, prompt: str, image_bytes: Optional[bytes] = None, image_media_type: Optional[str] = None) -> str:
        logger.info(f"🖼️ Backend generate: image_bytes={'present' if image_bytes else 'None'} ({len(image_bytes) if image_bytes else 0} bytes)")
        messages = []
        content = []
        if image_bytes:
            mime = image_media_type if image_media_type else "image/png"
            img_b64 = base64.b64encode(image_bytes).decode("utf-8")
            data_url = f"data:{mime};base64,{img_b64}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        content.append({"type": "text", "text": prompt})
        messages.append({"role": "user", "content": content})

        logger.info(f"inference.ctx_check: n_ctx={N_CTX}, image={'yes' if image_bytes else 'no'}, prompt_chars={len(prompt)}")
        response = self._llm.create_chat_completion(
            messages=messages,
            max_tokens=MAX_TOKENS,
            temperature=1.0,
            stop=["</s>", "<|im_end|>"],
        )
        return response["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Backend: OpenRouter
# ---------------------------------------------------------------------------
class _OpenRouterBackend:
    def __init__(self):
        import httpx
        self._client = httpx.Client(timeout=60)
        self._key = OPENROUTER_KEY
        self._model = OPENROUTER_MODEL
        logger.info(f"inference.openrouter: model={self._model}")

    def generate(self, prompt: str, image_bytes: Optional[bytes] = None, image_media_type: Optional[str] = None) -> str:
        resp = self._client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://sre-agent",
                "X-Title": "SRE Agent",
            },
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": MAX_TOKENS,
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Backend: Mock fallback
# ---------------------------------------------------------------------------
class _MockBackend:
    def generate(self, prompt: str, image_bytes: Optional[bytes] = None, image_media_type: Optional[str] = None) -> str:
        if "JSON array" in prompt or "runbook" in prompt.lower():
            return ('["Step 1: Check service logs for recent errors", '
                    '"Step 2: Verify database connection pool metrics", '
                    '"Step 3: Restart the affected service if connections are exhausted", '
                    '"Step 4: Monitor error rate for 5 minutes after restart"]')
        if "JSON" in prompt or "triage" in prompt.lower():
            return ('{"severity":"P2","component":"checkout-service",'
                    '"hypothesis":"Database connection pool exhausted under load",'
                    '"keywords":["checkout","database","timeout","connection"],'
                    '"needs_escalation":true}')
        if "resolution" in prompt.lower():
            return ("The database connection pool exhaustion has been resolved by increasing the pool size. "
                    "The root cause was a surge in concurrent checkout requests. "
                    "Monitor connection metrics over the next 24 hours to confirm stability.")
        return ("The checkout service is experiencing intermittent failures due to "
                "database connection pool exhaustion. Immediate action: increase pool size.")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_backend = None


def _init_backend():
    global _backend
    if _backend is not None:
        return

    model_path = os.path.join(MODELS_DIR, "Qwen.Qwen3.5-0.8B.Q4_K_M.gguf")
    mmproj_path = os.path.join(MODELS_DIR, "mmproj-Qwen.Qwen3.5-0.8B.f16.gguf")

    if not os.path.exists(model_path):
        gguf_files = glob.glob(os.path.join(MODELS_DIR, "*.gguf"))
        for f in gguf_files:
            if "mmproj" not in f.lower():
                model_path = f
                break
    if not os.path.exists(mmproj_path):
        gguf_files = glob.glob(os.path.join(MODELS_DIR, "*.gguf"))
        for f in gguf_files:
            if "mmproj" in f.lower():
                mmproj_path = f
                break
        else:
            mmproj_path = None

    if model_path and os.path.exists(model_path):
        try:
            _backend = _LocalBackend(model_path, mmproj_path)
            logger.info(f"inference.backend: choice=local, model={model_path}, mmproj={mmproj_path}")
            return
        except Exception as e:
            logger.warning(f"inference.local_failed: {e}")

    if OPENROUTER_KEY:
        try:
            _backend = _OpenRouterBackend()
            logger.info("inference.backend: choice=openrouter")
            return
        except Exception as e:
            logger.warning(f"inference.openrouter_failed: {e}")

    logger.warning("inference.backend: choice=mock")
    _backend = _MockBackend()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def run_triage(context: str, image_bytes: Optional[bytes] = None, image_media_type: Optional[str] = None) -> str:
    _init_backend()
    prompt = TRIAGE_PROMPT.format(context=context)
    result = _backend.generate(prompt, image_bytes=image_bytes, image_media_type=image_media_type)
    result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL)
    result = re.sub(r"<\|im_end\|>", "", result)
    result = result.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    result = re.sub(r"\s+", " ", result)
    match = re.search(r"\{.*\}", result, re.DOTALL)
    if match:
        result = match.group(0)
    logger.info(f"inference.triage_done: chars_out={len(result)}")
    return result


def run_summary(context: str, triage_json: str, image_bytes: Optional[bytes] = None, image_media_type: Optional[str] = None) -> str:
    _init_backend()
    prompt = SUMMARY_PROMPT.format(context=context, triage_json=triage_json)
    result = _backend.generate(prompt, image_bytes=image_bytes, image_media_type=image_media_type)
    result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL)
    result = re.sub(r"<\|im_end\|>", "", result)
    logger.info(f"inference.summary_done: chars_out={len(result)}")
    return result


def run_resolution_notes(ticket: dict) -> str:
    _init_backend()
    triage = ticket.get("triage_meta", {})
    prompt = RESOLUTION_PROMPT.format(
        title=ticket.get("title", ""),
        component=triage.get("component", "unknown"),
        hypothesis=triage.get("hypothesis", "Unable to determine"),
        description=(ticket.get("description", ""))[:500],
    )
    result = _backend.generate(prompt)
    result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL)
    result = re.sub(r"<\|im_end\|>", "", result).strip()
    logger.info(f"inference.resolution_done: chars_out={len(result)}")
    return result


def run_runbook(triage: dict) -> list:
    """Generate 3-5 concrete runbook steps for the on-call engineer."""
    _init_backend()
    prompt = RUNBOOK_PROMPT.format(
        severity=triage.get("severity", "P3"),
        component=triage.get("component", "unknown"),
        hypothesis=triage.get("hypothesis", ""),
        keywords=", ".join(triage.get("keywords", [])),
    )
    result = _backend.generate(prompt)
    result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL)
    result = re.sub(r"<\|im_end\|>", "", result).strip()

    # Try to parse JSON array
    match = re.search(r"\[.*\]", result, re.DOTALL)
    if match:
        try:
            steps = json.loads(match.group(0))
            if isinstance(steps, list) and steps:
                logger.info(f"inference.runbook_done: steps={len(steps)}")
                return [str(s) for s in steps[:5]]
        except json.JSONDecodeError:
            pass

    # Fallback: extract numbered lines
    lines = [l.strip() for l in result.split("\n") if re.match(r"^\d+[\.\):]", l.strip())]
    if lines:
        logger.info(f"inference.runbook_done (fallback lines): steps={len(lines)}")
        return lines[:5]

    logger.warning("inference.runbook_done: using default steps")
    return [
        f"Step 1: Check {triage.get('component', 'service')} logs for recent errors",
        "Step 2: Verify health metrics and error rates in your observability dashboard",
        "Step 3: Inspect recent deployments or config changes that may correlate with the incident",
        "Step 4: Apply mitigation (rollback / restart / scale) based on findings",
        "Step 5: Confirm recovery and update ticket with root cause",
    ]
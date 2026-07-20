# Aperture — AI Google Meet Interview System

A Flask application that takes just a candidate's **name, email, and a
Google Meet link**, joins the call as an AI interviewer, asks about the
candidate's role and experience directly (no form fields for that),
conducts an adaptive interview, scores it, and emails a PDF report when
it's done. Ships as an installable PWA with offline support.

## Architecture

Built as a set of coordinated agents, orchestrated by a master agent —
still a single Flask app under the hood, just organized by responsibility
rather than by technical layer:

```
Aperture-AI-Interviewer/
├── app.py                     # Flask entrypoint
├── config.py                  # Env-driven configuration
├── routes/
│   ├── interview_routes.py    # REST API (/api/start, /status, /end, /report)
│   ├── webhook_routes.py      # Meeting BaaS status webhooks (/api/webhooks/meetingbaas)
│   └── audio_ws_routes.py     # WebSocket audio bridge to Meeting BaaS
├── agents/
│   ├── orchestrator_agent.py  # Master coordinator — drives the full interview lifecycle
│   ├── meeting_agent.py       # Joins/speaks/listens/leaves via Meeting BaaS
│   ├── audio_agent.py         # Real-time audio channels + per-speaker turn detection
│   ├── stt_agent.py           # Speech-to-text (provider-agnostic)
│   ├── tts_agent.py           # Text-to-speech (provider-agnostic)
│   ├── interview_agent.py     # Phrases questions from the Reasoning Agent's plan
│   ├── reasoning_agent.py     # Decides category/difficulty/when to conclude
│   ├── memory_agent.py        # Structured candidate context across the interview
│   ├── evaluation_agent.py    # Scores each answer
│   ├── monitor_agent.py       # Tracks audio/call health, surfaces warnings
│   ├── report_agent.py        # Builds the JSON + PDF report
│   └── email_agent.py         # Delivers the report to the candidate (multi-provider)
├── api/
│   ├── gemini_api.py          # LLM client (Cerebras primary, Groq fallback, circuit
│   │                          # breaker) — filename is legacy from an earlier
│   │                          # Gemini-based build, contents aren't Gemini anymore
│   └── meetingbaas_client.py  # Meeting BaaS REST client used by the Meeting Agent
├── interview/
│   └── session.py             # Session data model + in-memory store (not an agent — shared state)
├── utils/
│   ├── startup_validation.py  # Fails fast at boot on missing/bad config, not mid-interview
│   ├── bot_cleanup_sweeper.py # Background sweep that retries removal of stuck bots
│   ├── redis_store.py         # Optional Redis mirror for session status/report durability
│   └── validators.py, helpers.py, logger.py
├── scripts/
│   └── fetch_bot_recording.py # Standalone script to pull a call recording from Meeting BaaS
├── public/                    # PWA assets: manifest.json, sw.js, offline.html, icons
├── static/, templates/        # Frontend
└── reports/, audio/, logs/    # Runtime output
```

### How the agents fit together

The **Orchestrator Agent** drives everything: it asks the **Meeting Agent**
to join the call, the **Interview Agent** (via the **Reasoning Agent**'s
plan and the **Memory Agent**'s context) what to ask, the **TTS Agent** to
speak it, the **Meeting Agent** to capture the answer, the **STT Agent**
to transcribe it, and the **Evaluation Agent** to score it — updating the
**Memory Agent** after every answer so later questions have context. The
**Monitor Agent** watches audio/call health throughout and attaches
warnings to the final report. Once the interview ends, the **Report
Agent** builds the PDF and the **Email Agent** sends it.

Reasoning is deliberately separate from Interview: the Reasoning Agent
decides *what* to ask about (category, difficulty, whether to wrap up
early), and the Interview Agent only handles *phrasing* that into an
actual spoken question. This split makes the interview strategy testable
and adjustable independently of question wording.

## Setup

```bash
python3.11 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then fill in your API keys — see "Required setup" below
python app.py
```

Visit `http://localhost:5000`.

## What the candidate sees

The form only asks for **name, email, and a Google Meet link**. That's it —
no role, experience, or duration dropdowns. Once the bot joins the call,
the AI interviewer asks the candidate directly what role they're
interviewing for and their experience level, and adapts the interview
from there. The interview runs until it naturally concludes (capped by
`MAX_QUESTIONS` in `.env` as a safety limit, not a fixed target), and the
report — including every question, the candidate's answer, the time
taken per question, and full scoring — is emailed automatically (and is
also viewable/downloadable from the report screen either way).

The frontend is also an installable **PWA**: `public/manifest.json` and
`public/sw.js` (served from the root path so the service worker's scope
covers the whole app, not just `/static/`) enable "Add to Home Screen"
and an offline fallback page (`public/offline.html`) when connectivity
drops.

## Required setup

### 1. LLM (question generation + scoring)
```
LLM_PROVIDER=cerebras
CEREBRAS_API_KEY=your-key       # https://cloud.cerebras.ai/ — free, generous limits
CEREBRAS_MODEL=gpt-oss-120b     # Cerebras retired the Llama family from its public
                                 # endpoints; gpt-oss-120b is the current stable default
                                 # (gemma-4-31b / zai-glm-4.7 are preview alternatives)

# optional fallback if Cerebras' quota is exhausted mid-interview
LLM_FALLBACK_PROVIDER=groq
GROQ_API_KEY=your-key           # https://console.groq.com/keys — free, generous limits
```
This build only supports Cerebras and Groq (both free-tier, OpenAI-compatible).
`api/gemini_api.py` is a legacy filename from an earlier Gemini-based
build — the client inside it now only speaks to Cerebras/Groq.

A per-provider **circuit breaker** sits in front of every LLM call: once a
provider fails with a quota/rate-limit error `LLM_QUOTA_BREAKER_THRESHOLD`
times in a row (default 2), it's skipped for `LLM_QUOTA_BREAKER_SECONDS`
(default 300s) and calls go straight to the fallback provider (or a
canned/neutral response) instead of retrying a call that's almost certain
to fail again — this, not just the per-call `LLM_TIMEOUT_SECONDS`, is what
keeps a burnt-out quota from stalling every remaining turn of the
interview.

### 2. Speech-to-text
```
STT_PROVIDER=groq   # or: whisper
```
- `groq` (recommended, and the default in `.env.example`/`render.yaml`) uses Groq's hosted
  Whisper endpoint — much faster, needs network access, same `GROQ_API_KEY` as above, and
  needs no ffmpeg/PyTorch install (what makes this work on Render's plain Python runtime).
- `whisper` runs **locally, no API key**, but is CPU-bound — a 60-90s answer can take over a
  minute to transcribe at the default `WHISPER_MODEL=base`. Requires `ffmpeg` installed
  (`pip install openai-whisper`, commented out in `requirements.txt` by default since it
  pulls in PyTorch — uncomment it if you switch to this provider).

### 3. Text-to-speech
```
TTS_PROVIDER=edge   # or: local
```
- `edge` (recommended) uses free, unlimited Edge-TTS voices — no API key or signup, noticeably
  more natural than `local`. `pip install edge-tts`. It's an unofficial wrapper around the same
  endpoint Microsoft Edge's Read Aloud uses, so treat it as "free and very good" rather than a
  guaranteed SLA.
- `local` uses pyttsx3 — fully offline, no API key, but sounds robotic, and needs `espeak` on Linux.

### 4. Meeting BaaS (how the bot joins Google Meet)
The bot joins via [Meeting BaaS](https://meetingbaas.com) rather than
driving a browser as a guest — Google actively blocks unauthenticated
automated guests, and Meeting BaaS operates the actual infrastructure
needed to join reliably and stream audio both ways.

```
MEETINGBAAS_API_KEY=your-key
PUBLIC_BASE_URL=https://your-ngrok-or-real-domain
```

**Use a v1-type Meeting BaaS API key.** Bot creation and status polling
work fine against the `/v2/bots` endpoints regardless of key type, but the
bot-removal (`DELETE`) endpoint 500s/404s with a v2-type key — that
mismatch is a common cause of bots that never leave the call. As a
belt-and-suspenders fix, `remove_bot()` in `api/meetingbaas_client.py`
retries the delete against both the `/v2/bots/{id}` and unprefixed
`/bots/{id}` paths, but getting a v1 key from your Meeting BaaS dashboard
in the first place avoids the issue entirely.

`PUBLIC_BASE_URL` must be a real, internet-reachable HTTPS URL — Meeting
BaaS can't reach `localhost`. For local dev:
```bash
ngrok http 5000
```
Copy the `https://...ngrok-free.dev` forwarding URL (not the `-> http://localhost:5000` part) into `.env`.

**Also configure a webhook URL in your Meeting BaaS dashboard** pointing
to `{PUBLIC_BASE_URL}/api/webhooks/meetingbaas` — v2's webhook delivery
is set at the account level, not per-request. This webhook is what
signals `cancel_event` when the bot is removed from the call, so the
background interview loop actually stops instead of continuing to poll a
call it's no longer in.

Install `ffmpeg` too (used to convert TTS output into the raw audio
format Meeting BaaS streams into the call):
```bash
# Debian/Ubuntu: sudo apt-get install ffmpeg
# macOS: brew install ffmpeg
# Windows: download from ffmpeg.org and add to PATH
```

### 5. Email (sends the report automatically)
`EMAIL_PROVIDER` picks the delivery mechanism. **On Render's free tier,
outbound SMTP (ports 25/465/587) is blocked**, so `smtp` won't deliver
there — use one of the HTTPS-API providers instead:

```
EMAIL_PROVIDER=smtp   # smtp | brevo | resend | mailjet | emailjs
```

| Provider | Free tier | Notes |
|---|---|---|
| `smtp` | depends on provider | Gmail SMTP, SendGrid, SES, company mail server — works locally, **blocked on Render's free tier** |
| `brevo` | 300 emails/day | Single API key, no domain/sender verification needed |
| `resend` | 3,000/month, 100/day | HTTPS API, unaffected by Render's SMTP block; without a verified domain the sandbox sender can only deliver to your own Resend account email — fine for testing, not for real candidates |
| `mailjet` | 6,000/month, 200/day | HTTPS API; unlike Resend's sandbox, auto-validates your signup email as a sender and can send to any recipient without owning a domain |
| `emailjs` | 200/month | Sends through your own Gmail via OAuth, no SMTP password or domain needed; requires `EMAILJS_PRIVATE_KEY` (server-side, not the public key) |

Each provider has its own block of `*_API_KEY` (and `EMAILJS_*`) variables
in `.env.example` — set the ones matching whichever `EMAIL_PROVIDER` you
choose. If left unconfigured, the interview still runs and the PDF is
still generated and downloadable from the report screen — it just won't
be emailed. This never blocks or crashes the interview flow.

### 6. Redis (optional, recommended in production)
```
REDIS_URL=
```
If set, session status and the JSON report are mirrored to Redis
(`utils/redis_store.py`) on every update, so `GET /api/status` and
`GET /api/report` keep working after a restart or if the request lands
on a different worker than the one running the interview. Free tier:
[Upstash Redis](https://upstash.com) gives 500K commands/month with a
standard `rediss://` URL. This does **not** make the live interview loop
itself multi-worker-safe — see "Deploying to Render" below.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/start` | Start an interview: `{name, email, meet_link}` |
| GET | `/api/status/<session_id>` | Poll live status (includes discovered role/experience) |
| POST | `/api/end` | End an in-progress interview early |
| GET | `/api/report/<session_id>` | JSON report (add `?format=pdf` for the PDF) |
| POST | `/api/webhooks/meetingbaas` | Meeting BaaS status callbacks (not user-facing) |
| GET | `/health` | Health check used by Render's `healthCheckPath` |

## Report contents

Each report includes, per question: the question asked, the candidate's
answer (transcribed), the time taken to answer, the score, and specific
feedback — plus overall/technical/communication/confidence/problem-solving
scores, a summary, strengths, weaknesses, and a hire recommendation.

## Security notes

- API keys live only in `.env`, never in code.
- All user input is validated (`utils/validators.py`) and HTML-escaped.
- CORS is restricted via `CORS_ORIGINS`; set this to your actual frontend
  origin in production instead of `*`.
- Rate limiting is applied via `flask-limiter` (`RATE_LIMIT` in `.env`).
- **Never share API keys in chat, screenshots, or version control.**

## Running it locally

```bash
python app.py
```
Runs the Flask dev server directly (with `threaded=True`, required since
each active session holds two long-lived audio WebSockets open at once)
— fine for local development.

## Deploying to Render

`render.yaml` defines the service. The start command is:
```
gunicorn -k gthread -w 1 --threads 8 --timeout 120 app:app
```
- **`-w 1` (single worker) is required, not optional.** Session state
  (`cancel_event`, the background orchestrator thread, the audio
  WebSocket bridge) is process-local, so `/api/start` and a later
  `/api/end` or webhook must land on the same worker. Scale by upgrading
  the instance size, not the worker count.
- **`-k gthread`, not `-k gevent`.** gevent's monkey-patching previously
  caused an `SSLContext` recursion crash on outbound requests and
  `"asyncio.run() cannot be called from a running event loop"` failures
  in `tts_agent.py`, because it rewrites `ssl`/thread state that this
  app's real OS threads and real asyncio event loops depend on. `gthread`
  gives Flask-Sock the same "one slow connection can't block the others"
  property via real per-connection threads, with no monkey-patching.
- `PUBLIC_BASE_URL` must be set *after* the first deploy, once you have
  the real `onrender.com` URL (or custom domain) — it can't be known
  before that first deploy completes.

On graceful shutdown (which Render triggers on *every* env var change,
not just a code push), `app.py`'s `atexit` hook removes any bot still
mid-interview so it isn't abandoned in the live Google Meet call. A
background `utils/bot_cleanup_sweeper.py` also periodically retries
removal for any session stuck in `leave_failed` (e.g. because Meeting
BaaS's `DELETE` endpoint was itself erroring), so a bot still gets kicked
out once the API recovers, without needing a manual fix or restart.

## Known limitations

- Session state is in-memory (`interview/session.py`), as is agent state
  (Memory/Monitor agents). If `REDIS_URL` is set, session status and the
  JSON report are mirrored to Redis so `/api/status`/`/api/report`
  survive a restart or a request landing on a different worker — but the
  live interview loop (orchestrator thread, audio WebSocket bridge,
  `cancel_event`) is still process-local, so it does *not* make the app
  safe to run multi-worker/multi-instance. Run a single worker process
  rather than relying on Redis to paper over that.
- Confidence scoring is inferred from transcript text only (fluency,
  hedging language), not from voice tone or video — the system never
  claims biometric analysis it isn't doing.
- Turn detection uses Meeting BaaS's per-speaker state (who's currently
  talking) to know when the candidate has finished answering, rather than
  overall stream silence — this matters because the meeting's audio is a
  single mixed stream of every participant, so relying on total silence
  breaks down the moment anyone else in the call is talking. If speaker-
  state data is unavailable for some reason, it falls back to plain
  silence detection.
- The Monitor Agent's warnings (repeated TTS/STT failures, no audio ever
  captured, extended cross-talk) are attached to the final report under
  `monitor_warnings` but aren't yet surfaced in the live status UI.
- Bot removal depends on using a v1-type Meeting BaaS API key (see
  "Meeting BaaS" above) — a v2-type key will still create/monitor bots
  fine but can 500/404 on removal.
- Joining isn't literally 100% guaranteed even via Meeting BaaS (waiting-
  room approval settings, expired links, and org-level meeting
  restrictions are still real failure modes) — but it's a maintained,
  sanctioned integration rather than something fighting platform
  detection.
- Meeting BaaS is a paid service beyond its free trial credits — factor
  that into your deployment costs.

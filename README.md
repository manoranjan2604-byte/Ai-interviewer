# Aperture — AI Google Meet Interview System

A Flask application that takes a candidate's **name, email, and a
Google Meet link** (role and experience level are optional fields — leave
them blank and the AI interviewer asks directly on the call instead),
joins the call as an AI interviewer, conducts an adaptive interview,
scores it, and emails a PDF report when it's done.

## Architecture

Built as a set of coordinated agents, orchestrated by a master agent —
still a single Flask app under the hood, just organized by responsibility
rather than by technical layer:

```
AI-Interview-System/
├── app.py                     # Flask entrypoint
├── config.py                  # Env-driven configuration
├── routes/
│   ├── interview_routes.py    # REST API (/api/start, /status, /end, /report)
│   ├── webhook_routes.py      # Meeting BaaS status webhooks
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
│   └── email_agent.py         # Delivers the report to the candidate
├── api/
│   ├── gemini_api.py          # LLM client (Cerebras primary, Groq fallback) used by several agents
│   │                           #   (name is a holdover from an earlier Gemini-based version —
│   │                           #    it hasn't called Gemini in a while, kept for import stability)
│   └── meetingbaas_client.py  # Meeting BaaS REST client used by the Meeting Agent
├── interview/
│   └── session.py             # Session data model + in-memory store (not an agent — shared state)
├── utils/
│   ├── validators.py, helpers.py, logger.py
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
cp .env.example .env
# Open .env and fill in your API keys (CEREBRAS_API_KEY, MEETINGBAAS_API_KEY,
# PUBLIC_BASE_URL, and an email provider's keys if you want emailed reports).
python app.py
```

Pinned to Python 3.11.9 (see `runtime.txt` / `render.yaml`) — not 3.12.

Visit `http://localhost:5000`.

## What the candidate sees

The form asks for **name, email, and a Google Meet link** — required —
plus **role** and **experience level**, which are optional. Leave them
blank and the AI interviewer asks for both directly once it joins the
call, then adapts the interview from there; fill them in and it skips
that spoken question and jumps straight into role-specific questions.
There's no interview-duration field either way — the interview runs
until it naturally concludes (capped by `MAX_QUESTIONS` in `.env` as a
safety limit, not a fixed target), and the report — including every
question, the candidate's answer, the time taken per question, and full
scoring — is emailed automatically (and is also viewable/downloadable
from the report screen either way).

## Required setup

### 1. LLM (question generation + scoring)
```
LLM_PROVIDER=cerebras
CEREBRAS_API_KEY=your-key   # https://cloud.cerebras.ai/ — free, generous limits

# optional fallback if Cerebras' quota is exhausted mid-interview
LLM_FALLBACK_PROVIDER=groq
GROQ_API_KEY=your-key       # https://console.groq.com/keys — free, generous limits
```
This build only supports Cerebras and Groq (both free-tier, OpenAI-compatible).

### 2. Speech-to-text
```
STT_PROVIDER=groq   # or: whisper
```
- `groq` (the `.env.example` default) uses Groq's hosted Whisper endpoint — same `GROQ_API_KEY` as above, no extra package, no ffmpeg/PyTorch. This is the recommended choice for Render, since it needs no heavy system-level installs.
- `whisper` runs **locally, no API key**, but is CPU-bound — a 60-90s answer can take over a minute to transcribe, and it pulls in PyTorch (a heavy install). Requires `ffmpeg` (`pip install openai-whisper` pulls the Python package; ffmpeg is a separate OS-level install; uncomment it in `requirements.txt` first). Fine for local dev without a `GROQ_API_KEY`, but a poor fit for Render's free/cheap tiers.

### 3. Text-to-speech
```
TTS_PROVIDER=edge   # or: local
```
- `edge` (the `.env.example` default) uses free, unlimited Edge-TTS voices — no API key or signup, noticeably more natural, and no system dependency, which makes it the recommended choice for Render. `pip install edge-tts`. It's an unofficial wrapper around the same endpoint Microsoft Edge's Read Aloud uses, so treat it as "free and very good" rather than a guaranteed SLA.
- `local` uses pyttsx3 — fully offline, no API key, but sounds robotic, and needs `espeak` installed on Linux (macOS/Windows use their built-in speech engines instead).

### 4. Meeting BaaS (how the bot joins Google Meet)
The bot joins via [Meeting BaaS](https://meetingbaas.com) rather than
driving a browser as a guest — Google actively blocks unauthenticated
automated guests, and Meeting BaaS operates the actual infrastructure
needed to join reliably and stream audio both ways.

```
MEETINGBAAS_API_KEY=your-key
PUBLIC_BASE_URL=https://your-ngrok-or-real-domain
```

`PUBLIC_BASE_URL` must be a real, internet-reachable HTTPS URL — Meeting
BaaS can't reach `localhost`. For local dev:
```bash
ngrok http 5000
```
Copy the `https://...ngrok-free.dev` forwarding URL (not the `-> http://localhost:5000` part) into `.env`.

**Also configure a webhook URL in your Meeting BaaS dashboard** pointing
to `{PUBLIC_BASE_URL}/api/webhooks/meetingbaas` — v2's webhook delivery
is set at the account level, not per-request.

Install `ffmpeg` too (used to convert TTS output into the raw audio
format Meeting BaaS streams into the call):
```bash
# Debian/Ubuntu: sudo apt-get install ffmpeg
# macOS: brew install ffmpeg
# Windows: download from ffmpeg.org and add to PATH
```

### 5. Email (sends the report automatically)
`EMAIL_PROVIDER` selects one of five delivery methods — pick whichever
fits your deployment:

```
EMAIL_PROVIDER=smtp   # smtp | brevo | resend | mailjet | emailjs
```

- **`smtp`** — any standard SMTP provider (Gmail SMTP, SendGrid, Mailgun,
  Amazon SES's SMTP interface, a company mail server):
  ```
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USERNAME=your-address@gmail.com
  SMTP_PASSWORD=your-app-password   # not your normal password, if using Gmail
  EMAIL_FROM=your-address@gmail.com
  ```
  **Important if deploying to Render:** Render's free tier blocks
  outbound SMTP ports (25/465/587) entirely, so `smtp` will silently fail
  to send there. Use one of the HTTPS-API providers below instead.
- **`brevo`** — Brevo's transactional email REST API, free tier 300
  emails/day, no SMTP app-password fiddling, just `BREVO_API_KEY`.
- **`resend`** — genuinely free tier (3,000/month, 100/day, no card),
  sent over plain HTTPS so it isn't affected by Render's SMTP block. Set
  `RESEND_API_KEY`. Without a verified domain its sandbox sender can only
  deliver to your own Resend account email — fine for testing, not for
  real candidate reports without owning a domain.
- **`mailjet`** — free tier (6,000/month, 200/day, no card). Unlike
  Resend's sandbox, it auto-validates your signup email as a sender and
  lets you send to any recipient without a verified domain. Set
  `MAILJET_API_KEY` and `MAILJET_API_SECRET`.
- **`emailjs`** — sends through your own Gmail account via OAuth, no SMTP
  app password, no domain needed, also a plain HTTPS call. Free tier 200
  emails/month. Set `EMAILJS_SERVICE_ID`, `EMAILJS_TEMPLATE_ID`,
  `EMAILJS_PUBLIC_KEY`, and `EMAILJS_PRIVATE_KEY` (the private key is
  required for server-side calls — the public key alone gets rejected).
  `EMAILJS_ATTACHMENT_PARAM` is optional: only set it if your EmailJS
  template has an attachment variable, to get the PDF attached; leave
  blank for text-only emails.

If left unconfigured, the interview still runs and the PDF is still
generated and downloadable from the report screen — it just won't be
emailed. This never blocks or crashes the interview flow.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/start` | Start an interview: `{name, email, meet_link, role?, experience_level?}` |
| GET | `/api/status/<session_id>` | Poll live status (includes discovered role/experience) |
| POST | `/api/end` | End an in-progress interview early |
| GET | `/api/report/<session_id>` | JSON report (add `?format=pdf` for the PDF) |

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
Runs the Flask dev server directly — fine for local development. For a
production setup, run behind a proper WSGI server instead of the dev
server, e.g. `gunicorn -k gthread -w 1 --threads 8 app:app`. Keep it to
a single worker: session state, the orchestrator's background thread,
and the audio WebSocket bridge are all process-local (see the
`REDIS_URL` comment in `config.py`), so `/api/start` and `/api/end` for
a given session must hit the same worker/process that's running it.

## Deploying to Render

`render.yaml` is included and pre-configured — connect the repo as a
Blueprint on Render and most settings apply automatically. A few things
still need you:

- Set `MEETINGBAAS_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`, and your
  chosen email provider's key(s) as environment variables in the Render
  dashboard (marked `sync: false` in `render.yaml`, so they're not
  committed to the repo).
- Set `PUBLIC_BASE_URL` **after** the first deploy, once you have the
  real `https://your-app.onrender.com` URL (or custom domain) — Meeting
  BaaS needs this to send webhooks and stream audio back to the app.
- `STT_PROVIDER=groq` and `TTS_PROVIDER=edge` are set by default in
  `render.yaml` specifically because Render's plain Python runtime has no
  system-package installs — `whisper`/`local` would need PyTorch and
  `espeak`, neither of which fit well.
- `EMAIL_PROVIDER=smtp` is the `render.yaml` default but **will not work
  on Render's free tier** (see the email section above) — set
  `EMAIL_PROVIDER=brevo` (or another HTTPS provider) plus the matching
  key instead.
- `REDIS_URL` is optional but strongly recommended in production —
  without it, session status and reports live in-memory only and are
  lost on every redeploy/restart (see "Known limitations" below).
- The start command is deliberately
  `gunicorn -k gthread -w 1 --threads 8 --timeout 120 app:app` — see
  "Running it locally" below for why `-w 1` and `gthread` (not `gevent`)
  matter; don't change these when scaling, upgrade the instance size
  instead.

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
- Joining isn't literally 100% guaranteed even via Meeting BaaS (waiting-
  room approval settings, expired links, and org-level meeting
  restrictions are still real failure modes) — but it's a maintained,
  sanctioned integration rather than something fighting platform
  detection.
- Meeting BaaS is a paid service beyond its free trial credits — factor
  that into your deployment costs.

# Aperture — AI Google Meet Interview System

A Flask application that takes just a candidate's **name, email, and a
Google Meet link**, joins the call as an AI interviewer, asks about the
candidate's role and experience directly (no form fields for that),
conducts an adaptive interview, scores it, and emails a PDF report when
it's done.

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
python3.12 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
# A .env is already included with sensible defaults — just open it and
# fill in your API keys (CEREBRAS_API_KEY, MEETINGBAAS_API_KEY, PUBLIC_BASE_URL,
# and the SMTP_* / EMAIL_FROM vars if you want emailed reports).
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
STT_PROVIDER=whisper   # or: groq
```
- `whisper` runs **locally, no API key**, but is CPU-bound — a 60-90s answer can take over a minute to transcribe. Requires `ffmpeg` installed (`pip install openai-whisper` pulls the Python package; ffmpeg is a separate OS-level install).
- `groq` uses Groq's hosted Whisper endpoint (free tier, same `GROQ_API_KEY` as above) — much faster, needs network access.

### 3. Text-to-speech
```
TTS_PROVIDER=local   # or: edge
```
- `local` uses pyttsx3 — fully offline, no API key, but sounds robotic.
- `edge` uses free, unlimited Edge-TTS voices — no API key or signup, noticeably more natural. `pip install edge-tts`. It's an unofficial wrapper around the same endpoint Microsoft Edge's Read Aloud uses, so treat it as "free and very good" rather than a guaranteed SLA.

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
Any standard SMTP provider works — Gmail SMTP, SendGrid, Mailgun, Amazon
SES's SMTP interface, or a company mail server:
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD=your-app-password   # not your normal password, if using Gmail
EMAIL_FROM=your-address@gmail.com
```
If left unconfigured, the interview still runs and the PDF is still
generated and downloadable from the report screen — it just won't be
emailed. This never blocks or crashes the interview flow.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/start` | Start an interview: `{name, email, meet_link}` |
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

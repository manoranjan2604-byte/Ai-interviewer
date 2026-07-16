"""
app.py
Flask application entrypoint: wires up config, CORS, rate limiting,
routes, and static frontend serving.
"""
# No gevent monkey-patching here (see render.yaml: the start command now
# uses `-k gthread`, not `-k gevent`). gevent's patching was the root cause
# of two separate crashes -- an SSLContext RecursionError on outbound
# requests calls, and "asyncio.run() cannot be called from a running event
# loop" in tts_agent.py -- both stemming from gevent rewriting ssl/thread
# state that this app's real OS threads + real asyncio event loops (the
# background interview thread, the TTS/STT ThreadPoolExecutor) depend on
# behaving normally. gthread's real per-connection threads give Flask-Sock
# the same "one slow connection can't block the others" property without
# any monkey-patching, so none of that is needed.
import atexit
import os

from flask import Flask, render_template, send_from_directory
from flask_cors import CORS

from config import config
from routes.audio_ws_routes import init_audio_ws
from routes.interview_routes import interview_bp
from routes.webhook_routes import webhook_bp
from utils.logger import get_logger
from utils.startup_validation import validate_startup

logger = get_logger("app")


def _cleanup_active_bots() -> None:
    """
    Runs on graceful process shutdown (e.g. gunicorn worker exiting because
    Render is redeploying -- which happens on every env var change, not
    just on a code push). Session state is in-memory only by default, so
    without this, any bot that's mid-interview when the process restarts
    is simply abandoned in the live Google Meet call: the background
    thread that would eventually call leave() dies with the process, and
    the new process that starts up afterward has no record the session
    ever existed. This is what "audio_out: no channel registered for bot
    <id>" repeating in the logs after a restart means.

    This only covers a *graceful* shutdown (SIGTERM with time to run
    cleanup, which is what gunicorn sends on a normal restart/redeploy) --
    it can't help on a hard crash or SIGKILL, since nothing gets a chance
    to run at all in that case. For full resilience against those too,
    the real fix is a persistent (Redis-backed) session store plus a
    periodic reconciliation job that checks Meeting BaaS for orphaned bots
    -- worth doing if this keeps happening, but out of scope here.
    """
    # Local imports: session_store/remove_bot aren't needed until shutdown,
    # and importing at module load time would risk a circular import with
    # routes that import from app.
    from api.meetingbaas_client import remove_bot
    from interview.session import session_store

    for session in session_store.all():
        # Includes "leave_failed" as well as "joined"/"joining": a session
        # can reach leave_failed if remove_bot() already failed once (e.g.
        # Meeting BaaS's DELETE endpoint 500ing) before this shutdown --
        # that bot is still live and this is the last chance this process
        # gets to retry before session_store (in-memory only, not restored
        # from Redis on boot) is gone and the new process has no record of
        # it at all. Missing this case here is exactly how a bot stuck in
        # leave_failed at redeploy time became permanently unrecoverable.
        if session.bot_id and session.bot_status in ("joined", "joining", "leave_failed"):
            try:
                logger.warning(
                    "Shutdown: removing bot %s for session %s so it isn't left "
                    "stranded in the call.", session.bot_id, session.session_id,
                )
                remove_bot(session.bot_id)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Shutdown cleanup failed to remove bot %s for session %s: %s",
                    session.bot_id, session.session_id, exc,
                )


def create_app() -> Flask:
    # Bugs #5 / #28: validate config, provider keys/models, and required
    # local binaries (ffmpeg, whisper) before wiring up any routes. This is
    # what would have caught the Cerebras model_not_found issue (bug #2) at
    # boot instead of on the first interview's first LLM call.
    validate_startup(strict=config.STARTUP_VALIDATION_STRICT)

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SECRET_KEY"] = config.SECRET_KEY

    # PWA assets (icons, manifest, sw.js, offline.html) live in their own
    # public/ folder, flat -- not nested under static/. Flask only wires
    # up one automatic static folder (static_folder above, mapped to
    # /static/<path>), so this second folder gets its own explicit route.
    public_dir = os.path.join(app.root_path, "public")

    CORS(app, resources={r"/api/*": {"origins": config.CORS_ORIGINS}})

    try:
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address

        limiter = Limiter(get_remote_address, app=app, default_limits=[config.RATE_LIMIT])
        limiter.limit(config.RATE_LIMIT)(interview_bp)
    except ImportError:
        logger.warning("flask-limiter not installed; rate limiting disabled.")

    app.register_blueprint(interview_bp)
    app.register_blueprint(webhook_bp)
    init_audio_ws(app)

    atexit.register(_cleanup_active_bots)

    from utils import bot_cleanup_sweeper
    bot_cleanup_sweeper.start()
    atexit.register(bot_cleanup_sweeper.stop)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/health")
    def health():
        return {"status": "ok"}

    # Served from the root path (not /static/...) on purpose: a service
    # worker's default scope is the directory it's served from, so
    # registering it from /static/sw.js would only ever let it control
    # requests under /static/. Serving it as /sw.js gives it scope over
    # the whole app.
    @app.route("/sw.js")
    def service_worker():
        response = send_from_directory(public_dir, "sw.js")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.route("/manifest.json")
    def manifest():
        return send_from_directory(public_dir, "manifest.json")

    @app.route("/offline.html")
    def offline():
        return send_from_directory(public_dir, "offline.html")

    # Icons (and anything else dropped in public/) are served from
    # /public/<filename>, e.g. /public/icon-192.png.
    @app.route("/public/<path:filename>")
    def public_assets(filename):
        return send_from_directory(public_dir, filename)

    return app


app = create_app()

if __name__ == "__main__":
    logger.info("Starting AI Interview System on %s:%s", config.HOST, config.PORT)
    # threaded=True is required: each active interview session holds open two
    # long-lived WebSocket connections (audio_in + audio_out) at once, on top
    # of webhook callbacks and Meeting BaaS status polls. Werkzeug's dev
    # server defaults to handling ONE connection at a time; without this flag
    # those sockets starve each other, Meeting BaaS eventually closes the
    # idle side (WS close code 1005), and no audio ever actually flows in
    # either direction even though the app logs as if it did. For real
    # deployments, prefer a proper concurrent server instead of the dev
    # server, e.g.: gunicorn -k gevent -w 1 --threads 8 app:app
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, threaded=True)

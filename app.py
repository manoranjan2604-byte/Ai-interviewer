"""
app.py
Flask application entrypoint: wires up config, CORS, rate limiting,
routes, and static frontend serving.
"""
from flask import Flask, render_template
from flask_cors import CORS

from config import config
from routes.audio_ws_routes import init_audio_ws
from routes.interview_routes import interview_bp
from routes.webhook_routes import webhook_bp
from utils.logger import get_logger
from utils.startup_validation import validate_startup

logger = get_logger("app")


def create_app() -> Flask:
    # Bugs #5 / #28: validate config, provider keys/models, and required
    # local binaries (ffmpeg, whisper) before wiring up any routes. This is
    # what would have caught the Cerebras model_not_found issue (bug #2) at
    # boot instead of on the first interview's first LLM call.
    validate_startup(strict=config.STARTUP_VALIDATION_STRICT)

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SECRET_KEY"] = config.SECRET_KEY

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

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/health")
    def health():
        return {"status": "ok"}

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

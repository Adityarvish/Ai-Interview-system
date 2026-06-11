from gevent import monkey
monkey.patch_all()

import gevent.hub
gevent.hub.Hub.threadpool_size = 16

import logging
import os
import time
from pathlib import Path

import gevent

from flask import Flask, send_from_directory, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO

from config.settings import Config
from config.mongodb import mongodb
from routes.interview_routes import init_routes
from routes.socket_routes import register_socket_events

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
)
logger = logging.getLogger(__name__)

BACKEND_DIR  = Path(__file__).parent.resolve()
FRONTEND_DIR = BACKEND_DIR.parent / 'frontend'

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path='')
app.config['SECRET_KEY']         = os.environ.get('FLASK_SECRET_KEY', 'dev-change-me')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

CORS(app, resources={r"/api/*": {"origins": "*"}})

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='gevent',
    logger=True,
    engineio_logger=True,
    max_http_buffer_size=10 * 1024 * 1024,
    ping_timeout=300,
    ping_interval=25,
    cors_credentials=True,
)

t_boot = time.perf_counter()

mongodb.connect()
logger.info(f"[BOOT] MongoDB connected in {int((time.perf_counter() - t_boot)*1000)} ms")

init_routes(app, socketio)
register_socket_events(socketio)

def _warmup():
    from services.warm_cache import warmup_all, start_audio_cleanup_loop
    from services.text_to_speech import get_tts_debug_report
    warmup_all()
    start_audio_cleanup_loop(Config.AUDIO_FOLDER)
    report = get_tts_debug_report()
    if report["status"] != "ok":
        logger.critical(
            "[BOOT] TTS health check FAILED — status=%s  error=%s\n"
            "       Interviews will use browser speechSynthesis as fallback.\n"
            "       Fix: %s",
            report["status"], report["error"],
            "pip install groq>=0.9.0  and  set GROQ_API_KEY in backend/.env"
        )
    else:
        logger.info(
            "[BOOT] TTS health check OK — provider=%s  model=%s  voice=%s",
            report["provider"], report["model"], report["voice"]
        )

gevent.spawn(_warmup)
logger.info("[BOOT] Background model warm-up + cleanup loop spawned")


@app.route('/api/tts/health')
def tts_health():
    """
    TTS debug endpoint. Hit this in the browser to confirm Groq TTS is active:
      curl http://localhost:5000/api/tts/health
    Expected response when working:
      {"status": "ok", "provider": "GroqOrpheus", "voice": "leah", ...}
    """
    from services.text_to_speech import get_tts_debug_report
    report = get_tts_debug_report()
    http_status = 200 if report["status"] == "ok" else 503
    return jsonify(report), http_status


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/<path:path>')
def static_or_spa(path):
    full = Path(app.static_folder) / path
    if full.is_file():
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    return send_from_directory(app.static_folder, 'index.html')


@app.errorhandler(500)
def server_error(error):
    logger.exception(f"500: {error}")
    return jsonify({'success': False, 'error': 'Internal server error'}), 500


if __name__ == '__main__':
    if not Config.GROQ_API_KEY:
        logger.error(
            "[BOOT] GROQ_API_KEY is not set in backend/.env — "
            "the system will not be able to generate questions or evaluations. "
            "Get your key at https://console.groq.com/keys"
        )
    else:
        from services.llm_service import GroqService
        if GroqService().check_connection():
            logger.info("[BOOT] Groq API reachable ✓  primary=%s  fallback=%s",
                        Config.GROQ_PRIMARY_MODEL, Config.GROQ_FALLBACK_MODEL)
        else:
            logger.warning(
                "[BOOT] Groq API probe failed — check GROQ_API_KEY in backend/.env "
                "and ensure https://api.groq.com is reachable from this host."
            )

    logger.info(f"Frontend dir: {app.static_folder}")
    logger.info(f"Listening on http://{Config.FLASK_HOST}:{Config.FLASK_PORT}")

    socketio.run(
        app,
        host=Config.FLASK_HOST,
        port=Config.FLASK_PORT,
        debug=False,
        use_reloader=False,
    )

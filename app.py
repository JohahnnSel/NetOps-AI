"""
app.py — Servidor Flask de NetOps AI.

Responsabilidades:
  * Servir la UI (templates/index.html + static/).
  * Exponer /api/chat/stream: recibe un mensaje y devuelve NDJSON (una
    línea JSON por evento) en streaming, usando ITAgent.send_stream().
  * Aislar la memoria conversacional por sesión de navegador: cada sesión
    tiene su propia instancia de ITAgent en el dict SESSIONS, indexado por
    una cookie 'sid'. Sin esto, dos personas abriendo la demo al mismo
    tiempo compartirían historial — un bug de privacidad/coherencia en
    cualquier demo multiusuario.
  * Loguear cada turno en SQLite (db.py) sin bloquear la respuesta al
    usuario si el logging falla.
  * Exponer /api/history y /api/history/<id> para el panel lateral.

NDJSON en vez de Server-Sent Events clásico: NDJSON es trivial de consumir
desde fetch() + ReadableStream en el frontend, sin pelear con la limitación
de EventSource de no soportar POST con body.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

import db
from agent import ITAgent

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("netops.app")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
DB_PATH = os.environ.get("NETOPS_DB_PATH", "data/netops.db")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
PORT = int(os.environ.get("PORT", "5000"))
DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() in {"1", "true", "yes"}

SESSION_COOKIE = "sid"
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 días

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Memoria de sesiones en proceso: {sid: ITAgent}. Para un despliegue
# multiusuario real fuera de esta demo, esto debería moverse a Redis (ver
# README, sección "Notas de seguridad y limitaciones").
SESSIONS: dict[str, ITAgent] = {}

db.init_db(DB_PATH)


def _get_or_create_session_id() -> tuple[str, bool]:
    """Devuelve (session_id, es_nueva). No crea la cookie; eso lo hace la respuesta."""
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        return sid, False
    return str(uuid.uuid4()), True


def _get_agent(session_id: str) -> ITAgent:
    agent = SESSIONS.get(session_id)
    if agent is None:
        agent = ITAgent(api_key=GOOGLE_API_KEY)
        SESSIONS[session_id] = agent
    return agent


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "El campo 'message' es requerido"}), 400
    if not GOOGLE_API_KEY:
        return jsonify({"error": "El servidor no tiene configurada GOOGLE_API_KEY"}), 500

    session_id, is_new = _get_or_create_session_id()
    agent = _get_agent(session_id)

    def generate():
        start = time.perf_counter()
        final_reply = ""
        tool_trace: list[dict] = []
        try:
            for event in agent.send_stream(message):
                if event["type"] == "done":
                    final_reply = event["reply"]
                    tool_trace = event["tool_trace"]
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error durante el streaming del agente")
            yield json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False) + "\n"
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            try:
                db.log_conversation(
                    DB_PATH, session_id, message, final_reply, tool_trace, duration_ms
                )
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo loguear la conversación (no bloquea la respuesta)")

    resp = Response(generate(), mimetype="application/x-ndjson")
    if is_new:
        resp.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
        )
    return resp


@app.route("/api/history")
def history():
    session_id = request.cookies.get(SESSION_COOKIE)
    rows = db.get_history(DB_PATH, session_id=session_id, limit=50)
    return jsonify(rows)


@app.route("/api/history/<int:conversation_id>")
def history_detail(conversation_id: int):
    detail = db.get_conversation_detail(DB_PATH, conversation_id)
    if detail is None:
        return jsonify({"error": "Conversación no encontrada"}), 404
    return jsonify(detail)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "model_configured": bool(GOOGLE_API_KEY)})


if __name__ == "__main__":
    if not GOOGLE_API_KEY:
        logger.warning(
            "GOOGLE_API_KEY no está configurada — copia .env.example a .env y "
            "coloca tu API key de Google AI Studio antes de chatear con el agente."
        )
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)

"""
db.py — Persistencia en SQLite de conversaciones y llamadas a herramientas.

Por qué SQLite y no Postgres/Mongo: este es un proyecto de portafolio de un
solo proceso; SQLite da persistencia real con cero infraestructura
adicional. El esquema usa claves foráneas explícitas, así que migrar a
Postgres más adelante (si el proyecto creciera a multiusuario real) es
sobre todo un cambio de driver, no de diseño.

Esquema:
  conversations(id, session_id, question, response, duration_ms, created_at)
  tool_calls(id, conversation_id FK, tool_name, input_json, output,
             duration_ms, success, created_at)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    question    TEXT NOT NULL,
    response    TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tool_name       TEXT NOT NULL,
    input_json      TEXT NOT NULL,
    output          TEXT NOT NULL,
    duration_ms     REAL NOT NULL,
    success         INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_conversation ON tool_calls(conversation_id);
"""


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Crea las tablas si no existen. Llamar una vez al arrancar la app."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
    logger.info("Base de datos inicializada en %s", db_path)


def log_conversation(
    db_path: str,
    session_id: str,
    question: str,
    response: str,
    tool_trace: list[dict[str, Any]],
    duration_ms: float,
) -> int | None:
    """Guarda un turno completo (pregunta + respuesta + herramientas usadas).

    No lanza excepciones: si el logging falla, se registra en el logger y
    se devuelve None, para no romper la respuesta al usuario por un
    problema de persistencia.
    """
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO conversations (session_id, question, response, duration_ms) "
                "VALUES (?, ?, ?, ?)",
                (session_id, question, response, duration_ms),
            )
            conversation_id = cur.lastrowid
            for call in tool_trace:
                conn.execute(
                    "INSERT INTO tool_calls "
                    "(conversation_id, tool_name, input_json, output, duration_ms, success) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        call.get("tool", "?"),
                        json.dumps(call.get("input", {}), ensure_ascii=False),
                        str(call.get("output", "")),
                        call.get("duration_ms", 0.0),
                        1 if call.get("success") else 0,
                    ),
                )
            return conversation_id
    except sqlite3.Error:
        logger.exception("No se pudo guardar la conversación en SQLite")
        return None


def get_history(db_path: str, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Devuelve conversaciones pasadas (más recientes primero) para el panel Historial."""
    try:
        with _connect(db_path) as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT id, session_id, question, response, duration_ms, created_at "
                    "FROM conversations WHERE session_id = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, session_id, question, response, duration_ms, created_at "
                    "FROM conversations ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error:
        logger.exception("No se pudo leer el historial de SQLite")
        return []


def get_conversation_detail(db_path: str, conversation_id: int) -> dict[str, Any] | None:
    """Devuelve una conversación puntual junto con todas sus tool calls."""
    try:
        with _connect(db_path) as conn:
            conv = conn.execute(
                "SELECT id, session_id, question, response, duration_ms, created_at "
                "FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conv is None:
                return None
            tool_rows = conn.execute(
                "SELECT tool_name, input_json, output, duration_ms, success, created_at "
                "FROM tool_calls WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()
            detail = dict(conv)
            detail["tool_calls"] = [
                {
                    "tool_name": row["tool_name"],
                    "input": json.loads(row["input_json"]),
                    "output": row["output"],
                    "duration_ms": row["duration_ms"],
                    "success": bool(row["success"]),
                    "created_at": row["created_at"],
                }
                for row in tool_rows
            ]
            return detail
    except sqlite3.Error:
        logger.exception("No se pudo leer el detalle de la conversación")
        return None

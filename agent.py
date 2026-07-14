"""
agent.py — ITAgent: agente de diagnóstico de red sobre Google Gemini.

Implementa un loop agentic al estilo ReAct:

    Usuario → Gemini decide → ¿function_call? → ejecutar tool(s) en tools.py
            → devolver resultado a Gemini → repetir hasta que Gemini
            responda solo con texto (o se llegue a MAX_TOOL_ITERATIONS).

Puntos de diseño relevantes:

* Se usa `client.models.generate_content_stream` directamente (no
  `client.chats.create`) para tener control total sobre `contents`
  (la lista de turnos) y poder loguear cada tool call con su duración,
  igual que hacía la versión original basada en Anthropic. `chats.create`
  maneja el historial de forma opaca y activa "automatic function calling"
  quando se pasan callables de Python como tools — acá queremos ejecutar
  las tools nosotros mismos para poder trazarlas.
* `automatic_function_calling` de Gemini nunca se activa porque los tools
  se declaran como `types.FunctionDeclaration` (esquema puro), no como
  funciones Python pasadas directamente — así que el control manual está
  garantizado por diseño, no por una config que alguien podría olvidar.
* `send_stream` es un generador que emite eventos homogéneos
  (`status`, `token`, `tool_start`, `tool_result`, `done`, `error`) listos
  para serializarse a NDJSON en app.py.
* La memoria conversacional vive en `self.history`: una lista de
  `types.Content`. Cada instancia de `ITAgent` = una sesión de navegador
  (ver `SESSIONS` en app.py), así el modelo ve todo el hilo de la
  conversación y resuelve referencias como "¿y el puerto 22?" sin
  volver a preguntar por el host.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Generator

from google import genai
from google.genai import types

from tools import TOOL_FUNCTIONS, TOOL_SPECS

logger = logging.getLogger(__name__)

MODEL_NAME ="gemini-3.1-flash-lite"
MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = """\
Eres NetOps AI, un ingeniero de NOC (Network Operations Center) junior, \
autónomo y meticuloso. Tu trabajo es diagnosticar problemas de red y \
sistemas usando las herramientas de solo lectura disponibles, no solo \
conversar sobre el tema.

## Cómo trabajar

1. Cuando el usuario reporte un problema ("no puedo entrar a X", "X está \
   caído/lento", "revisa Y"), decide tú mismo qué herramientas ejecutar y \
   en qué orden. NO preguntes permiso antes de cada paso — encadena las \
   herramientas necesarias de forma autónoma y solo entonces responde.
2. Secuencia de referencia para "no puedo acceder a <host>":
   dns_query (A) → ping_host → check_port (443 y/o el puerto relevante)
   → si ping o el puerto fallan, traceroute_host para ver dónde se corta
   → si aplica (HTTP/HTTPS), http_check y/o check_ssl_certificate.
   No es una receta rígida: sáltate pasos que no apliquen y agrega otros \
   (whois_lookup, geoip_lookup, reverse_dns, dns_query con otros tipos, \
   get_system_info, list_connections, speed_test_lite) cuando la evidencia \
   lo pida.
3. Usa el historial de la conversación en vez de volver a preguntar. Si el \
   usuario ya mencionó un host y luego pregunta algo ambiguo como "¿y el \
   puerto 22?", asume que se refiere al mismo host.
4. Basa cada afirmación en evidencia real devuelta por las herramientas. \
   Nunca inventes latencias, códigos HTTP, registros DNS ni certificados: \
   si no ejecutaste la herramienta que lo confirma, no lo afirmes.

## Formato de respuesta

Para preguntas triviales o de una sola herramienta (ej: "¿cuál es la IP de \
github.com?", "revisa el certificado de google.com"), responde directo y \
breve, sin forzar secciones.

Para diagnósticos reales (algo está fallando o el usuario pide investigar \
un problema), estructura la respuesta final en:

**Resumen ejecutivo** — 1-2 frases con el estado general.
**Evidencia** — lista de lo que arrojó cada herramienta ejecutada.
**Posibles causas** — ordenadas de más a menos probable según la evidencia.
**Recomendación** — próximos pasos concretos y accionables.

Responde siempre en español, en tono técnico pero claro, como lo haría un \
ingeniero de NOC dejando un reporte para el siguiente turno.
"""


def _build_tool() -> types.Tool:
    declarations = [types.FunctionDeclaration(**spec) for spec in TOOL_SPECS]
    return types.Tool(function_declarations=declarations)


class ITAgent:
    """Agente conversacional con memoria por sesión y tool calling autónomo."""

    def __init__(self, api_key: str, history: list[types.Content] | None = None) -> None:
        if not api_key:
            raise ValueError("Se requiere GOOGLE_API_KEY para inicializar ITAgent")
        self._client = genai.Client(api_key=api_key)
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[_build_tool()],
        )
        self.history: list[types.Content] = history if history is not None else []

    # ------------------------------------------------------------------ #
    # API principal: streaming, usada por app.py
    # ------------------------------------------------------------------ #

    def send_stream(self, user_message: str) -> Generator[dict[str, Any], None, None]:
        """Procesa un mensaje, encadena herramientas y emite eventos incrementales.

        Eventos emitidos (todos dicts serializables a JSON):
          {"type": "status", "step": "thinking"|"executing"|"analyzing"|"done"}
          {"type": "token", "text": "..."}
          {"type": "tool_start", "tool": "...", "input": {...}}
          {"type": "tool_result", "tool": "...", "input": {...}, "output": "...",
           "timestamp": float, "duration_ms": float, "success": bool}
          {"type": "done", "reply": "...", "tool_trace": [...]}
          {"type": "error", "message": "..."}
        """
        self.history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
        )
        tool_trace: list[dict[str, Any]] = []
        final_text = ""

        yield {"type": "status", "step": "thinking"}

        for iteration in range(MAX_TOOL_ITERATIONS):
            round_text = ""
            function_calls: list[types.FunctionCall] = []
            model_parts: list[types.Part] = []

            try:
                stream = self._client.models.generate_content_stream(
                    model=MODEL_NAME,
                    contents=self.history,
                    config=self._config,
                )
                for chunk in stream:
                    if not chunk.candidates or not chunk.candidates[0].content:
                        continue
                    for part in chunk.candidates[0].content.parts or []:
                        if getattr(part, "function_call", None):
                            function_calls.append(part.function_call)
                            model_parts.append(part)
                        elif getattr(part, "text", None):
                            round_text += part.text
                            model_parts.append(part)
                            yield {"type": "token", "text": part.text}
            except Exception as exc:  # noqa: BLE001
                logger.exception("Error llamando a Gemini")
                yield {"type": "error", "message": f"Error del modelo: {exc}"}
                return

            if model_parts:
                self.history.append(types.Content(role="model", parts=model_parts))

            if not function_calls:
                final_text += round_text
                break

            yield {"type": "status", "step": "executing"}
            response_parts: list[types.Part] = []

            for call in function_calls:
                tool_name = call.name
                tool_args = dict(call.args or {})
                yield {"type": "tool_start", "tool": tool_name, "input": tool_args}

                start = time.perf_counter()
                func = TOOL_FUNCTIONS.get(tool_name)
                if func is None:
                    output = f"Error: herramienta desconocida '{tool_name}'"
                    success = False
                else:
                    try:
                        output = func(**tool_args)
                        success = not str(output).startswith("Error")
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Fallo ejecutando la herramienta '%s'", tool_name)
                        output = f"Error: {exc}"
                        success = False
                duration_ms = round((time.perf_counter() - start) * 1000, 2)

                trace_entry = {
                    "tool": tool_name,
                    "input": tool_args,
                    "output": output,
                    "timestamp": time.time(),
                    "duration_ms": duration_ms,
                    "success": success,
                }
                tool_trace.append(trace_entry)
                yield {"type": "tool_result", **trace_entry}

                response_parts.append(
                    types.Part.from_function_response(
                        name=tool_name, response={"result": output}
                    )
                )

            self.history.append(types.Content(role="tool", parts=response_parts))
            yield {"type": "status", "step": "analyzing"}
        else:
            final_text += (
                "\n\n_Se alcanzó el límite de pasos automáticos "
                f"({MAX_TOOL_ITERATIONS}); este diagnóstico está basado en la "
                "evidencia reunida hasta este punto._"
            )

        yield {"type": "status", "step": "done"}
        yield {"type": "done", "reply": final_text, "tool_trace": tool_trace}

    # ------------------------------------------------------------------ #
    # API simple no-streaming, para pruebas rápidas por consola
    # ------------------------------------------------------------------ #

    def send(self, user_message: str) -> dict[str, Any]:
        """Envuelve send_stream() y devuelve {"reply": str, "tool_trace": [...]}."""
        reply = ""
        tool_trace: list[dict[str, Any]] = []
        for event in self.send_stream(user_message):
            if event["type"] == "done":
                reply = event["reply"]
                tool_trace = event["tool_trace"]
            elif event["type"] == "error":
                reply = event["message"]
        return {"reply": reply, "tool_trace": tool_trace}

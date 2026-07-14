# NetOps AI — Agente de Operaciones de Red sobre Google Gemini

Agente Inteligente para Diagnóstico y Operaciones de Redes utilizando Google Gemini y Function Calling: 
recibe un problema en lenguaje natural, decide qué
herramientas de diagnóstico ejecutar, las encadena de forma autónoma, y
construye un diagnóstico con evidencia real

Proyecto de portafolio pensado para demostrar: **AI Agents, Function
Calling, Networking, Linux/Windows, IT Support, DevOps y Observabilidad.**

---

## Características

```
- Agente basado en Google Gemini
- Function Calling manual
- Diagnóstico automático de redes
- Streaming NDJSON
- Historial persistente
- Herramientas de diagnóstico
- Docker
- Validación de entradas
- Interfaz estilo NOC
```
---

## Arquitectura

```
Usuario
   │
   ▼
templates/index.html + static/app.js   (UI, streaming NDJSON, sin frameworks)
   │
   ▼
Flask (app.py)  ──  SQLite (db.py)  ──  conversaciones + tool calls
   │
   ▼
ITAgent (agent.py)  ──  loop ReAct manual + memoria por sesión + streaming
   │
   ├── Google Gemini API (function calling, google-genai SDK)
   │
   ▼
tools.py  ──  12 herramientas de solo lectura
```
---

## Estructura del proyecto

```text
NetOps-AI/
│
├── agent.py
├── app.py
├── tools.py
├── db.py
│
├── templates/
│   └── index.html
│
├── static/
│   ├── app.js
│   └── style.css
│
├── data/
│   └── netops.db
│
├── Imagenes/
│   ├── codigo/
│   ├── estado/
│   ├── pruebagit/
│   └── pruebaservidor/
│
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
---

## Tecnologías

| Capa | Tecnología |
|---|---|
| Modelo / IA | Google Gemini (`gemini-2.5-flash`) vía SDK oficial [`google-genai`](https://pypi.org/project/google-genai/), function calling manual |
| Backend | Python 3.12, Flask 3 |
| Persistencia | SQLite (`sqlite3` de la librería estándar) |
| Frontend | HTML + CSS + JS vanilla, sin frameworks ni build step |
| Contenedores | Docker / docker-compose |
| Config | `python-dotenv` |

Este proyecto **no usa el SDK de Anthropic**: toda la comunicación con el
modelo pasa por `google.genai`, con `GOOGLE_API_KEY` como única credencial
necesaria (capa gratuita de Google AI Studio).

---

## Google Gemini y function calling: cómo funciona el agente

`agent.py` no usa `client.chats.create(...)` (que gestiona el historial de
forma opaca y puede activar *automatic function calling* si le pasas
funciones Python directamente). En cambio, llama a
`client.models.generate_content_stream(...)` a mano, manteniendo `self.history`
como una lista explícita de `types.Content`. La razón: así el agente puede
trazar cada tool call con su duración y resultado —tal como pide el
panel **Herramientas** de la UI— en vez de que el SDK lo resuelva por
detrás sin visibilidad.

El loop, en `ITAgent.send_stream()`, sigue el patrón ReAct:

1. Se agrega el mensaje del usuario a `self.history`.
2. Se llama a Gemini en streaming con las 12 `FunctionDeclaration`
   definidas en `tools.py` como tools disponibles.
3. Si la respuesta trae una o más `function_call`, el agente **las
   ejecuta él mismo** contra `tools.py`, mide la duración con
   `time.perf_counter()`, arma un `Part.from_function_response(...)` por
   cada una, las agrega al historial con `role="tool"`, y vuelve al
   paso 2 — sin preguntarle permiso al usuario entre pasos.
4. Si la respuesta es solo texto, ese texto se transmite token a token
   (eventos `token`) y el loop termina.
5. Esto se repite hasta `MAX_TOOL_ITERATIONS = 8` rondas, como límite de
   seguridad ante un loop de tool calls que no converge.

El `SYSTEM_PROMPT` le da a Gemini una secuencia de referencia (DNS → ping →
puerto → traceroute si falla → SSL/HTTP si aplica) y la instrucción
explícita de encadenar sin pedir permiso en cada paso, además del formato
de diagnóstico (**Resumen ejecutivo → Evidencia → Posibles causas →
Recomendación**) para problemas reales, y respuestas directas para
preguntas triviales de una sola herramienta.

**Nota de diseño:** el pedido original distinguía `dns_lookup` (básico) de
un "DNS avanzado" nuevo. Se fusionaron en una sola herramienta
`dns_query(host, record_type)` — dos tools que hacen lo mismo con distinto
nombre solo le complica al modelo decidir cuál llamar. Por eso el conteo
final es **12 herramientas**, no 13: una fusión deliberada, documentada acá
y en el propio `tools.py`, no una tool faltante.

---

## Herramientas disponibles (`tools.py`)

Todas de solo lectura / no destructivas, con timeout explícito y sin
`shell=True`:

| Herramienta | Qué hace |
|---|---|
| `ping_host` | ICMP ping, latencia y pérdida de paquetes (Linux/Mac/Windows) |
| `traceroute_host` | Traza de saltos de red (`traceroute`/`tracepath`/`tracert`) |
| `check_port` | Verifica si un puerto TCP está abierto (socket, sin dependencias del SO) |
| `dns_query` | Registros A, AAAA, MX, TXT, NS, CNAME, SOA (vía `dnspython`) |
| `reverse_dns` | IP → hostname (PTR) |
| `get_system_info` | SO, CPU, memoria, disco, uptime del servidor donde corre el agente |
| `list_connections` | Conexiones de red activas del sistema local |
| `http_check` | GET/HEAD, código HTTP, headers clave, tiempo de respuesta |
| `check_ssl_certificate` | Emisor, validez, SAN, versión TLS |
| `whois_lookup` | Cliente WHOIS propio (socket crudo puerto 43, sigue el referral de IANA) |
| `geoip_lookup` | Ubicación aproximada + ISP/ASN vía `ip-api.com` |
| `speed_test_lite` | Descarga de prueba contra Cloudflare, Mbps aproximados |

`whois_lookup` implementa el protocolo WHOIS a mano en vez de depender de
un paquete externo con soporte irregular por TLD. `geoip_lookup` usa
`ip-api.com` (gratis, sin API key, HTTP) — límite conocido: ~45
requests/minuto, suficiente para una demo, no para producción con volumen.

**Compatibilidad Windows/Linux:** `ping_host`/`traceroute_host` detectan el
SO con `platform.system()` (`ping -c` en Linux/Mac vs `ping -n` en Windows;
`traceroute`/`tracepath` vs `tracert`). El resto de herramientas usan
sockets y librerías Python puras — multiplataforma por diseño, sin lógica
adicional.

---

## Memoria conversacional y sesiones

Cada sesión de navegador tiene su propia instancia de `ITAgent`
(`app.py`, diccionario `SESSIONS` indexado por una cookie `sid`, httpOnly).
Así, si preguntas "¿y el puerto 22?" después de mencionar `github.com`,
Gemini ve todo el historial de esa sesión (`self.history`, una lista de
`types.Content`) y resuelve la referencia sin volver a preguntar. Sin
aislar por sesión, dos personas abriendo la demo al mismo tiempo
compartirían memoria — un bug de privacidad/coherencia en cualquier demo
multiusuario.

---

## Streaming (NDJSON)

`ITAgent.send_stream()` es un generador que emite eventos incrementales:
`status`, `token`, `tool_start`, `tool_result`, `done`, `error`. `app.py`
los expone como **NDJSON** (`POST /api/chat/stream`, una línea JSON por
evento) en vez de Server-Sent Events clásico, porque NDJSON es trivial de
consumir desde `fetch()` + `ReadableStream` sin pelear con la limitación de
`EventSource` de no soportar `POST` con body. El frontend (`static/app.js`)
lee el stream chunk a chunk, parte por líneas y actualiza la UI en tiempo
real (texto token a token + la barra de pipeline de estado).

---

## Logs persistentes (SQLite)

`db.py` crea dos tablas: `conversations` (pregunta, respuesta, duración
total) y `tool_calls` (una fila por cada ejecución de herramienta,
enlazada a su conversación por `conversation_id`, con clave foránea). Cada
turno del chat se registra automáticamente al terminar el streaming
(`app.py::chat_stream`), sin bloquear la respuesta al usuario si el
logging falla (`try/except` alrededor de `db.log_conversation`).

---

## UI: consola NOC

`templates/index.html` + `static/style.css` + `static/app.js`, sin
frameworks. Estética de sala de operaciones: fondo azul-carbón (no negro
puro) con **cuatro acentos semánticos de estado** —verde ok, ámbar
degradado, rojo crítico, azul activo/info—, tipografía IBM Plex Mono para
datos técnicos e IBM Plex Sans para texto conversacional.

El elemento distintivo es la **barra de pipeline** bajo el header
(`Pensando → Ejecutando → Analizando → Diagnóstico`), un riel que se
ilumina segmento por segmento con cada evento `status` del streaming — es
lo que hace que la interfaz se sienta como un agente trabajando y no un
chatbot esperando input. El sidebar tiene dos paneles: **Herramientas**
(traza en vivo de la conversación actual, con badges de éxito/error y
duración) e **Historial** (conversaciones pasadas desde SQLite). Los
mensajes del agente se renderizan con un markdown-lite propio (sin
dependencias) que soporta `## encabezados`, `**negritas**`, listas y
`` `code` ``.

---

## Instalación y ejecución en Linux/Mac

```bash
cd NetOps-AI
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edita .env y coloca tu GOOGLE_API_KEY (gratis en https://aistudio.google.com/apikey)
nano .env

python app.py
```

Abre `http://localhost:5000`.

Si `traceroute` no está instalado en tu distro:
```bash
sudo apt install traceroute      # Debian/Ubuntu
sudo dnf install traceroute      # Fedora
```

En Windows, `ping` y `tracert` ya vienen incluidos en el sistema — no se
necesita instalar nada adicional para esas dos herramientas.

### Probar rápido desde la terminal (sin UI)

```bash
source venv/bin/activate
python -c "
import os
from agent import ITAgent
a = ITAgent(api_key=os.environ['GOOGLE_API_KEY'])
r = a.send('¿Está abierto el puerto 443 en github.com?')
print(r['reply'])
for t in r['tool_trace']:
    print(t['tool'], t['success'], t['duration_ms'], 'ms')
"
```

---

## Ejecución con Docker

```bash
cp .env.example .env
# edita .env con tu GOOGLE_API_KEY

docker compose up --build
```

Abre `http://localhost:5000`. La base SQLite queda en `./data/netops.db` en
tu máquina host, persistente entre reinicios del contenedor.

```bash
docker compose down   # detener
```

---

## Ejemplos para probar el encadenamiento automático

- `"No puedo entrar a github.com"` → debería encadenar DNS → ping → puerto
  443, y si el ping falla, seguir con traceroute.
- Después: `"¿y el puerto 22?"` → debe recordar que seguimos hablando de
  github.com sin que lo repitas.
- `"Revisa el certificado SSL de google.com"` → una sola herramienta,
  respuesta directa sin forzar el formato de diagnóstico completo.
- `"Haz un whois de un dominio que parezca sospechoso, prueba con este:
  xn--80ak6aa92e.com"` → deja que Gemini interprete y use `whois_lookup`.

---

## Seguridad

- Todas las herramientas son de solo lectura / no destructivas.
- `subprocess` se llama siempre con lista de argumentos (nunca
  `shell=True` con strings concatenados), lo que evita inyección de
  comandos.
- Hosts, IPs, puertos, URLs y dominios se validan/sanitizan antes de
  tocar `subprocess`, `socket` o `requests` (`tools.py::_validate_*`).
- `http_check` solo permite `GET`/`HEAD`.
- Toda operación de red tiene timeout explícito, para que un host que no
  responde no cuelgue el agente completo.
- El `.env` con la API key **no se versiona** (`.gitignore`) y tampoco se
  copia a la imagen Docker.
- La cookie de sesión (`sid`) es `httpOnly` y `SameSite=Lax`.

## Limitaciones conocidas

- `SESSIONS` vive en memoria del proceso Flask: para un despliegue
  multiusuario real fuera de esta demo, habría que moverlo a Redis (o
  similar) y correr la app con varios workers detrás de un balanceador.
- El servidor de desarrollo de Flask (`app.run(...)`) no está pensado para
  producción; para eso usar un WSGI server como `gunicorn` o `waitress`
  delante.
- `geoip_lookup` depende de `ip-api.com` (HTTP, ~45 req/min en su capa
  gratuita) y `speed_test_lite` de la disponibilidad del endpoint de
  Cloudflare — ambas son dependencias externas fuera de nuestro control.
- `list_connections` puede requerir privilegios elevados según el sistema
  operativo y el usuario con el que corre el proceso.
- La autenticación real de usuarios no está implementada — la "sesión" hoy
  es solo una cookie `sid` sin login detrás.

## Roadmap

- Herramienta de lectura de logs (`journalctl`, `/var/log/syslog`).
- Integración con un sistema de tickets.
- Autenticación de usuarios real.
- Rate limiting sobre `geoip_lookup`/`whois_lookup` para respetar los
  límites de los servicios externos gratuitos.
- Mover `SESSIONS` a Redis y `SQLite` a Postgres para un despliegue
  multiusuario real (el esquema de `db.py` ya usa claves foráneas
  explícitas pensando en esta migración).
- Capturas de pantalla de la consola en este README (agrégalas después de
  correr la app localmente — no se incluyen por defecto en este scaffold).
  
 ## Resultados

Durante las pruebas se verificó correctamente:

- ✅ Estado del sistema
- ✅ Ping ICMP
- ✅ Resolución DNS
- ✅ Traceroute
- ✅ Verificación de puertos
- ✅ HTTP Check
- ✅ SSL
- ✅ WHOIS
- ✅ GeoIP
- ✅ Historial SQLite
- ✅ Streaming
- ✅ Docker
- ✅ Docker Compose


 ## Autor

**Joan André Gallo Ugarte**

Ingeniería Mecatrónica

Universidad Continental


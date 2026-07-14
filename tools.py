"""
tools.py — Herramientas de diagnóstico de red/sistema para NetOps AI.

Todas las funciones de este módulo son de SOLO LECTURA y no destructivas:
ninguna modifica configuración de red, mata procesos ni escribe fuera de la
base de datos propia de la aplicación. Están pensadas para ejecutarse como
"function calls" invocadas por el modelo de Gemini, así que cada una:

  * Recibe únicamente tipos primitivos (str, int, float) como parámetros,
    igual que llegan desde el JSON de la function call.
  * Nunca lanza excepciones hacia quien la llama: cualquier fallo se
    devuelve como texto plano que empieza con "Error: ..." (así el propio
    modelo puede leer el motivo y decidir cómo continuar).
  * Devuelve siempre un string legible por humanos (y por el modelo), no
    un objeto — simplifica tanto el prompt como el logging en SQLite.
  * Tiene timeouts explícitos en cualquier operación de red o proceso, para
    que un host que no responde no cuelgue el agente completo.

Este módulo NO importa nada de google.genai: la conversión a
`types.FunctionDeclaration` ocurre en agent.py a partir de TOOL_SPECS, para
mantener tools.py reutilizable e independiente del proveedor del modelo.
"""

from __future__ import annotations

import ipaddress
import logging
import platform
import re
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable

import psutil
import requests

try:
    import dns.resolver
    import dns.reversename
except ImportError:  # pragma: no cover - dnspython siempre debería estar instalado
    dns = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Validación / sanitización de entradas
# --------------------------------------------------------------------------- #

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

_ALLOWED_HTTP_METHODS = {"GET", "HEAD"}
_ALLOWED_DNS_TYPES = {"A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA"}

DEFAULT_TIMEOUT = 5  # segundos, usado como base para operaciones de socket/HTTP


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _validate_host(host: str) -> str:
    """Valida que `host` sea un hostname o IP con forma razonable.

    No resuelve DNS aquí (eso lo hace cada herramienta si lo necesita);
    solo evita que caracteres de shell o basura lleguen a subprocess/socket.
    """
    host = (host or "").strip().rstrip(".")
    if not host:
        raise ValueError("El host no puede estar vacío")
    if _is_valid_ip(host):
        return host
    if not _HOSTNAME_RE.match(host):
        raise ValueError(f"'{host}' no parece un hostname o IP válido")
    return host


def _validate_port(port: int) -> int:
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(f"Puerto fuera de rango (1-65535): {port}")
    return port


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("La URL debe comenzar con http:// o https://")
    return url


# --------------------------------------------------------------------------- #
# 1. ping_host
# --------------------------------------------------------------------------- #

def ping_host(host: str, count: int = 4) -> str:
    """Hace ping a un host y devuelve la salida resumida (latencia, pérdida)."""
    try:
        host = _validate_host(host)
        count = max(1, min(int(count), 10))
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}"

    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", str(count), "-w", "2000", host]
    else:
        cmd = ["ping", "-c", str(count), "-W", "2", host]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=8 + count * 2
        )
    except subprocess.TimeoutExpired:
        return f"Error: ping a '{host}' agotó el tiempo de espera"
    except FileNotFoundError:
        return "Error: el comando 'ping' no está disponible en este sistema"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado en ping_host")
        return f"Error: {exc}"

    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        return f"Error: '{host}' no responde al ping.\n{output}"
    return output


# --------------------------------------------------------------------------- #
# 2. traceroute_host
# --------------------------------------------------------------------------- #

def traceroute_host(host: str, max_hops: int = 20) -> str:
    """Traza la ruta de saltos de red hacia un host (traceroute/tracert)."""
    try:
        host = _validate_host(host)
        max_hops = max(1, min(int(max_hops), 30))
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}"

    system = platform.system().lower()
    if system == "windows":
        candidates = [["tracert", "-h", str(max_hops), "-w", "1500", host]]
    else:
        candidates = [
            ["traceroute", "-m", str(max_hops), "-w", "2", host],
            ["tracepath", "-m", str(max_hops), host],
        ]

    last_error = "comando de traceroute no disponible"
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            output = result.stdout.strip() or result.stderr.strip()
            return output or "Error: traceroute no devolvió salida"
        except FileNotFoundError:
            last_error = f"'{cmd[0]}' no está instalado"
            continue
        except subprocess.TimeoutExpired:
            return f"Error: traceroute a '{host}' agotó el tiempo de espera"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo inesperado en traceroute_host")
            return f"Error: {exc}"

    return f"Error: {last_error}"


# --------------------------------------------------------------------------- #
# 3. check_port
# --------------------------------------------------------------------------- #

def check_port(host: str, port: int, timeout: float = 3.0) -> str:
    """Intenta abrir una conexión TCP a host:port y mide la latencia."""
    try:
        host = _validate_host(host)
        port = _validate_port(port)
        timeout = max(0.5, min(float(timeout), 10.0))
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}"

    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            return f"Puerto {port} en '{host}' está ABIERTO (conectado en {elapsed_ms} ms)"
    except socket.timeout:
        return f"Puerto {port} en '{host}' no respondió (timeout de {timeout}s) — probablemente filtrado/firewall"
    except ConnectionRefusedError:
        return f"Puerto {port} en '{host}' está CERRADO (conexión rechazada)"
    except socket.gaierror as exc:
        return f"Error: no se pudo resolver '{host}' ({exc})"
    except OSError as exc:
        return f"Error: {exc}"


# --------------------------------------------------------------------------- #
# 4. dns_query
# --------------------------------------------------------------------------- #

def dns_query(host: str, record_type: str = "A") -> str:
    """Consulta registros DNS (A, AAAA, MX, TXT, NS, CNAME, SOA) de un dominio."""
    try:
        host = _validate_host(host)
    except ValueError as exc:
        return f"Error: {exc}"

    record_type = (record_type or "A").strip().upper()
    if record_type not in _ALLOWED_DNS_TYPES:
        return f"Error: tipo de registro '{record_type}' no soportado. Usa uno de: {', '.join(sorted(_ALLOWED_DNS_TYPES))}"

    if dns is None:
        return "Error: dnspython no está instalado en el servidor"

    resolver = dns.resolver.Resolver()
    resolver.lifetime = DEFAULT_TIMEOUT
    resolver.timeout = DEFAULT_TIMEOUT

    try:
        answer = resolver.resolve(host, record_type)
        records = [rdata.to_text() for rdata in answer]
        if not records:
            return f"Sin registros {record_type} para '{host}'"
        joined = "\n".join(f"  - {r}" for r in records)
        return f"Registros {record_type} de '{host}':\n{joined}"
    except dns.resolver.NXDOMAIN:
        return f"Error: el dominio '{host}' no existe (NXDOMAIN)"
    except dns.resolver.NoAnswer:
        return f"Sin registros {record_type} para '{host}' (el dominio existe pero no tiene ese tipo de registro)"
    except dns.exception.Timeout:
        return f"Error: timeout consultando DNS para '{host}'"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado en dns_query")
        return f"Error: {exc}"


# --------------------------------------------------------------------------- #
# 5. reverse_dns
# --------------------------------------------------------------------------- #

def reverse_dns(ip: str) -> str:
    """Resuelve una IP a su hostname (PTR / reverse DNS)."""
    ip = (ip or "").strip()
    if not _is_valid_ip(ip):
        return f"Error: '{ip}' no es una dirección IP válida"

    try:
        hostname, aliases, _ = socket.gethostbyaddr(ip)
        alias_txt = f" (alias: {', '.join(aliases)})" if aliases else ""
        return f"{ip} → {hostname}{alias_txt}"
    except socket.herror:
        return f"Sin registro PTR para '{ip}' (no tiene reverse DNS configurado)"
    except socket.timeout:
        return f"Error: timeout resolviendo PTR de '{ip}'"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado en reverse_dns")
        return f"Error: {exc}"


# --------------------------------------------------------------------------- #
# 6. get_system_info
# --------------------------------------------------------------------------- #

def get_system_info() -> str:
    """Devuelve un resumen del sistema local: SO, CPU, memoria, disco, uptime."""
    try:
        uname = platform.uname()
        cpu_percent = psutil.cpu_percent(interval=0.3)
        cpu_count = psutil.cpu_count(logical=True)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        boot_ts = psutil.boot_time()
        uptime = datetime.now(timezone.utc) - datetime.fromtimestamp(boot_ts, tz=timezone.utc)

        lines = [
            f"Sistema operativo : {uname.system} {uname.release} ({uname.machine})",
            f"Hostname          : {uname.node}",
            f"Python            : {platform.python_version()}",
            f"CPU               : {cpu_count} núcleos lógicos, uso actual {cpu_percent}%",
            f"Memoria           : {mem.used / (1024**3):.1f} GB usados de {mem.total / (1024**3):.1f} GB ({mem.percent}%)",
            f"Disco (/)         : {disk.used / (1024**3):.1f} GB usados de {disk.total / (1024**3):.1f} GB ({disk.percent}%)",
            f"Uptime            : {str(uptime).split('.')[0]}",
        ]
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado en get_system_info")
        return f"Error: {exc}"


# --------------------------------------------------------------------------- #
# 7. list_connections
# --------------------------------------------------------------------------- #

def list_connections(limit: int = 25) -> str:
    """Lista conexiones de red activas del sistema local (solo lectura)."""
    limit = max(1, min(int(limit), 100))
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return (
            "Error: se requieren privilegios elevados para listar conexiones "
            "de red en este sistema (ejecuta el proceso con permisos suficientes)"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado en list_connections")
        return f"Error: {exc}"

    if not conns:
        return "No hay conexiones de red activas visibles"

    lines = []
    for c in conns[:limit]:
        laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "-"
        raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "-"
        proto = "TCP" if c.type == socket.SOCK_STREAM else "UDP"
        lines.append(f"  {proto:<4} {laddr:<22} → {raddr:<22} {c.status:<12} pid={c.pid}")

    header = f"Mostrando {min(len(conns), limit)} de {len(conns)} conexiones:"
    return header + "\n" + "\n".join(lines)


# --------------------------------------------------------------------------- #
# 8. http_check
# --------------------------------------------------------------------------- #

def http_check(url: str, method: str = "GET") -> str:
    """Hace una petición HTTP GET/HEAD y devuelve código, headers clave y tiempo de respuesta."""
    try:
        url = _validate_url(url)
    except ValueError as exc:
        return f"Error: {exc}"

    method = (method or "GET").strip().upper()
    if method not in _ALLOWED_HTTP_METHODS:
        return f"Error: método '{method}' no permitido. Usa GET o HEAD"

    try:
        resp = requests.request(
            method, url, timeout=DEFAULT_TIMEOUT * 2, allow_redirects=True
        )
    except requests.exceptions.SSLError as exc:
        return f"Error: fallo de certificado SSL al conectar a '{url}': {exc}"
    except requests.exceptions.ConnectionError as exc:
        return f"Error: no se pudo conectar a '{url}': {exc}"
    except requests.exceptions.Timeout:
        return f"Error: timeout esperando respuesta de '{url}'"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado en http_check")
        return f"Error: {exc}"

    elapsed_ms = round(resp.elapsed.total_seconds() * 1000, 1)
    key_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() in {"server", "content-type", "content-length", "location", "cache-control"}
    }
    headers_txt = "\n".join(f"  {k}: {v}" for k, v in key_headers.items()) or "  (sin headers relevantes)"
    redirect_txt = f"\nURL final tras redirecciones: {resp.url}" if resp.url != url else ""

    return (
        f"{method} {url}\n"
        f"Código HTTP: {resp.status_code} {resp.reason}\n"
        f"Tiempo de respuesta: {elapsed_ms} ms{redirect_txt}\n"
        f"Headers:\n{headers_txt}"
    )


# --------------------------------------------------------------------------- #
# 9. check_ssl_certificate
# --------------------------------------------------------------------------- #

def check_ssl_certificate(host: str, port: int = 443) -> str:
    """Se conecta por TLS a host:port y reporta emisor, validez, SAN y versión TLS."""
    try:
        host = _validate_host(host)
        port = _validate_port(port)
    except ValueError as exc:
        return f"Error: {exc}"

    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT * 2) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                tls_version = ssock.version()
    except ssl.SSLCertVerificationError as exc:
        return f"Error: el certificado de '{host}:{port}' no es válido: {exc}"
    except socket.timeout:
        return f"Error: timeout conectando a '{host}:{port}'"
    except (ConnectionRefusedError, socket.gaierror, OSError) as exc:
        return f"Error: no se pudo conectar a '{host}:{port}': {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo inesperado en check_ssl_certificate")
        return f"Error: {exc}"

    issuer = dict(x[0] for x in cert.get("issuer", []))
    subject = dict(x[0] for x in cert.get("subject", []))
    sans = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    not_before = cert.get("notBefore", "?")
    not_after = cert.get("notAfter", "?")

    days_left_txt = ""
    try:
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_left = (expires - datetime.now(timezone.utc)).days
        days_left_txt = f" ({days_left} días restantes)" if days_left >= 0 else f" (¡EXPIRADO hace {-days_left} días!)"
    except ValueError:
        pass

    return (
        f"Certificado SSL/TLS de {host}:{port}\n"
        f"Emisor       : {issuer.get('organizationName', issuer.get('commonName', '?'))}\n"
        f"Sujeto (CN)  : {subject.get('commonName', '?')}\n"
        f"Válido desde : {not_before}\n"
        f"Válido hasta : {not_after}{days_left_txt}\n"
        f"SAN          : {', '.join(sans) if sans else '(ninguno)'}\n"
        f"Versión TLS  : {tls_version}"
    )


# --------------------------------------------------------------------------- #
# 10. whois_lookup
# --------------------------------------------------------------------------- #

_IANA_WHOIS = "whois.iana.org"
_WHOIS_PORT = 43
_MAX_WHOIS_CHARS = 2500


def _whois_query(server: str, query: str, timeout: float = 6.0) -> str:
    with socket.create_connection((server, _WHOIS_PORT), timeout=timeout) as sock:
        sock.sendall((query + "\r\n").encode("utf-8", errors="ignore"))
        chunks = []
        sock.settimeout(timeout)
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout:
            pass
    return b"".join(chunks).decode("utf-8", errors="ignore")


def whois_lookup(domain: str) -> str:
    """Consulta WHOIS de un dominio implementando el protocolo a mano (socket, puerto 43).

    Primero pregunta a IANA cuál es el servidor WHOIS autoritativo para el
    TLD y luego repite la consulta contra ese servidor (siguiendo el
    'referral'), en vez de depender de un paquete externo con soporte
    irregular por TLD.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not _HOSTNAME_RE.match(domain):
        return f"Error: '{domain}' no parece un dominio válido"

    try:
        iana_response = _whois_query(_IANA_WHOIS, domain)
    except socket.timeout:
        return "Error: timeout consultando whois.iana.org"
    except OSError as exc:
        return f"Error: no se pudo contactar whois.iana.org: {exc}"

    referral_server = None
    for line in iana_response.splitlines():
        if line.lower().startswith("refer:"):
            referral_server = line.split(":", 1)[1].strip()
            break

    if not referral_server:
        text = iana_response.strip()[:_MAX_WHOIS_CHARS]
        return text or f"Error: IANA no devolvió información para '{domain}'"

    try:
        final_response = _whois_query(referral_server, domain)
    except (socket.timeout, OSError):
        # Si falla el servidor referido, al menos devolvemos lo que dio IANA
        final_response = iana_response

    text = final_response.strip()[:_MAX_WHOIS_CHARS]
    suffix = "\n\n[...salida truncada...]" if len(final_response.strip()) > _MAX_WHOIS_CHARS else ""
    return (text + suffix) if text else f"Error: sin datos WHOIS para '{domain}'"


# --------------------------------------------------------------------------- #
# 11. geoip_lookup
# --------------------------------------------------------------------------- #

def geoip_lookup(host_or_ip: str) -> str:
    """Geolocaliza aproximadamente una IP u host (ISP/ASN/ciudad/país) vía ip-api.com.

    Limitación conocida: ip-api.com en su capa gratuita permite ~45
    requests/minuto y solo ofrece el endpoint por HTTP (no HTTPS) —
    suficiente para una demo, no pensado para producción con volumen.
    """
    target = (host_or_ip or "").strip()
    if not target:
        return "Error: host o IP vacío"

    ip = target
    if not _is_valid_ip(target):
        try:
            ip = socket.gethostbyname(target)
        except socket.gaierror as exc:
            return f"Error: no se pudo resolver '{target}': {exc}"

    try:
        resp = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,message,country,regionName,city,isp,org,as,lat,lon,query"},
            timeout=DEFAULT_TIMEOUT,
        )
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        return f"Error: fallo consultando ip-api.com: {exc}"
    except ValueError:
        return "Error: respuesta inválida de ip-api.com"

    if data.get("status") != "success":
        return f"Error: {data.get('message', 'no se pudo geolocalizar la IP')}"

    return (
        f"GeoIP de {target} ({data.get('query', ip)})\n"
        f"País   : {data.get('country', '?')}\n"
        f"Región : {data.get('regionName', '?')}\n"
        f"Ciudad : {data.get('city', '?')}\n"
        f"ISP    : {data.get('isp', '?')}\n"
        f"Org    : {data.get('org', '?')}\n"
        f"ASN    : {data.get('as', '?')}\n"
        f"Lat/Lon: {data.get('lat', '?')}, {data.get('lon', '?')}"
    )


# --------------------------------------------------------------------------- #
# 12. speed_test_lite
# --------------------------------------------------------------------------- #

_SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes=10000000"  # 10 MB


def speed_test_lite() -> str:
    """Descarga ~10MB de prueba desde Cloudflare y estima el ancho de banda de bajada."""
    try:
        start = time.perf_counter()
        total_bytes = 0
        with requests.get(_SPEEDTEST_URL, stream=True, timeout=15) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=65536):
                total_bytes += len(chunk)
        elapsed = time.perf_counter() - start
    except requests.exceptions.RequestException as exc:
        return f"Error: no se pudo completar el test de velocidad: {exc}"

    if elapsed <= 0 or total_bytes == 0:
        return "Error: la prueba de velocidad no descargó datos"

    mbps = (total_bytes * 8) / elapsed / 1_000_000
    mb = total_bytes / (1024 * 1024)
    return (
        f"Descarga de prueba: {mb:.1f} MB en {elapsed:.2f} s\n"
        f"Velocidad estimada de bajada: {mbps:.1f} Mbps"
    )


# --------------------------------------------------------------------------- #
# Registro de specs (para function calling) + dispatcher de nombre → función
#
# Nota de diseño: el pedido original distinguía "dns_lookup" (básico) de un
# "DNS avanzado" nuevo con más tipos de registro. Se fusionaron en una sola
# herramienta `dns_query(host, record_type)` — tener dos tools que hacen lo
# mismo con distinto nombre solo confunde al modelo a la hora de elegir cuál
# llamar, sin aportar nada. Por eso el conteo final es 12 herramientas y no
# 13: una fusión deliberada, no una tool faltante.
# --------------------------------------------------------------------------- #

TOOL_SPECS: list[dict] = [
    {
        "name": "ping_host",
        "description": "Envía paquetes ICMP ping a un host y mide latencia/pérdida de paquetes. Primer paso típico ante 'no puedo acceder a X' o 'X está caído'.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Hostname o IP a hacer ping (ej: github.com, 8.8.8.8)"},
                "count": {"type": "integer", "description": "Cantidad de paquetes a enviar (1-10, default 4)"},
            },
            "required": ["host"],
        },
    },
    {
        "name": "traceroute_host",
        "description": "Traza la ruta de saltos de red hasta un host. Útil cuando ping falla, para ver en qué salto se pierde la conexión.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Hostname o IP destino"},
                "max_hops": {"type": "integer", "description": "Máximo de saltos a trazar (default 20)"},
            },
            "required": ["host"],
        },
    },
    {
        "name": "check_port",
        "description": "Verifica si un puerto TCP específico está abierto en un host (ej: 443 para HTTPS, 22 para SSH).",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Hostname o IP"},
                "port": {"type": "integer", "description": "Puerto TCP a verificar (1-65535)"},
                "timeout": {"type": "number", "description": "Timeout en segundos (default 3.0)"},
            },
            "required": ["host", "port"],
        },
    },
    {
        "name": "dns_query",
        "description": "Consulta registros DNS de un dominio: A, AAAA, MX, TXT, NS, CNAME o SOA.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Dominio a consultar (ej: github.com)"},
                "record_type": {
                    "type": "string",
                    "description": "Tipo de registro DNS: A, AAAA, MX, TXT, NS, CNAME o SOA (default A)",
                },
            },
            "required": ["host"],
        },
    },
    {
        "name": "reverse_dns",
        "description": "Resuelve una IP a su hostname mediante reverse DNS (PTR).",
        "parameters": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "Dirección IP a resolver"},
            },
            "required": ["ip"],
        },
    },
    {
        "name": "get_system_info",
        "description": "Devuelve información del sistema local donde corre el agente: SO, CPU, memoria, disco, uptime.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_connections",
        "description": "Lista las conexiones de red activas (TCP/UDP) del sistema local donde corre el agente.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Máximo de conexiones a mostrar (default 25)"},
            },
        },
    },
    {
        "name": "http_check",
        "description": "Hace una petición HTTP GET o HEAD a una URL y reporta código de estado, headers y tiempo de respuesta.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL completa, con http:// o https:// (ej: https://github.com)"},
                "method": {"type": "string", "description": "GET o HEAD (default GET)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "check_ssl_certificate",
        "description": "Revisa el certificado SSL/TLS de un host: emisor, fechas de validez, SAN y versión de TLS negociada.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Hostname (no IP) del servidor a revisar"},
                "port": {"type": "integer", "description": "Puerto HTTPS (default 443)"},
            },
            "required": ["host"],
        },
    },
    {
        "name": "whois_lookup",
        "description": "Consulta información WHOIS de un dominio (registrante, fechas de registro/expiración, servidores de nombres).",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Dominio a consultar (ej: example.com)"},
            },
            "required": ["domain"],
        },
    },
    {
        "name": "geoip_lookup",
        "description": "Geolocaliza aproximadamente un host o IP: país, ciudad, ISP y ASN.",
        "parameters": {
            "type": "object",
            "properties": {
                "host_or_ip": {"type": "string", "description": "Hostname o IP a geolocalizar"},
            },
            "required": ["host_or_ip"],
        },
    },
    {
        "name": "speed_test_lite",
        "description": "Mide de forma aproximada el ancho de banda de bajada del servidor donde corre el agente, descargando un archivo de prueba.",
        "parameters": {"type": "object", "properties": {}},
    },
]

TOOL_FUNCTIONS: dict[str, Callable[..., str]] = {
    "ping_host": ping_host,
    "traceroute_host": traceroute_host,
    "check_port": check_port,
    "dns_query": dns_query,
    "reverse_dns": reverse_dns,
    "get_system_info": get_system_info,
    "list_connections": list_connections,
    "http_check": http_check,
    "check_ssl_certificate": check_ssl_certificate,
    "whois_lookup": whois_lookup,
    "geoip_lookup": geoip_lookup,
    "speed_test_lite": speed_test_lite,
}

assert {spec["name"] for spec in TOOL_SPECS} == set(TOOL_FUNCTIONS), (
    "TOOL_SPECS y TOOL_FUNCTIONS deben tener exactamente los mismos nombres de herramienta"
)

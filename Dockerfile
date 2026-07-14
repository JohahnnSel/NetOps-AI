FROM python:3.12-slim

# iputils-ping y traceroute son paquetes de sistema (no de pip): sin ellos,
# ping_host y traceroute_host fallarían dentro del contenedor con
# "el comando no está disponible". El resto de herramientas de tools.py
# son socket/requests puros y no necesitan nada más del sistema operativo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping traceroute \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# La base SQLite vive acá; docker-compose.yml monta ./data como volumen
# para que sobreviva a un `docker compose down`.
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

CMD ["python", "app.py"]

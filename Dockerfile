# Grand Prix Alpha-Scalp — Production Dockerfile
#
# Design: dependencies are baked into the image; source code is
# bind-mounted from the VPS filesystem at runtime. This means:
#   - `docker build` only needed when requirements.txt changes
#   - `git pull + docker compose restart` handles all code updates
#   - Data files (trades.jsonl, bot_state.json …) persist on the VPS disk

FROM python:3.11-slim

# System deps required by pandas / numpy / ccxt / websockets
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps as a separate cached layer.
# Only invalidated when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source code is NOT copied here — it arrives via bind mount in docker-compose.
# This keeps the image small and makes code updates instant (no rebuild).

# Unbuffered output so docker logs shows everything in real time
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "main.py"]
